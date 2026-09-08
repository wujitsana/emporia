"""Stripe payment processing for Emporia.

Payment modes supported:
  free        — no payment required
  stripe_spt  — Shared Payment Token (spt_xxx) from Stripe Link / link-cli.
                Agent calls `link-cli mpp pay`; relay validates via
                GET /v1/shared_payment/granted_tokens/{spt_id}, then creates a
                PaymentIntent with payment_method_data[shared_payment_granted_token].
                Requires a US Stripe Link account on the paying agent.
                Docs: https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens
  stripe_pi   — Standard Stripe PaymentIntent (pi_xxx). Agent or relay creates the PI;
                relay verifies status == "succeeded" or "requires_capture".
  mpp         — Machine Payments Protocol (future). Relay issues HTTP 402 with
                WWW-Authenticate: Payment ... challenge; agent wallet retries.
                Kept as enum value; full MPP server wiring is post-hackathon.

Payment split: 97.5% winner / 2.5% platform (OPERATOR_FEE_BPS=250 default).
STRIPE_SECRET_KEY must come from env — never hardcoded.

Stripe does NOT provide agent identity. The SPT object exposes only:
card brand, last-four, usage limits (currency/max_amount/expires_at), deactivation
status. Agent identity on Emporia stays with Ed25519 + Nous — Stripe is payment only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
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
    different payments into one settlement row and, downstream, one Stripe
    payout — proved end-to-end via the HTTP API this pass: two real charges,
    one settlement, one transfer.

    `payment_intent_ids` is folded in via a short SHA-256 digest rather than
    joined in raw, for two reasons: (1) Stripe caps `Idempotency-Key` at 255
    chars, and a session-escrow row's PI list is unbounded across participants;
    (2) a fixed-width digest keeps every caller's key the same shape regardless
    of how many PIs are involved. The list is sorted before hashing so a
    session row's multi-PI dedupe key doesn't depend on insertion order.

    Used both as `settlements.dedupe_key` (the storage-level natural key) and,
    by every payout/refund call site, as the base of that operation's Stripe
    `Idempotency-Key` — so the two agree by construction instead of by two
    people separately getting the shape right.
    """
    digest = hashlib.sha256(",".join(sorted(payment_intent_ids)).encode()).hexdigest()[:16]
    return f"{session_id}:{payer_id or ''}:{digest}"


def platform_fee(amount_cents: int) -> int:
    """2.5% platform fee in cents. Only collected when the rounded amount is ≥ 1¢.
    Transactions where 2.5% rounds to 0 pay no fee — payout goes 100% to the recipient.
    """
    if amount_cents <= 0:
        return 0
    fee = round(amount_cents * OPERATOR_FEE_BPS / 10000)
    return fee if fee >= 1 else 0


# P3: the rail seam. "A payment" used to be spelled `payment_intent_id` even in
# places meant to be rail-agnostic (access_entitlements.payment_intent_id, most
# glaringly) — there was no way to say *which* rail a reference belongs to.
# RAIL_STRIPE/KNOWN_RAILS is the one canonical vocabulary for that tag, lives
# here (the payment-domain module) rather than in relay_server.py so
# entitlements.py — which must stay import-independent of relay_server.py, see
# its own module docstring — can import it too without a cycle
# (entitlements -> payments is acyclic; payments never imports entitlements).
#
# P1b: the second rail lands. RAIL_X402 is x402 (see the module docstring at
# the top of this pass's x402_* functions below) — a request-scoped,
# non-custodial stablecoin settlement, verified/settled via a facilitator
# rather than a Stripe API call. KNOWN_RAILS widening to two members is the
# whole point of P3's seam: every already-shipped `rail in KNOWN_RAILS` guard
# (record_payment, record_settlement, entitlements.grant) picks this up with
# no further change.
RAIL_STRIPE = "stripe"
RAIL_X402 = "x402"
KNOWN_RAILS = frozenset({RAIL_STRIPE, RAIL_X402})


@dataclass(frozen=True)
class PaymentRef:
    """A verified payment reference, tagged with the rail it was verified on.

    Returned by `_verify_and_confirm_payment()` (relay_server.py) — the single
    funnel all three paid routes (session join, room join, Agora subscribe)
    call to turn a client-supplied credential into a confirmed payment. Before
    P3 that funnel returned a bare `str` (the Stripe PaymentIntent id), so
    every downstream persistence point (record_payment, entitlements.grant,
    record_settlement) had no rail to record even after their own schema grew
    a `rail` column for exactly this purpose (item 2).

    `reference` is deliberately the *only* thing that ever reaches
    `settlement_dedupe_key()` or a Stripe `Idempotency-Key` — see that
    function's own docstring. The rail tag must never be folded into the
    hashed string: every settlement's `dedupe_key` and every payout/refund
    idempotency key derived from it would silently change value the moment it
    did, breaking in-flight idempotency guarantees for existing rows.
    """

    rail: str
    reference: str


STRIPE_API_BASE = os.getenv("STRIPE_API_BASE", "https://api.stripe.com/v1")
STRIPE_API_VERSION = os.getenv("STRIPE_API_VERSION", "2026-04-22.preview")
OPERATOR_FEE_BPS = int(os.getenv("OPERATOR_FEE_BPS", "250"))  # 2.5%

