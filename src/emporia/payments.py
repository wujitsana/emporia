"""Payment helpers for Emporia: x402 (USDC on EVM and Solana) and Tempo.

The relay never creates wallets or payment accounts for anyone and never makes,
confirms, transfers or refunds a payment on anyone's behalf. The payer pays the
payee directly; the relay only verifies proof. Platform fee: `fees.fee_bps()`.
(The card rail was removed in this release; see docs/payments.md.)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx



def settlement_dedupe_key(
    session_id: str, payer_id: str | None, payment_intent_ids: Sequence[str]
) -> str:
    """The natural key for a settlement: one row per (resource, payer, payment) —
    not per (resource, payer), which is what P1a-2 shipped and P1r-1 corrects.

    A payer can legitimately pay the same resource more than once (join a paid
    room, get kicked, rejoin; unsubscribe from a paid Agora topic, get
    re-invited, resubscribe — both membership paths re-admit cleanly after
    removal). Keying on `(session_id, payer_id)` alone collapsed two genuinely
    different payments into one settlement row and, downstream, one settlement.

    `payment_intent_ids` is folded in via a short SHA-256 digest rather than
    joined in raw, for two reasons: (1) a session-escrow row's
    payment list is unbounded across participants;
    (2) a fixed-width digest keeps every caller's key the same shape regardless
    of how many PIs are involved. The list is sorted before hashing so a
    session row's multi-PI dedupe key doesn't depend on insertion order.

    Used as `settlements.dedupe_key` (the storage-level natural key).
    """
    digest = hashlib.sha256(",".join(sorted(payment_intent_ids)).encode()).hexdigest()[:16]
    return f"{session_id}:{payer_id or ''}:{digest}"


def platform_fee(amount_cents: int) -> int:
    """1% platform fee in cents. Only collected when the rounded amount is ≥ 1¢.
    Transactions where the fee rounds to 0 pay no fee — payout goes 100% to the recipient.
    """
    if amount_cents <= 0:
        return 0
    from emporia import fees
    fee = round(amount_cents * fees.fee_bps() / 10000)
    return fee if fee >= 1 else 0


# P3: the rail seam. "A payment" used to be spelled `payment_intent_id` even in
# places meant to be rail-agnostic (access_entitlements.payment_intent_id, most
# glaringly) — there was no way to say *which* rail a reference belongs to.
# KNOWN_RAILS is the one canonical vocabulary for that tag, lives
# here (the payment-domain module) rather than in relay_server.py so
# entitlements.py — which must stay import-independent of relay_server.py, see
# its own module docstring — can import it too without a cycle
# (entitlements -> payments is acyclic; payments never imports entitlements).
#
# P1b: the second rail lands. RAIL_X402 is x402 (see the module docstring at
# the top of this pass's x402_* functions below) — a request-scoped,
# non-custodial stablecoin settlement, verified/settled via a facilitator. KNOWN_RAILS widening to two members is the
# whole point of P3's seam: every already-shipped `rail in KNOWN_RAILS` guard
# (record_payment, record_settlement, entitlements.grant) picks this up with
# no further change.
RAIL_X402 = "x402"
# P156: Tempo stablecoin, paid straight to the payee and checked on chain.
RAIL_TEMPO = "tempo"
KNOWN_RAILS = frozenset({RAIL_X402, RAIL_TEMPO})


@dataclass(frozen=True)
class PaymentRef:
    """A verified payment reference, tagged with the rail it was verified on.

    Returned by `_verify_and_confirm_payment()` (relay_server.py) — the single
    funnel all three paid routes (session join, room join, Agora subscribe)
    call to turn a client-supplied credential into a confirmed payment. Before
    P3 that funnel returned a bare `str` (the payment reference), so
    every downstream persistence point (record_payment, entitlements.grant,
    record_settlement) had no rail to record even after their own schema grew
    a `rail` column for exactly this purpose (item 2).

    `reference` is deliberately the *only* thing that ever reaches
    `settlement_dedupe_key()` — see that function's own docstring. The rail
    tag must never be folded into the hashed string.
    """

    rail: str
    reference: str
    # A split x402 entry (fees.py): the second payment, to the relay's fee address, and its amount.
    fee_reference: str | None = None
    fee_cents: int = 0



REQUEST_TIMEOUT = float(os.getenv("EMPORIA_HTTP_TIMEOUT", "30"))


# ─── x402 config (P1b) ───────────────────────────────────────────────────────
# Lazy accessors, not import-time module constants — re-read on every call, patchable
# via the env var directly in tests.

def _x402_facilitator_url() -> str:
    """L6: the default MUST stay blank — never a live/mainnet facilitator URL.
    A blank facilitator disables the rail outright (see _x402_enabled())."""
    return os.getenv("EMPORIA_X402_FACILITATOR_URL", "").strip()


def _x402_networks() -> list[str]:
    """CSV of CAIP-2 network ids, e.g. 'eip155:8453,solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'."""
    raw = os.getenv("EMPORIA_X402_NETWORKS", "").strip()
    if not raw:
        return []
    return [n.strip() for n in raw.split(",") if n.strip()]


def _x402_enabled() -> bool:
    """The rail is enabled only when all three of the enabled flag, a
    facilitator URL, and >=1 usable network (one with an asset configured)
    are present. A partially configured rail behaves exactly like a disabled
    one rather than failing halfway through building a 402 challenge."""
    return (
        os.getenv("EMPORIA_X402_ENABLED", "0").strip() == "1"
        and bool(_x402_facilitator_url())
        and any(_x402_network_parts(n) for n in _x402_networks())
    )


def _x402_asset_for(network: str) -> str | None:
    """The USDC contract/mint address for a CAIP-2 network id — deliberately
    **no built-in defaults**. A wrong token address is unrecoverable money,
    the same reasoning item 2's wallet-format validation is built on, and
    Coinbase's own network-support docs don't publish a canonical address
    list either ("consult the token registry") — this was checked against
    live docs during planning rather than assumed. The operator must set
    EMPORIA_X402_ASSET_<NETWORK> explicitly (':' and '-' become '_',
    uppercased) for each network they actually want advertised; an
    unconfigured network is silently excluded from `accepts`, not guessed at.
    """
    env_name = "EMPORIA_X402_ASSET_" + re.sub(r"[:-]", "_", network.upper())
    value = os.getenv(env_name, "").strip()
    return value or None


def x402_network_family(network: str) -> str | None:
    """CAIP-2 network id -> the wallet key space / signing mechanism it needs
    (P1b-2's network_family). Unrecognized namespaces resolve to None so a
    misconfigured EMPORIA_X402_NETWORKS entry is silently excluded rather than
    raising mid-request.

    P45: moved here from relay_server.py. It used to live beside the wallet
    lookup because that was its only caller; it now also decides whether a
    network's requirement needs an EIP-712 domain (below), and two copies of
    "which prefix means EVM" is exactly the drift P1br-2 factored
    `x402_requirement()` out to prevent.
    """
    if network.startswith("eip155:"):
        return "evm"
    if network.startswith("solana:"):
        return "svm"
    return None


# The `exact` scheme on EVM authorizes payment with an EIP-712 signature over an
# EIP-3009 `transferWithAuthorization` struct, and the EIP-712 domain includes
# the token contract's own `name` and `version`. Those are readable only from the
# chain, so the spec carries them in `PaymentRequirements.extra` — which means a
# challenge that omits `extra` is a challenge no EVM payer can sign. P1brr
# recorded "`extra` is never sent" as a known unknown; P45 found it was the
# reason the rail had no reachable client (F4) and made it required per family.
_X402_FAMILIES_REQUIRING_EXTRA = frozenset({"evm"})


def _x402_extra_for(network: str) -> dict[str, Any] | None:
    """The `PaymentRequirements.extra` object for one CAIP-2 network, read from
    EMPORIA_X402_EXTRA_<NETWORK> as a JSON object — same env-name transform as
    `_x402_asset_for()` above (':' and '-' -> '_', uppercased), because an
    operator configures the asset and its signing domain for the same network id
    and having the two transforms disagree would silently drop one of them.

    Carried verbatim as an object rather than as name/version scalars: `extra`
    is scheme-defined, so this same var covers EVM's `{name, version}`, the
    protocol-reserved `assetTransferMethod`/`paymentFlow` keys, and whatever a
    non-EVM mechanism needs, without inventing a new env var per scheme.

    No built-in defaults, for `_x402_asset_for()`'s reason: the domain is part
    of what the payer signs, and a wrong one produces a signature that the
    facilitator rejects (at best) against a token that isn't the one advertised.
    Unset *and* unparseable both return None — see `_x402_network_parts()` for
    why that excludes the network rather than degrading the challenge.
    """
    env_name = "EMPORIA_X402_EXTRA_" + re.sub(r"[:-]", "_", network.upper())
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    return parsed


def _x402_network_parts(network: str) -> tuple[str, dict[str, Any] | None] | None:
    """(asset, extra) for a fully configured network, else None.

    The single definition of "this relay can actually offer this network",
    shared by `_x402_enabled()` and `build_x402_challenge()` so the enabled
    check and the `accepts` builder can never disagree about which networks
    count — they used to share only the asset half of the condition, and P45
    added a second half that would otherwise have had to be written twice.

    A family in `_X402_FAMILIES_REQUIRING_EXTRA` with no usable `extra` is
    excluded outright rather than advertised with the domain missing: the
    resulting challenge would be well-formed, accepted by every existing test,
    and unpayable.
    """
    asset = _x402_asset_for(network)
    if not asset:
        return None
    extra = _x402_extra_for(network)
    if extra is None and x402_network_family(network) in _X402_FAMILIES_REQUIRING_EXTRA:
        return None
    return asset, extra



def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ─── x402 / 402 helpers (P1b) ────────────────────────────────────────────────
# x402 v2 spec: https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md
#
# x402 v2 moved payment data into headers rather than a response body — the
# PAYMENT-REQUIRED header below carries the base64-encoded, spec-correct
# PaymentRequired object, which is what an x402-aware client is expected to
# decode. It is also mirrored under a nested "x402" body key for a client
# that inspects the JSON body instead — nested, not flattened to top-level
# keys, because the body already has a "resource" key (a plain
# string, e.g. "emporia:room:{room_id}") and x402's own `resource` field is
# an object.
#
# USDC uses 6 decimals on every network x402 supports today (confirmed
# during P1b planning) — amount_cents (always USD cents in Emporia's own
# convention) converts to atomic units assuming a 1:1 USDC/USD peg, the
# standard assumption every USDC-based x402 integration makes.
_USDC_DECIMALS = 6


def _x402_amount_atomic(amount_cents: int) -> str:
    """USD cents -> atomic USDC units (6 decimals, 1:1 peg assumption — see
    build_x402_challenge()'s docstring above). Shared with
    _verify_and_confirm_payment()'s x402 branch (relay_server.py) so the
    challenge and the later resource-binding assertion can never compute two
    different numbers for the same amount_cents."""
    return str(amount_cents * (10 ** _USDC_DECIMALS) // 100)


# x402 v2 spec's default; not currently configurable per network/operator.
_X402_MAX_TIMEOUT_SECONDS = 300


def x402_requirement(
    network: str | None, amount_cents: int, asset: str | None, pay_to: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `accepts` entry / PaymentRequirements object — the single
    definition of the x402 requirement shape, used identically by
    build_x402_challenge() (below, what this relay *offers*) and
    _verify_and_confirm_payment()'s x402 branch (relay_server.py, what a
    submitted credential must *match*).

    P1br-2: `"scheme": "exact"` and `maxTimeoutSeconds` used to be
    duplicated literals at both call sites — change one without the other
    and the resource-binding assertion silently rejects every payment,
    with no test able to tell you why. Factored here so the two can never
    drift apart; `_x402_amount_atomic()` was already shared, this widens
    the shared surface to the whole object.

    Deliberately tolerant of `None` inputs (a missing network/asset/payTo)
    rather than raising — the verification caller builds a requirement from
    a possibly-incomplete client claim specifically so it can list every
    mismatched field at once, not just the first one that would raise.
    """
    requirement = {
        "scheme": "exact",
        "network": network,
        "amount": _x402_amount_atomic(amount_cents),
        "asset": asset,
        "payTo": pay_to,
        "maxTimeoutSeconds": _X402_MAX_TIMEOUT_SECONDS,
    }
    if extra is not None:
        requirement["extra"] = extra
    elif network and x402_network_family(network) in _X402_FAMILIES_REQUIRING_EXTRA:
        # P45: `extra` is Optional in the spec, so absence normally means "omit
        # the key". Not here. On a family that signs with EIP-712 the domain is
        # part of what makes the requirement satisfiable at all, so a missing
        # one is recorded as an explicit None — which the verification caller's
        # `want is None` rule already turns into a named 402 rather than a
        # requirement whose domain simply goes unasserted.
        requirement["extra"] = None
    return requirement


def build_x402_challenge(
    amount_cents: int,
    resource_url: str,
    pay_to_by_network: dict[str, str],
    currency: str = "usd",
) -> dict[str, Any] | None:
    """Build the x402 v2 `accepts` array plus the PAYMENT-REQUIRED header.

    `pay_to_by_network` is `{CAIP-2 network id: creator's payTo address}`,
    already resolved and filtered by the caller (relay_server.py — it needs
    AGENT_REGISTRY, which this payment-domain module doesn't import; see
    _x402_pay_to_by_network()). Only x402-enabled networks with both a
    configured asset (_x402_asset_for) and a wallet for this creator make it
    into `accepts` (P45: "configured" now also means a usable `extra` where the
    mechanism needs one — see `_x402_network_parts()`).

    Returns None (offer nothing) when x402 isn't enabled, the currency isn't
    "usd" (USDC's peg assumption above only holds for USD), or no network is
    both fully configured and backed by a wallet for this creator — the caller
    then omits x402 from the 402 entirely rather than sending an empty
    `accepts` array.
    """
    if currency != "usd" or not _x402_enabled():
        return None
    accepts = []
    for network in _x402_networks():
        pay_to = pay_to_by_network.get(network)
        parts = _x402_network_parts(network)
        if not pay_to or parts is None:
            continue
        asset, extra = parts
        accepts.append(x402_requirement(network, amount_cents, asset, pay_to, extra))
    if not accepts:
        return None
    payment_required = {
        "x402Version": 2,
        "resource": {"url": resource_url},
        "accepts": accepts,
    }
    return {
        "body": payment_required,
        "headers": {"PAYMENT-REQUIRED": _b64url(json.dumps(payment_required).encode())},
    }


def extract_x402_payload(payment_signature_header: str | None) -> dict[str, Any] | None:
    """Decode the client's retry credential from the PAYMENT-SIGNATURE header.
    Per the v2 spec this is base64-encoded JSON: `{x402Version, resource?, accepted, payload,
    extensions?}`. Returns None (not a raised error) for anything malformed —
    the caller treats "no x402 credential" the same as "no credential at all"
    and falls through to issuing a fresh 402 challenge."""
    if not payment_signature_header:
        return None
    raw = payment_signature_header.strip()
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if not isinstance(decoded, dict) or "accepted" not in decoded or "payload" not in decoded:
        return None
    return decoded


async def x402_verify_payment(payment_payload: dict[str, Any], payment_requirements: dict[str, Any]) -> dict[str, Any]:
    """POST to the configured facilitator's /verify — confirms the signed
    payload actually satisfies `payment_requirements`, without moving funds
    (free, per the x402 spec). Shaped like verify_payment_intent() above
    (call the backend, return its response) so the same
    @patch("emporia.payments.x402_verify_payment") test convention applies.

    Callers must pass THEIR OWN server-computed `payment_requirements` here —
    never the client's `payload_payload["accepted"]` as-is — see
    _verify_and_confirm_payment()'s x402 branch for why: the requirement is
    the resource-binding assertion, not a courtesy echo of the client's claim.
    """
    facilitator_url = _x402_facilitator_url()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{facilitator_url.rstrip('/')}/verify",
            json={
                "x402Version": 2,
                "paymentPayload": payment_payload,
                "paymentRequirements": payment_requirements,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def x402_settle_payment(payment_payload: dict[str, Any], payment_requirements: dict[str, Any]) -> dict[str, Any]:
    """POST to the configured facilitator's /settle — executes the payment
    on-chain. Real money moves here; unlike x402_verify_payment() this is not
    free or idempotent from the facilitator's point of view. Only ever call
    this after the resource-binding assertion has passed (see this module's
    x402 section docstring and _verify_and_confirm_payment()'s x402 branch).
    """
    facilitator_url = _x402_facilitator_url()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{facilitator_url.rstrip('/')}/settle",
            json={
                "x402Version": 2,
                "paymentPayload": payment_payload,
                "paymentRequirements": payment_requirements,
            },
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# P48: reading EIP-3009 authorization state back off the chain
#
# x402's /settle has no Idempotency-Key and the spec offers no equivalent, so
# a relay that crashes between a successful settlement and its own durable
# record has, on the protocol's own terms, no way to ask "did that land?".
# It does not need one. The `exact` scheme on EVM is EIP-3009
# `transferWithAuthorization`, and EIP-3009 keeps a per-authorizer nonce map
# specifically so an authorization can be spent at most once — which makes
# the chain itself the idempotency record the protocol lacks. Two reads:
#
#   authorizationState(address,bytes32) -> bool   did this nonce get spent?
#   AuthorizationUsed(address,bytes32)  (event)   ...and in which transaction?
#
# Both constants below are the keccak of their own signature and are pinned
# by a test that derives them rather than restating them. Verified against
# Base Sepolia during P48 using P45's real settlement (see docs/security-log.md).
# ---------------------------------------------------------------------------

EIP3009_AUTHORIZATION_STATE_SELECTOR = "0xe94a0102"
EIP3009_AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
)

RECONCILE_BLOCK_WINDOW = int(os.getenv("EMPORIA_X402_RECONCILE_BLOCKS", "10000") or "10000")


def _x402_rpc_url_for(network: str) -> str | None:
    """The JSON-RPC endpoint for a CAIP-2 network id, or None if the operator
    hasn't configured one. Same no-built-in-defaults posture as
    `_x402_asset_for()`, for a weaker reason: a wrong RPC endpoint isn't
    unrecoverable money, it's a wrong answer to "was this payer charged?",
    which is the question the whole reconciliation path exists to answer.
    Unset means reconciliation reports "unknown" — never "not charged"; see
    `x402_authorization_used()`'s tri-state return.
    """
    env_name = "EMPORIA_X402_RPC_" + re.sub(r"[:-]", "_", network.upper())
    return os.getenv(env_name, "").strip() or None


def _pad32_address(address: str) -> str:
    """An address as a 32-byte ABI word / log topic (64 hex chars, no 0x)."""
    return address.lower().removeprefix("0x").rjust(64, "0")


def _bytes32(value: str) -> str:
    """A bytes32 as 64 hex chars, no 0x — accepts either form."""
    return value.lower().removeprefix("0x").rjust(64, "0")


async def _eth_rpc(rpc_url: str, method: str, params: list[Any]) -> Any:
    """One JSON-RPC round trip. Raises on transport failure or an RPC error
    object; callers translate that into "unknown", never into "unspent"."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"{method} failed: {body['error']}")
    return body.get("result") if isinstance(body, dict) else None


async def x402_authorization_used(
    network: str, asset: str, authorizer: str, nonce: str,
) -> bool | None:
    """Has this EIP-3009 authorization already been spent?

    **Tri-state on purpose.** True = spent (the payer was charged), False =
    not spent (they were not), and None = *unknown* — no RPC configured, or
    the call failed. Collapsing None into False is the one mistake that would
    hand the double-charge straight back: "I couldn't reach the chain" and
    "the money never moved" are the same value to a caller that only tests
    truthiness, and only one of them makes it safe to settle again.
    """
    rpc_url = _x402_rpc_url_for(network)
    if not rpc_url or not asset or not authorizer or not nonce:
        return None
    data = (
        EIP3009_AUTHORIZATION_STATE_SELECTOR
        + _pad32_address(authorizer)
        + _bytes32(nonce)
    )
    try:
        result = await _eth_rpc(rpc_url, "eth_call", [{"to": asset, "data": data}, "latest"])
    except Exception:
        return None
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    try:
        return int(result, 16) != 0
    except ValueError:
        return None


async def x402_authorization_transaction(
    network: str, asset: str, authorizer: str, nonce: str,
) -> str | None:
    """The transaction hash that spent this authorization, by its
    `AuthorizationUsed(authorizer, nonce)` log, or None if it can't be found.

    The search window is bounded to the last `EMPORIA_X402_RECONCILE_BLOCKS`
    blocks (P44's rule: no unbounded work on a request path, and public RPC
    providers reject wide `eth_getLogs` ranges anyway). Not finding the log
    is **not** evidence the payment didn't happen — that question belongs to
    `x402_authorization_used()`, which reads a permanent state map rather
    than a retention-limited log index. A caller that has a True from there
    and a None from here knows the payer was charged and simply lacks the
    tx hash; it must not conclude the settlement never happened.
    """
    rpc_url = _x402_rpc_url_for(network)
    if not rpc_url or not asset or not authorizer or not nonce:
        return None
    try:
        head = await _eth_rpc(rpc_url, "eth_blockNumber", [])
        head_block = int(head, 16)
        from_block = max(0, head_block - max(1, RECONCILE_BLOCK_WINDOW))
        logs = await _eth_rpc(rpc_url, "eth_getLogs", [{
            "address": asset,
            "fromBlock": hex(from_block),
            # The head we just read, not "latest". Providers cap this range —
            # Base's public RPC at 10,000 blocks — and "latest" advances between
            # the two calls, so a window sized exactly at the cap would fail
            # intermittently, in a code path that only ever runs while a payment
            # is already in an uncertain state.
            "toBlock": hex(head_block),
            "topics": [
                EIP3009_AUTHORIZATION_USED_TOPIC,
                "0x" + _pad32_address(authorizer),
                "0x" + _bytes32(nonce),
            ],
        }])
    except Exception:
        return None
    if not isinstance(logs, list) or not logs:
        return None
    first = logs[0]
    return first.get("transactionHash") if isinstance(first, dict) else None




















































