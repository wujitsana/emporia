"""The x402 payer — the half of the rail this repo never had (P45).

P1b built the seller: `payments.build_x402_challenge()` issues the 402,
`relay_server._verify_x402_payment()` asserts resource binding and calls a
facilitator's /verify + /settle. Nothing in this repo could produce the
credential that satisfies any of it. `mcp_server.py`'s fork note records why —
EIP-191 signing, the retry loop and every `X402_*` var were stripped out of the
PTGS fork deliberately — and `pyproject.toml` carried no signing dependency, so
all 320 x402 references in the test suite were seller-side with a mocked
facilitator. This module is the missing half.

Scope: one job, `exact` scheme on EVM. Given a PaymentRequired challenge and a
private key, produce the base64 `PAYMENT-SIGNATURE` header value that satisfies
it. That means an EIP-712 signature over an EIP-3009
`transferWithAuthorization` struct, which is what the `exact` mechanism
authorizes payment with (x402 v2 spec §5.2.2). No RPC, no chain reads, no
balance checks: everything needed is in the challenge, which is precisely why
P45 had to make the relay *send* `extra` (the token's EIP-712 domain) before a
payer was possible at all.

Deliberately not the official `x402` package, for the reason `docs/payments.md`
already records about its ASGI middleware, and the same hand-rolled-over-httpx
posture this repo takes toward Stripe. It does depend on `eth-account` for the
EIP-712 encoding — that is 19 packages of transitive weight, so it lives in the
optional `x402` extra and the relay image installs none of it. Importing this
module without that extra raises a message that says so.

Nothing here moves money by itself: signing an authorization is not settlement.
The facilitator settles, and only after the resource server has asserted the
credential matches what the resource actually charges.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

from emporia.payments import x402_network_family

try:
    from eth_account import Account
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the import test
    raise ModuleNotFoundError(
        "emporia.x402_payer needs EIP-712 signing, which ships in the optional "
        "'x402' extra: `uv sync --extra x402` (or `pip install 'emporia[x402]'`). "
        "The relay itself never signs, so its image deliberately omits it."
    ) from exc


class X402PayerError(Exception):
    """No requirement in this challenge is payable by this module.

    Distinct from a signing failure: it means the offer and the payer could not
    meet, which a caller may reasonably respond to by trying another rail.
    """


# EIP-3009's struct, exactly as the `exact` EVM mechanism signs it. The field
# order is part of the EIP-712 type hash — reordering these silently produces a
# signature the token contract will not honour, so this is written once and
# never rebuilt per call.
_TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

# An authorization is backdated slightly so a payer whose clock runs a little
# fast doesn't sign something the settling node considers not-yet-valid. 60s is
# the same order as the facilitator's own tolerance and far below any sane
# `maxTimeoutSeconds`.
_VALID_AFTER_SKEW_SECONDS = 60


def payer_address(private_key: str) -> str:
    """The checksummed EVM address this key pays from — the `from` field of
    every authorization it signs, and the address that must actually hold the
    token. Exposed so a caller can fund or check a payer without importing
    eth_account itself."""
    return Account.from_key(private_key).address


def payment_required_from(body: dict[str, Any]) -> dict[str, Any]:
    """Find the PaymentRequired object in a relay 402 body.

    Emporia's 402 is a joint MPP/x402 response: the x402 object is nested under
    an `"x402"` key rather than flattened, because the MPP body already has a
    `resource` key of a different type (`payments.py`'s x402 section docstring
    explains the collision). A caller that decoded the PAYMENT-REQUIRED header
    instead already holds the object itself, so both shapes are accepted —
    otherwise every call site would have to know which door it came in by.
    """
    if "accepts" in body:
        return body
    nested = body.get("x402")
    if isinstance(nested, dict) and "accepts" in nested:
        return nested
    raise X402PayerError(
        "no x402 PaymentRequired object in this response — the relay offered no "
        f"x402 requirement (keys: {sorted(body)})"
    )


def select_requirement(
    challenge: dict[str, Any], *, networks: list[str] | None = None,
) -> dict[str, Any]:
    """Pick the `accepts` entry this module can actually pay.

    Filters, in order: the `exact` scheme, an EVM network, a present EIP-712
    domain (`extra.name` + `extra.version`), and — when `networks` is given —
    membership in the caller's allowlist. That allowlist is the payer's own
    policy, not the relay's: an agent that is funded only on Base Sepolia
    should not sign a mainnet authorization because a relay offered one, and
    the default of None (pay on any offered EVM network) is the permissive
    case a caller opts into by omitting it.

    Raises rather than returning None. Every caller here treats "nothing
    payable" as an error path, and a None would have to be re-checked at each.
    """
    accepts = challenge.get("accepts") or []
    if not isinstance(accepts, list):
        raise X402PayerError("challenge `accepts` is not a list")
    reasons: list[str] = []
    for entry in accepts:
        if not isinstance(entry, dict):
            continue
        network = entry.get("network") or ""
        if entry.get("scheme") != "exact":
            reasons.append(f"{network}: scheme {entry.get('scheme')!r} is not 'exact'")
            continue
        if x402_network_family(network) != "evm":
            reasons.append(f"{network}: not an EVM network")
            continue
        if networks is not None and network not in networks:
            reasons.append(f"{network}: not in this payer's allowed networks")
            continue
        extra = entry.get("extra")
        if not isinstance(extra, dict) or not extra.get("name") or not extra.get("version"):
            # F4 from the payer's side: this is the state every Emporia
            # challenge was in before P45 — well-formed and unsignable.
            reasons.append(f"{network}: no EIP-712 domain in `extra` (name/version)")
            continue
        return entry
    raise X402PayerError(
        "no payable requirement in this challenge: " + ("; ".join(reasons) or "`accepts` was empty")
    )


def build_payment_payload(
    requirement: dict[str, Any],
    private_key: str,
    *,
    resource: dict[str, Any] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Sign one requirement into an x402 v2 PaymentPayload.

    `requirement` is echoed into `accepted` verbatim rather than rebuilt from
    its parts. The resource server compares its own computed requirement
    against this field, so anything this module normalised, reordered or
    dropped on the way through would read as tampering — and the point of the
    binding assertion is that it cannot tell an honest reconstruction from a
    dishonest one, nor should it have to.

    The authorization's validity window comes from the requirement's own
    `maxTimeoutSeconds` (the resource server's stated deadline), backdated by
    `_VALID_AFTER_SKEW_SECONDS` against clock skew. `nonce` is 32 fresh random
    bytes per call — EIP-3009 nonces are single-use at the token contract, so
    reusing one across two payments makes the second unsettleable.
    """
    network = requirement.get("network") or ""
    try:
        chain_id = int(network.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise X402PayerError(f"cannot read a chain id out of network {network!r}") from exc

    extra = requirement.get("extra") or {}
    asset = requirement.get("asset")
    pay_to = requirement.get("payTo")
    amount = requirement.get("amount")
    if not asset or not pay_to or amount is None:
        raise X402PayerError(
            "requirement is missing asset/payTo/amount — nothing to authorize"
        )

    account = Account.from_key(private_key)
    issued = int(time.time()) if now is None else int(now)
    valid_after = issued - _VALID_AFTER_SKEW_SECONDS
    valid_before = issued + int(requirement.get("maxTimeoutSeconds") or 0)
    if valid_before <= issued:
        raise X402PayerError(
            f"requirement's maxTimeoutSeconds ({requirement.get('maxTimeoutSeconds')!r}) "
            "leaves no window in which the authorization would be valid"
        )
    nonce = secrets.token_bytes(32)

    signed = Account.sign_typed_data(
        private_key,
        domain_data={
            # The domain is what binds this signature to one token contract on
            # one chain. `name`/`version` are the contract's own EIP-712 domain
            # values and are readable only from the chain — which is why they
            # have to arrive in the challenge.
            "name": extra["name"],
            "version": extra["version"],
            "chainId": chain_id,
            "verifyingContract": asset,
        },
        message_types=_TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data={
            "from": account.address,
            "to": pay_to,
            "value": int(amount),
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce,
        },
    )

    payload: dict[str, Any] = {
        "x402Version": 2,
        "accepted": requirement,
        "payload": {
            "signature": signed.signature.to_0x_hex(),
            # Strings, not ints: the spec types every Authorization field as a
            # string, and a facilitator that parses strictly will reject an
            # int-typed `value` even though the signature over it is correct.
            "authorization": {
                "from": account.address,
                "to": pay_to,
                "value": str(amount),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": "0x" + nonce.hex(),
            },
        },
    }
    if resource:
        payload["resource"] = resource
    return payload


def payment_signature_header(payload: dict[str, Any]) -> str:
    """The PAYMENT-SIGNATURE header value: base64url JSON, unpadded.

    The exact inverse of `payments.extract_x402_payload()`, which re-pads
    before decoding — pinned as a round trip in the tests rather than trusted,
    because the two live in different modules and a padding disagreement would
    look like a malformed credential rather than an encoding bug.
    """
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


def sign_challenge(
    body: dict[str, Any],
    private_key: str,
    *,
    networks: list[str] | None = None,
) -> str:
    """402 body in, PAYMENT-SIGNATURE header value out — the whole retry step.

    The three functions above are separately callable because a payer that
    wants to inspect what it is about to spend needs to stop between selecting
    and signing. Callers that don't, use this.
    """
    challenge = payment_required_from(body)
    requirement = select_requirement(challenge, networks=networks)
    return payment_signature_header(
        build_payment_payload(requirement, private_key, resource=challenge.get("resource"))
    )