# P74: the constant `settle()`'s docstring has always cited and that never
# existed. It claimed "Requires total_stake_cents >= STRIPE_MIN_CHARGE_CENTS
# (50c) - enforced upstream"; nothing upstream enforced anything, and the only
# amount guard in this module was `amount_cents <= 0`. So a resource priced at
# a penny passed every check here and was rejected by Stripe, and the agent saw
# a raw provider error instead of a relay that had declined to sell it.
#
# 50 is Stripe's USD floor. It is NOT universal - the minimum differs per
# currency - and this codebase only ever charges in USD today. A second
# currency needs a per-currency table rather than this constant widened, which
# is why the value is named for the rail rather than for "money".
STRIPE_MIN_CHARGE_CENTS = 50
REQUEST_TIMEOUT = float(os.getenv("EMPORIA_HTTP_TIMEOUT", "30"))
# Machine Payment Profile ID for the Stripe fiat MPP rail.
# Set via Stripe dashboard → Machine Payments → Profile. Used in mppStripe.charge().
STRIPE_PROFILE_ID = os.getenv("STRIPE_PROFILE_ID", "")


# ─── x402 config (P1b) ───────────────────────────────────────────────────────
# Lazy accessors, not import-time module constants — mirrors relay_server.py's
# _stripe_profile_ready() (re-read on every call, patchable via the env var
# directly in tests) rather than TEMPO_ENABLED (an import-time constant that
# forces tests to patch the module attribute instead). See the P1b plan's
# config section for why that distinction matters here.

def _x402_facilitator_url() -> str:
    """L6: the default MUST stay blank — never a live/mainnet facilitator URL.
    A blank facilitator disables the rail outright (see _x402_enabled()), the
    same posture as the never-reached live rk_live_ Stripe key."""
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

# Per-process secret for binding MPP challenges (32 random bytes).
# The mppx library uses this to sign challenge payloads so they can't be replayed
# across relay instances. Generate once per process — challenges expire at request end.
_MPP_SECRET = secrets.token_bytes(32)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ─── MPP / 402 helpers ───────────────────────────────────────────────────────
# Stripe MPP spec: https://docs.stripe.com/payments/machine/mpp
# mppx library reference: https://github.com/stripe-samples/machine-payments
#
# Stripe MPP challenge-response (fiat/SPT path):
#   1. Agent sends request with no payment credential
#   2. Relay responds 402 with:
#      WWW-Authenticate: Payment id="<chal_id>", method="stripe",
#                        intent="charge", request="<b64url_payload>"
#   3. Agent wallet (link-cli) reads the challenge, generates spt_xxx
#   4. Agent retries with:
#      Authorization: Payment <b64url_credential>
#   5. Relay extracts spt_xxx from credential, calls retrieve_spt() + confirm_spt()
#
# For agents that provide SPTs proactively (no 402 retry needed), skip to step 5.

def build_mpp_challenge(amount_cents: int, resource: str, currency: str = "usd") -> dict[str, str]:
    """Build HTTP 402 challenge headers per the official Stripe MPP spec.

    Emits the `Payment` authentication scheme required by Stripe's MPP protocol
    (https://docs.stripe.com/payments/machine/mpp). The `request` field is a
    base64url-encoded JSON payload with amount, currency, and resource.

    Challenge ID is relay-generated (prefix chal_). The mppx library on the
    agent side uses the `id` to correlate the challenge with its credential response.

    WWW-Authenticate format:
      Payment id="chal_xxx", method="stripe", intent="charge", request="<b64url>"

    Agent Authorization response:
      Authorization: Payment <b64url_credential>
    """
    chal_id = f"chal_{secrets.token_hex(12)}"
    request_payload = _b64url(json.dumps({
        "amount": amount_cents,
        "currency": currency,
        "resource": resource,
    }).encode())
    profile_part = f', profile_id="{STRIPE_PROFILE_ID}"' if STRIPE_PROFILE_ID else ""
    return {
        "WWW-Authenticate": (
            f'Payment id="{chal_id}", method="stripe", intent="charge"'
            f', request="{request_payload}"{profile_part}'
        ),
        "X-MPP-Version": "1",
        "X-MPP-Network": "stripe",
    }


# ─── x402 / 402 helpers (P1b) ────────────────────────────────────────────────
# x402 v2 spec: https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md
# A sibling of build_mpp_challenge() above, not one function with a flag —
# ROADMAP.md is explicit that x402 stays a fully separate code path from
# Stripe MPP. The two protocols share only the "HTTP 402, structured
# challenge, wallet retries" shape; everything else (transport, settlement
# backend, credential format) differs.
#
# x402 v2 moved payment data into headers rather than a response body — the
# PAYMENT-REQUIRED header below carries the base64-encoded, spec-correct
# PaymentRequired object, which is what an x402-aware client is expected to
# decode. It is also mirrored under a nested "x402" body key for a client
# that inspects the JSON body instead — nested, not flattened to top-level
# keys, because the existing MPP body already has a "resource" key (a plain
# string, e.g. "emporia:room:{room_id}") and x402's own `resource` field is
# an object; flattening the two would silently change the MPP body's
# existing "resource" field's type out from under any client that has never
# heard of x402.
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
    """Decode the client's retry credential from the PAYMENT-SIGNATURE header —
    the x402 analogue of extract_mpp_token() above. Per the v2 spec this is
    base64-encoded JSON: `{x402Version, resource?, accepted, payload,
    extensions?}`. Returns None (not a raised error) for anything malformed —
    the caller treats "no x402 credential" the same as "no credential at all"
    and falls through to issuing a fresh 402 challenge, exactly like a missing
    Authorization header does for MPP."""
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
# Base Sepolia during P48 using P45's real settlement (see SECURITY.md).
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


