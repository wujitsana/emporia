"""P262: who a payment goes to, checked by the payer — not taken from the relay.

A relay builds every payment offer: the x402 `payTo`, the Tempo `recipient`,
the escrow hold's `receiver`. Before P262 the address came from the payee's
row on the relay, set over a bearer-token request with no signature of the
payee's, so the relay (or anything holding a token for the payee) could put
its own wallet there and be paid instead of the payee.

Now an agent signs each payout wallet it registers
(`wire.signed_payout_wallet_payload`), and the relay serves that statement as
the offer's `payee`:

    {"agent_id", "stable_id", "public_key_hex", "key_log",
     "wallets": {family: {"address", "at", "signature"}}}

`check()` is what a payer runs before signing: the address it is asked to pay
must be one the payee signed, with a key that hashes to (or a key log leads
from) the payee's stable id. That much needs no state, and the signer runs it
too. `check_pinned()` adds the client's pin (P244): the payee's handle on this
relay must still be the agent first seen under it, so a relay cannot swap in
an agent of its own under a known name.

What it cannot do: on first contact a hostile relay can name its own agent as
the payee of a listing it serves — trust on first use, as for every key here.
A listing's own signature (P240) names the seller's stable id; callers buying
from a listing pass it as `expected_stable_id`.
"""

from __future__ import annotations

from typing import Any


class PayeeError(Exception):
    """The address a payment would go to is not one its payee signed."""


def _norm(family: str, address: str) -> str:
    a = str(address or "").strip()
    return a.lower() if family == "evm" else a


def check(payee: Any, family: str, address: str, *, expected_stable_id: str = "") -> str:
    """Raise PayeeError unless `address` is the `family` wallet `payee` signed.
    Returns a label for approval prompts: `agent_id (stable id prefix)`."""
    from emporia import identity, wire

    if not isinstance(payee, dict):
        raise PayeeError("the offer does not say who is paid, signed by them — refusing to pay "
                         "(an older relay, or one that withholds it)")
    stable_id = str(payee.get("stable_id") or "")
    key = str(payee.get("public_key_hex") or "").lower()
    if expected_stable_id and stable_id != expected_stable_id:
        raise PayeeError("the offer pays a different agent than the one you are dealing with")
    if why := wire.key_verdict(stable_id, key, payee.get("key_log") or [], identity.verify):
        raise PayeeError(f"the payee's key does not check out: {why}")
    wallet = ((payee.get("wallets") or {}).get(family)) or {}
    signed_address = str(wallet.get("address") or "")
    if not signed_address or _norm(family, signed_address) != _norm(family, address):
        raise PayeeError(f"the offer pays {address}, which is not the {family} wallet its payee signed")
    at = str(wallet.get("at") or "")
    # The key the payee held when it signed: a wallet registered before a key
    # rotation stays good; one signed with a key a recovery cut off does not.
    signer = wire.key_at(wire.key_log_windows(stable_id, payee.get("key_log") or [], identity.verify, key), at)
    if not signer:
        raise PayeeError("that wallet was signed at a time none of the payee's keys was good")
    statement = wire.signed_payout_wallet_payload(stable_id, family, signed_address, at)
    if not identity.verify(statement, str(wallet.get("signature") or ""), signer):
        raise PayeeError("the payee's signature on that wallet does not match")
    return f"{payee.get('agent_id') or '?'} ({stable_id[:12]})"


def check_pinned(payee: Any, family: str, address: str, relay_url: str, *,
                 expected_stable_id: str = "") -> str:
    """`check()`, then the payer's pin of the payee's handle on this relay: the
    first agent seen under a handle is pinned, and a later offer naming a
    different one under it is refused unless its own signed key log explains
    the change (`identity.pinned_signing_key`)."""
    from emporia import identity

    label = check(payee, family, address, expected_stable_id=expected_stable_id)
    changed: list[str] = []
    agent_id = str(payee.get("agent_id") or "")
    identity.pinned_signing_key(agent_id, relay_url, str(payee.get("public_key_hex") or ""),
                                str(payee.get("stable_id") or ""), changed, payee.get("key_log") or [])
    if changed:
        raise PayeeError(f"{agent_id} on this relay is not the agent first seen under that name — "
                         f"refusing to pay; check its fingerprint with its operator")
    return label