def extract_mpp_token(authorization_header: str | None) -> str | None:
    """Extract SPT from Authorization header per Stripe MPP spec.

    Expected format (https://docs.stripe.com/payments/machine/mpp):
      Authorization: Payment <token_or_b64url_credential>

    The credential from mppx is base64url-encoded JSON containing the spt_xxx.
    A bare spt_xxx (from direct link-cli usage) is returned as-is.
    """
    if not authorization_header:
        return None
    header = authorization_header.strip()
    if not header.startswith("Payment "):
        return None
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    raw = parts[1].strip()
    # Try to decode as base64url JSON credential (mppx format)
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        if isinstance(decoded, dict):
            return decoded.get("token") or decoded.get("spt") or decoded.get("credential")
    except Exception:
        pass
    return raw or None


async def create_test_spt(amount_cents: int, currency: str = "usd") -> dict[str, Any]:
    """Mint a Stripe sandbox shared payment token for demo/bootstrap flows.

    Requires a Stripe test secret key and the shared payment preview API version.
    This is for local hackathon/demo automation only; production agents should
    obtain SPTs through Link approval or another MPP wallet flow.
    """
    key = _stripe_key()
    # P75: deliberately NOT stripe_key_mode(). Every other bare-prefix check in
    # this codebase was a mode question wearing a prefix check's clothes and was
    # corrected to ask stripe_key_mode(); this one is the exception, and the
    # distinction is the one stripe_key_mode()'s own docstring draws — mode is
    # not capability. This calls /v1/test_helpers/, which a *restricted* test
    # key (rk_test_) may not be permitted to reach at all. Being in test mode
    # does not make a key able to mint tokens, so the narrow check stays and the
    # message says which kind of key, not which mode.
    if not key.startswith("sk_test_"):
        raise RuntimeError(
            "create_test_spt requires a full Stripe test secret key (sk_test_). "
            "A restricted test key (rk_test_) is still test mode but may lack "
            "permission for Stripe's test_helpers endpoints."
        )
    amount = f"{amount_cents / 100:.2f}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/test_helpers/shared_payment/granted_tokens",
            auth=(key, ""),
            headers={"Stripe-Version": STRIPE_API_VERSION},
            data={
                "payment_method": "pm_card_visa",
                "usage_limits[amount]": amount,
                "usage_limits[currency]": currency.lower(),
            },
        )
        resp.raise_for_status()
        token = resp.json()
        return {
            "id": token["id"],
            "currency": currency.lower(),
            "amount_cents": amount_cents,
        }


async def retrieve_spt(spt_id: str) -> dict[str, Any]:
    """Retrieve and validate a Stripe Shared Payment Token before charging.

    Per https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens:
      GET /v1/shared_payment/granted_tokens/{spt_id}

    Returns usage limits (max_amount, currency, expires_at) and deactivation
    status. Check this before confirm_spt() to validate the token is still live
    and covers the required amount.

    Note: SPTs carry NO agent/customer identity — only card brand + last-four +
    usage limits. Agent identity on Emporia is Ed25519-based, not Stripe-based.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{STRIPE_API_BASE}/shared_payment/granted_tokens/{spt_id}",
            auth=(key, ""),
            headers={"Stripe-Version": STRIPE_API_VERSION},
        )
        resp.raise_for_status()
        spt = resp.json()
        limits = spt.get("usage_limits", {})
        max_amt = limits.get("max_amount")
        return {
            "spt_id": spt_id,
            "active": spt.get("deactivated_at") is None,
            "deactivated_reason": spt.get("deactivated_reason"),
            "currency": limits.get("currency", "usd"),
            "max_amount_cents": int(max_amt * 100) if isinstance(max_amt, (int, float)) else 0,
            "expires_at": limits.get("expires_at"),
        }


async def confirm_spt(
    spt_token: str,
    amount_cents: int,
    session_id: str,
    agent_id: str,
    service_type: str = "emporia:session",
    *,
    currency: str = "usd",
    capture_method: str = "manual",
    resource_type: str = "session",
) -> dict[str, Any]:
    """Create and confirm a PaymentIntent from a Stripe SPT (spt_xxx).

    Uses the correct field per Stripe docs:
      payment_method_data[shared_payment_granted_token] = spt_xxx

    Stripe clones a PaymentMethod from the grant; subsequent refunds/reporting
    behave as if the PaymentMethod was provided directly.

    Before charging, the relay verifies that the SPT is still active and that
    its usage limits cover this amount/currency.
    """
    if not spt_token.startswith("spt_"):
        raise ValueError("Expected a Stripe shared payment token (spt_xxx)")

    spt = await retrieve_spt(spt_token)
    if not spt.get("active"):
        raise ValueError(
            f"SPT is inactive: {spt.get('deactivated_reason') or 'deactivated'}"
        )
    spt_currency = (spt.get("currency") or currency).lower()
    if spt_currency != currency.lower():
        raise ValueError(
            f"SPT currency mismatch: expected {currency.lower()}, got {spt_currency}"
        )
    max_amount_cents = int(spt.get("max_amount_cents") or 0)
    if max_amount_cents and max_amount_cents < amount_cents:
        raise ValueError(
            f"SPT max_amount too small: need {amount_cents}, got {max_amount_cents}"
        )

    key = _stripe_key()
    metadata = {
        "metadata[session_id]": session_id,
        "metadata[buyer_agent_id]": agent_id,
        "metadata[service_type]": service_type,
        "metadata[protocol]": "emporia:v1+spt",
        "metadata[resource_type]": resource_type,
        "metadata[resource_id]": session_id,
    }
    if resource_type == "room":
        metadata["metadata[room_id]"] = session_id
    elif resource_type == "agora":
        metadata["metadata[topic_id]"] = session_id

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents",
            auth=(key, ""),
            headers={"Stripe-Version": STRIPE_API_VERSION},
            data={
                "amount": str(amount_cents),
                "currency": currency.lower(),
                "confirm": "true",
                "capture_method": capture_method,
                "payment_method_data[shared_payment_granted_token]": spt_token,
                **metadata,
            },
        )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": pi.get("status"),
            "payment_intent_id": pi["id"],
            "receipt": pi.get("latest_charge") or pi.get("id"),
            "via": "spt",
        }


def stripe_key_mode() -> str:
    """P26-4: which Stripe key space this relay is configured for —
    "test" | "live" | "" (unconfigured/unrecognized). Lives here rather than
    in relay_server.py so `settle()` can apply the same rule without importing
    the server module (payments.py must stay server-import-free, same
    constraint entitlements.py has)."""
    sk = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    # P62: `rk_` is Stripe's *restricted* key prefix — same test/live segment,
    # fewer permissions. Only `sk_` was recognised, so a restricted key returned
    # "" and every protection keyed on mode switched itself off: the live-mode
    # webhook-secret refusal never fired, and P26-4's account-mode mismatch
    # check could never fire either, because it only acts when both modes are
    # known. The symptom was on `/health` the whole time as `stripe_mode: ""`.
    #
    # Mode is not capability, and this function only answers mode. The branches
    # that need a *full* test secret key — auto-confirming a PaymentIntent,
    # minting an SPT through `test_helpers` — stay gated on `sk_test_`
    # separately, so the relay never attempts a call a restricted key cannot
    # make and then reports the 403 as a payment failure.
    if sk.startswith(("sk_test_", "rk_test_")):
        return "test"
    if sk.startswith(("sk_live_", "rk_live_")):
        return "live"
    return ""


def stripe_account_mode_mismatch(account_mode: str) -> bool:
    """True only when both the account's mode and the configured key's mode are
    known AND they disagree — a transfer that is guaranteed to fail, since a
    test-mode `acct_...` simply does not exist in the live key space (and vice
    versa). Unknown on either side is deliberately NOT a mismatch: blocking on
    absence would break every payout on a relay that hasn't been migrated yet,
    which is the failure P26-1 exists to prevent, not repeat."""
    key_mode = stripe_key_mode()
    return bool(account_mode) and bool(key_mode) and account_mode != key_mode


def _stripe_key() -> str:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not set. "
            "Export it before running paid sessions. "
            "Use payment_mode='free' for free play."
        )
    return key


async def create_stake_intent(
    session_id: str,
    amount_cents: int,
    buyer_id: str,
    seller_id: str,
    service_type: str,
    mode: str = "stripe_pi",
    *,
    capture_method: str = "manual",
    currency: str = "usd",
    resource_type: str = "session",
) -> dict[str, Any]:
    """Create a Stripe PaymentIntent for a session stake using manual capture (escrow).

    capture_method=manual: funds are authorized (held) at join time but NOT captured
    until the session completes and a winner is determined. If the session never
    finishes, the hold expires in 5–7 days with automatic zero-platform refund.

    mode controls the accepted payment method:
      "stripe_pi"  — standard card (pm_card_visa in test, real card in live)
      "stripe_spt" — Shared Payment Token from Stripe Link / link-cli
      "mpp"        — same as stripe_spt for now; full MPP wiring is post-v1

    Capture happens in settle() when winner is confirmed.
    """
    key = _stripe_key()
    if mode in ("stripe_spt", "mpp"):
        pm_types = "link"
    else:
        pm_types = "card"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        metadata = {
            "metadata[session_id]": session_id,
            "metadata[buyer_agent_id]": buyer_id,
            "metadata[seller_agent_id]": seller_id,
            "metadata[service_type]": service_type,
            "metadata[mode]": mode,
            "metadata[protocol]": "emporia:v1+escrow",
            "metadata[resource_type]": resource_type,
            "metadata[resource_id]": session_id,
        }
        if resource_type == "room":
            metadata["metadata[room_id]"] = session_id
        elif resource_type == "agora":
            metadata["metadata[topic_id]"] = session_id
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents",
            auth=(key, ""),
            data={
                "amount": str(amount_cents),
                "currency": currency.lower(),
                "payment_method_types[]": pm_types,
                "capture_method": capture_method,
                **metadata,
            },
        )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": "created",
            "payment_intent_id": pi["id"],
            "client_secret": pi["client_secret"],
            "amount_cents": amount_cents,
            "capture_method": "manual",
            "mode": mode,
        }


async def verify_payment_intent(payment_intent_id: str) -> dict[str, Any]:
    """Fetch a PaymentIntent from Stripe and return its status.

    The relay calls this to verify the agent has already paid — relay does NOT
    create or confirm the payment, it only checks it. The agent is the payer;
    the relay is the merchant verifying receipt.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}",
            auth=(key, ""),
        )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": pi.get("status"),
            "payment_intent_id": payment_intent_id,
            "amount": pi.get("amount"),
            "currency": pi.get("currency"),
            "capture_method": pi.get("capture_method"),
            "metadata": pi.get("metadata") or {},
            "receipt": pi.get("latest_charge") or pi.get("id"),
        }


# Stripe's own always-succeeds test payment method. The default for a
# server-side confirm, so no card details exist anywhere in this repo; a caller
# with a real one (Stripe Link, or a stored method) passes it explicitly.
STRIPE_TEST_PAYMENT_METHOD = "pm_card_visa"


async def confirm_stripe_intent(
    payment_intent_id: str,
    payment_method_id: str | None = None,
) -> dict[str, Any]:
    """Confirm a Stripe PaymentIntent server-side.

    One endpoint, both modes. Test mode is not a different route at Stripe — it
    is this route under an `sk_test_` key, with a test payment method. The
    default `pm_card_visa` is Stripe's always-succeeds token; production callers
    pass their own `pm_...`.

    P46-1: this used to branch on the key prefix and POST test-mode confirms to
    `/v1/test_helpers/payment_intents/{id}/confirm`, which **Stripe does not
    serve** — probed directly during P46, it answers "Unrecognized request URL",
    the same as a path invented on the spot, while genuine `test_helpers` routes
    answer "You did not provide an API key". So every test-mode auto-confirm
    404'd, `raise_for_status()` raised, and both call sites surfaced it as a
    payment failure: a paid join whose PaymentIntent wasn't yet confirmed
    (`_verify_and_confirm_payment()`, relay_server.py) and
    `retry_with_stripe_auth()`. The function had no test of any kind — every
    x402 and SPT path in the suite is covered, this one was never called — and a
    mocked `httpx.AsyncClient.post` would have accepted any URL at all, which is
    why the defect needed a real API to find rather than more mocks.

    For autonomous agent payments in production, agents supply a payment method via
    Stripe Link (link-cli --test for sandbox, real Link account for production) or
    a stored payment method token. MPP is the correct long-term protocol.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/confirm",
            auth=(key, ""),
            data={
                "payment_method": payment_method_id or STRIPE_TEST_PAYMENT_METHOD,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": pi.get("status"),
            "payment_intent_id": payment_intent_id,
            "receipt": pi.get("latest_charge") or pi.get("id"),
        }


async def capture_payment_intent(payment_intent_id: str) -> dict[str, Any]:
    """Capture a manually-authorized PaymentIntent (escrow → platform balance).

    Called at session end to convert authorized holds into captured funds.
    The relay must capture before it can transfer winnings via Connect.
    Safe to call on already-captured PIs (Stripe is idempotent on capture).

    P1r-2 (R1) checked this call for the same missing-Idempotency-Key gap
    P1a-3/P1r-2 fixed on the transfer/refund calls, and found it doesn't apply:
    a capture acts on a specific, already-existing PaymentIntent resource (the
    `payment_intent_unexpected_state` branch above already treats a re-capture
    as a no-op success) rather than minting a new resource on every call the way
    a Transfer or Refund does — there's nothing for a second call to duplicate.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/capture",
            auth=(key, ""),
        )
        if resp.status_code == 400:
            data = resp.json()
            code = data.get("error", {}).get("code", "")
            if code in ("payment_intent_unexpected_state",):
                # Already captured or succeeded — treat as success
                return {"payment_intent_id": payment_intent_id, "status": "already_captured"}
        resp.raise_for_status()
        pi = resp.json()
        return {"payment_intent_id": payment_intent_id, "status": pi.get("status")}


async def cancel_payment_hold(payment_intent_id: str) -> dict[str, Any]:
    """Cancel an authorized hold, releasing funds back to the agent.

    Used when a session is abandoned (both agents disconnect, timeout) so neither
    agent is charged. Manual-capture PIs auto-expire in 5–7 days, but explicit
    cancel is faster and provides a clear paper trail.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/cancel",
            auth=(key, ""),
        )
        if resp.status_code == 400:
            data = resp.json()
            code = data.get("error", {}).get("code", "")
            if code in ("payment_intent_unexpected_state",):
                return {"payment_intent_id": payment_intent_id, "status": "already_finalized"}
        resp.raise_for_status()
        pi = resp.json()
        return {"payment_intent_id": payment_intent_id, "status": pi.get("status")}


async def settle(
    session_id: str,
    winner_id: str,
    total_stake_cents: int,
    payment_intent_ids: list[str],
    winner_stripe_account: str | None = None,
    *,
    winner_stripe_account_status: str,
    winner_stripe_account_mode: str = "",
) -> dict[str, Any]:
    """Capture all escrowed stakes, then transfer the winner's net payout.

    Escrow flow:
    1. All agent PIs were created with capture_method=manual (holds, not charges).
    2. On winner determination, relay captures all PIs → funds land in platform balance.
    3. Relay transfers 97.5% to winner's connected account; platform keeps 2.5%.
    4. If game was never played / abandoned, call cancel_payment_hold() instead.

    Platform fee: OPERATOR_FEE_BPS basis points (default 250 = 2.5%), minimum 1¢.
    Requires total_stake_cents ≥ STRIPE_MIN_CHARGE_CENTS (50¢). P74: that was
    a claim about a constant this module did not define and nobody enforced —
    it is now real, checked where the PaymentIntent is created, before Stripe
    is asked.

    `winner_stripe_account_status` (P25-2) is **required and keyword-only**
    (P26-9). P25 gave it a `"active"` default so pre-existing callers kept
    their behavior — but that is fail-*open*, the opposite of
    `get_stripe_account_status()`'s own fail-closed `'none'`, and it meant a
    future caller that simply forgot the argument would silently get the
    unguarded path with no signal. There is exactly one production call site
    and it always passes the winner's real AGENT_REGISTRY status, so making it
    explicit costs nothing and removes the footgun entirely: forgetting it is
    now a TypeError at the call, not a wrong money decision at runtime.

    `winner_stripe_account_mode` (P26-4) is the Stripe key space the account
    belongs to ("test"/"live"/"" for unknown). A transfer is refused when it is
    known and disagrees with the configured key's own mode: the two `acct_...`
    namespaces are disjoint, so paying a test account under a live key is a
    guaranteed failure at best. "" (unknown) does not block — pre-P26 rows are
    backfilled to a real value by `_backfill_stripe_account_state()`, and a
    caller that genuinely doesn't know shouldn't have its previously-working
    payout broken by this check.
    """
    fee = platform_fee(total_stake_cents)
    platform_fee_cents = fee
    winner_payout_cents = total_stake_cents - platform_fee_cents

    # Step 1: Capture all authorized holds → platform balance
    capture_results = []
    for pi_id in payment_intent_ids:
        if pi_id:
            try:
                result = await capture_payment_intent(pi_id)
                capture_results.append(result)
            except Exception as e:
                capture_results.append({"payment_intent_id": pi_id, "status": f"capture_failed: {e}"})

    transfer_id: str | None = None
    transfer_status = "pending_connect"

    # Step 2: Transfer winner's net share from platform balance to their connected account
    if winner_stripe_account and winner_payout_cents > 0 and stripe_account_mode_mismatch(
        winner_stripe_account_mode
    ):
        transfer_status = "pending_connect_wrong_mode"
    elif winner_stripe_account and winner_payout_cents > 0 and winner_stripe_account_status != "active":
        transfer_status = "pending_connect_not_active"
    elif winner_stripe_account and winner_payout_cents > 0:
        try:
            transfer = await payout_winner(
                winner_id=winner_id,
                stripe_account_id=winner_stripe_account,
                amount_cents=winner_payout_cents,
                session_id=session_id,
                # Matches record_settlement()'s dedupe_key for this session's
                # aggregate row — same helper, same inputs (payer_id NULL, this
                # settlement's own PI list), by construction (P1r-2, R1). P1a-3's
                # `f"emporia:payout:{session_id}:"` carried no PI digest, so a
                # second genuine settle() for the same session (which shouldn't
                # happen, but nothing enforced it) would have collided with the
                # first's payout instead of being rejected on its own terms.
                idempotency_key="emporia:payout:" + settlement_dedupe_key(
                    session_id, None, payment_intent_ids
                ),
            )
            transfer_id = transfer["transfer_id"]
            transfer_status = "transferred"
        except Exception as e:
            transfer_status = f"transfer_failed: {e}"

    return {
        "session_id": session_id,
        "winner_id": winner_id,
        "total_stake_cents": total_stake_cents,
        "platform_fee_cents": platform_fee_cents,
        "winner_payout_cents": winner_payout_cents,
        "platform_fee_bps": OPERATOR_FEE_BPS,
        "payment_intent_ids": payment_intent_ids,
        "capture_results": capture_results,
        "transfer_id": transfer_id,
        "transfer_status": transfer_status,
        "status": "settled",
    }


async def create_connected_account(agent_id: str) -> dict[str, Any]:
    """Create a Stripe Custom connected account for an agent (test mode).

    In test mode Stripe skips verification — the account is immediately usable
    for receiving transfers. Stored per-agent so settlement can transfer the winner's
    share directly to their account rather than just recording a number.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/accounts",
            auth=(key, ""),
            data={
                "type": "custom",
                "country": "US",
                "email": f"{agent_id}@emporia.local",
                "capabilities[card_payments][requested]": "true",
                "capabilities[transfers][requested]": "true",
                "tos_acceptance[date]": str(int(__import__("time").time())),
                "tos_acceptance[ip]": "127.0.0.1",
                "metadata[agent_id]": agent_id,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        acct = resp.json()
        return {
            "stripe_account_id": acct["id"],
            "agent_id": agent_id,
            "status": "created",
        }


async def create_live_connected_account(
    agent_id: str, *, email: str, country: str = "US",
) -> dict[str, Any]:
    """Create a Stripe Custom connected account for an agent (live mode).

    A sibling of create_connected_account() above, not a variant of it —
    that function's `tos_acceptance[*]` auto-fill (date=now, ip=127.0.0.1) is
    correct only because Stripe auto-approves Custom accounts in test mode
    without ever looking at it. Fabricating ToS-acceptance evidence for a
    real account would be lying about a real legal event, so this omits
    `tos_acceptance` entirely — Stripe's own hosted onboarding
    (create_account_onboarding_link() below) collects the real acceptance,
    and the account starts unverified/pending until that flow completes and
    the account.updated webhook (relay_server.py) reports it active.

    P26-6: `email` is required, with no default. P25 sent
    `f"{agent_id}@emporia.local"` — `.local` is unroutable by design, so every
    message Stripe sends the account holder about a REAL money-handling
    account (verification requirements, payout failures, restrictions) went
    nowhere. `country` matters even more than it looks: Stripe treats it as
    **immutable after creation**, so getting it wrong means deleting the
    account and redoing KYC from scratch. Both are now the caller's explicit
    decision rather than a hardcoded guess.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/accounts",
            auth=(key, ""),
            data={
                "type": "custom",
                "country": country,
                "email": email,
                "capabilities[card_payments][requested]": "true",
                "capabilities[transfers][requested]": "true",
                "metadata[agent_id]": agent_id,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        acct = resp.json()
        return {
            "stripe_account_id": acct["id"],
            "agent_id": agent_id,
            "status": "created",
        }


async def create_account_onboarding_link(
    stripe_account_id: str, refresh_url: str, return_url: str,
) -> str:
    """Create a Stripe-hosted Account Link (`type=account_onboarding`) for a
    live-mode Connected Account created by create_live_connected_account()
    above, and return its URL. `refresh_url` is where Stripe sends the agent's
    operator back if the link itself expired (they should be sent through
    this function again to get a fresh one); `return_url` is where Stripe
    sends them after the onboarding *form* is complete — that is not the same
    as the account being payable, since capability activation happens
    asynchronously and is only ever confirmed by the account.updated webhook.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/account_links",
            auth=(key, ""),
            data={
                "account": stripe_account_id,
                "refresh_url": refresh_url,
                "return_url": return_url,
                "type": "account_onboarding",
            },
        )
        resp.raise_for_status()
        return resp.json()["url"]


TRANSFER_SEARCH_PAGE_LIMIT = int(
    os.getenv("EMPORIA_TRANSFER_SEARCH_PAGES", "20") or "20"
)


async def find_settlement_transfer(
    *, transfer_group: str, dedupe_key: str
) -> tuple[bool, str | None]:
    """Ask Stripe whether the payout for one settlement already landed (P51).

    Returns `(known, transfer_id)`. **`known=False` never means "no transfer"**
    — it means the question could not be answered, and a caller that rounds it
    down to "not paid" re-posts a transfer that may already have moved money.
    This is the same tri-state discipline `x402_authorization_used()` carries
    for the chain, for the same reason and against the same failure.

    `known=True, transfer_id=None` is the only answer that licenses a retry.

    Matching is on `metadata[dedupe_key]`, which `payout_winner()` stamps onto
    every transfer for exactly this lookup: `transfer_group` is the *resource*
    (a room can have many buyers, so many transfers share it), and Stripe's
    Search API does not cover transfers, so the list endpoint plus a
    client-side match on our own natural key is the available shape.

    Paging is bounded at `TRANSFER_SEARCH_PAGE_LIMIT`. Exhausting the bound
    without a match returns `known=False`, not "absent" — a search window is a
    bound, not an answer, which is the lesson P48 learned from Base's
    10,000-block `eth_getLogs` cap the expensive way.
    """
    key = _stripe_key()
    params: dict[str, Any] = {"transfer_group": transfer_group, "limit": 100}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for _ in range(TRANSFER_SEARCH_PAGE_LIMIT):
            try:
                resp = await client.get(
                    f"{STRIPE_API_BASE}/transfers", auth=(key, ""), params=params
                )
                resp.raise_for_status()
                page = resp.json()
            except Exception:
                return (False, None)
            data = page.get("data") or []
            for transfer in data:
                if (transfer.get("metadata") or {}).get("dedupe_key") == dedupe_key:
                    return (True, transfer.get("id"))
            if not page.get("has_more") or not data:
                return (True, None)
            params = {**params, "starting_after": data[-1]["id"]}
    # Ran out of pages with more still to read: unknown, not absent.
    return (False, None)


async def payout_winner(
    winner_id: str,
    stripe_account_id: str,
    amount_cents: int,
    session_id: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Transfer winner payout to their Stripe Connected Account.

    Platform keeps OPERATOR_FEE_BPS; this function transfers only the winner's
    net share. Requires the platform balance to have sufficient funds — in test mode
    the confirmed PaymentIntents fund the platform balance automatically.

    `idempotency_key` (P1a-3, R3) is required, not defaulted: a Stripe Transfer
    creates a *new* resource on every call with no state-machine check to fall back
    on (unlike capture_payment_intent(), which is safe against a retry because
    Stripe itself rejects capturing an already-captured PI) — a retry after a
    timeout or mid-flight crash previously moved money twice. Callers must derive a
    key deterministic from the logical payout, not random, so a genuine retry reuses
    it and a different payout can't collide — see each call site for its own
    derivation (settle() uses the session-level dedupe_key shape with no payer
    suffix; the room/Agora call sites in relay_server.py include the paying agent,
    matching record_settlement()'s own dedupe_key for that same settlement).
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/transfers",
            auth=(key, ""),
            headers={"Idempotency-Key": idempotency_key},
            data={
                "amount": str(amount_cents),
                "currency": "usd",
                "destination": stripe_account_id,
                "transfer_group": session_id,
                "description": f"Emporia session payout: {session_id}",
                "metadata[winner_id]": winner_id,
                "metadata[session_id]": session_id,
                "metadata[protocol]": "emporia:v1",
                # P51: the settlement's own natural key, stamped on the
                # transfer so reconciliation can ask Stripe "did this one
                # land?" later. Derived from `idempotency_key` rather than
                # taken as a second parameter, so the two cannot disagree:
                # a transfer findable under a key it wasn't posted under
                # would be worse than not being findable at all.
                "metadata[dedupe_key]": idempotency_key.removeprefix("emporia:payout:"),
            },
        )
        resp.raise_for_status()
        transfer = resp.json()
        return {
            "transfer_id": transfer["id"],
            "winner_id": winner_id,
            "stripe_account_id": stripe_account_id,
            "amount_cents": amount_cents,
            "status": "transferred",
        }


async def arbitrate_and_refund(
    session_id: str,
    charge_id: str,
    reason: str = "fraudulent",
) -> dict[str, Any]:
    """Issue a Stripe refund when cheat detection or signature verification fails.

    Idempotency-Key (P1a-3, R3) derived deterministically from session_id + charge_id
    — the same pair fully identifies "the refund for this charge" — so a retry
    reuses it rather than risking a second real refund. Unlike payout_winner(), no
    caller-supplied key is needed: every piece the key is built from is already this
    function's own parameter.

    P1r-2 (R1) re-checked this key against the same (resource, payer)-vs-
    (resource, payer, payment) gap found in the payout keys, and left it
    unchanged: `charge_id` is already per-payment (Stripe mints a distinct charge
    per PaymentIntent), so this key was never coarser than the thing it
    identifies, unlike `payout_winner()`'s pre-P1r-2 keys.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/refunds",
            auth=(key, ""),
            headers={"Idempotency-Key": f"emporia:refund:{session_id}:{charge_id}"},
            data={
                "charge": charge_id,
                "reason": reason,
                "metadata[session_id]": session_id,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        refund = resp.json()
        return {
            "session_id": session_id,
            "refund_id": refund["id"],
            "charge_id": charge_id,
            "status": refund.get("status"),
            "reason": reason,
        }


async def refund_payment_intent(
    payment_intent_id: str,
    *,
    reason: str = "requested_by_customer",
    idempotency_key: str,
) -> dict[str, Any]:
    """Refund a payment directly by its PaymentIntent id (P2-7, kick-with-refund).

    Distinct from `arbitrate_and_refund()`, which refunds by `charge_id` and
    derives its own key from `(session_id, charge_id)`: this one's caller
    (kicking a paying room/Agora member) has the settlement's
    `payment_intent_id` on hand, not a charge id, and Stripe's refund API
    accepts either identifier — no charge lookup needed. `idempotency_key` is
    required, not derived here, so the caller can use the same
    `settlement_dedupe_key()`-based key it uses for the matching
    `reverse_transfer()` call, keeping the two halves of one kick-refund
    identifiably paired.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/refunds",
            auth=(key, ""),
            headers={"Idempotency-Key": idempotency_key},
            data={
                "payment_intent": payment_intent_id,
                "reason": reason,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        refund = resp.json()
        return {
            "payment_intent_id": payment_intent_id,
            "refund_id": refund["id"],
            "status": refund.get("status"),
            "reason": reason,
        }


async def reverse_transfer(
    transfer_id: str,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Reverse a Stripe Connect Transfer (P2-7, kick-with-refund).

    A room/Agora entry fee moves in two hops: the payer's charge lands in the
    platform balance, then `payout_winner()` transfers the creator's net share
    (97.5%) out to their connected account. Refunding the original charge
    without first clawing back the transfer would leave the platform eating
    the creator's share out of its own pocket — refunding the payer doesn't
    undo money that already left for someone else. This reverses that
    transfer so the platform balance is whole again before the refund below
    draws the payer's money back out of it.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/transfers/{transfer_id}/reversals",
            auth=(key, ""),
            headers={"Idempotency-Key": idempotency_key},
            data={"metadata[protocol]": "emporia:v1"},
        )
        resp.raise_for_status()
        reversal = resp.json()
        return {
            "transfer_id": transfer_id,
            "reversal_id": reversal["id"],
            "status": reversal.get("status"),
        }


async def retry_with_stripe_auth(
    url: str,
    payload: dict[str, Any],
    payment_intent_id: str,
) -> dict[str, Any]:
    """Shared 402-retry utility: confirm intent, then retry request with auth header.

    Used by turn submission, lobby sync, peer discovery — one function, not duplicated.
    """
    confirmation = await confirm_stripe_intent(payment_intent_id)
    receipt = confirmation.get("receipt") or payment_intent_id
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {receipt}",
                "X-Payment": receipt,
            },
        )
        return {
            "ok": resp.is_success,
            "status_code": resp.status_code,
            "paid": True,
            "receipt": receipt,
            "response": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
        }
