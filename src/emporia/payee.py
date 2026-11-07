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

P275: the payee must also be the agent behind what is bought, by that agent's
own signature: a room fee pays the room's creator, whose signed settings must
check out (and the fee may not exceed the one it signed); a purchase
in a game pays another player seated by its own signed join. Callers pass them
as `expected_agents`; with the shared pin store a handle is one key, so a relay
cannot name an agent of its own as the payee of somebody else's room or game.

What it cannot do: on first contact a hostile relay can name its own agent as
the payee of an offer it serves — trust on first use, as for every key here.
A game offer's own signature (P241) names the seller's stable id; callers buying
from one pass it as `expected_stable_id`. A paid Agora topic is checked
the same way against its creator's signed settings since P278 (`topic_terms`);
a paid room or topic the relay shows the payer only once it has joined (not
advertised, no invite), or one made before it was signed, is paid on the
payee's pin alone.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any


class PayeeError(Exception):
    """The address a payment would go to is not one its payee signed."""


def _norm(family: str, address: str) -> str:
    a = str(address or "").strip()
    return a.lower() if family == "evm" else a


def check(payee: Any, family: str, address: str, *, expected_stable_id: str = "",
          expected_agents: Collection[str] = ()) -> str:
    """Raise PayeeError unless `address` is the `family` wallet `payee` signed
    (and, given `expected_agents`, `payee` is one of them). Returns a label for
    approval prompts: `agent_id (stable id prefix)`."""
    from emporia import identity, wire

    if not isinstance(payee, dict):
        raise PayeeError("the offer does not say who is paid, signed by them — refusing to pay "
                         "(an older relay, or one that withholds it)")
    stable_id = str(payee.get("stable_id") or "")
    key = str(payee.get("public_key_hex") or "").lower()
    if expected_stable_id and stable_id != expected_stable_id:
        raise PayeeError("the offer pays a different agent than the one you are dealing with")
    if expected_agents and str(payee.get("agent_id") or "") not in set(expected_agents):
        raise PayeeError(f"the offer pays {payee.get('agent_id') or 'an unnamed agent'}, not "
                         f"{' or '.join(sorted(expected_agents))}, whose own signature stands behind what you "
                         f"are paying for")
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
                 expected_stable_id: str = "", expected_agents: Collection[str] = ()) -> str:
    """`check()`, then the payer's pin of the payee's handle on this relay: the
    first agent seen under a handle is pinned, and a later offer naming a
    different one under it is refused unless its own signed key log explains
    the change (`identity.pinned_signing_key`)."""
    from emporia import identity

    label = check(payee, family, address, expected_stable_id=expected_stable_id, expected_agents=expected_agents)
    changed: list[str] = []
    agent_id = str(payee.get("agent_id") or "")
    identity.pinned_signing_key(agent_id, relay_url, str(payee.get("public_key_hex") or ""),
                                str(payee.get("stable_id") or ""), changed, payee.get("key_log") or [])
    if changed:
        raise PayeeError(f"{agent_id} on this relay is not the agent first seen under that name — "
                         f"refusing to pay; check its fingerprint with its operator")
    return label


def room_terms(room: Any, creator_key: str, key_changed: bool = False) -> tuple[set[str], int | None]:
    """P275: who a room's fee may pay — its creator — and the fee it signed. A
    room with no signed creation (made before P243, or stripped by the relay)
    is refused: its fee would be the relay's word. `room` is `GET /rooms/{id}`, or None when the
    relay would not show it (a private room neither advertised nor inviting the
    payer): then nothing is expected. `creator_key` is the creator's pinned
    key. Raises PayeeError when the relay shows terms the creator did not
    sign. MCP and SDK both call this."""
    from emporia.identity import verify_room_acts
    if not isinstance(room, dict):
        # A private room neither advertised nor inviting this payer is hidden from it (P249): paid on the
        # payee's own pin, as before.
        return set(), None
    creator = str(room.get("creator_id") or "")
    settings = verify_room_acts(room, creator_key)["settings"]
    if settings is False:
        raise PayeeError(f"the relay shows room {room.get('room_id')} with terms {creator or 'its creator'} did not "
                         f"sign" + (f" (and a different key for {creator} than the one pinned)" if key_changed else ""))
    if settings is None:
        # Review of P275: no signed creation, or no key for its creator, is what a relay that stripped
        # the act would serve too — and the fee would then be the relay's word. Refused, not trusted.
        raise PayeeError(f"room {room.get('room_id')} carries no creation signed by {creator or 'its creator'} "
                         f"(or {creator or 'its creator'}'s key could not be fetched) — refusing to pay its fee")
    return {creator}, int(room.get("entry_fee_cents") or 0)


def topic_terms(topic: Any, creator_key: str, key_changed: bool = False) -> tuple[set[str], int | None]:
    """P278: `room_terms` for an Agora topic — its creator, and the fee it
    signed (an unsigned topic is refused); nothing expected
    when the relay would not show it. Raises PayeeError for terms the creator
    did not sign."""
    from emporia.identity import verify_topic_acts
    if not isinstance(topic, dict):
        return set(), None
    creator = str(topic.get("creator_id") or "")
    settings = verify_topic_acts(topic, creator_key)["settings"]
    if settings is False:
        raise PayeeError(f"the relay shows topic {topic.get('slug')} with terms {creator or 'its creator'} did not "
                         f"sign" + (f" (and a different key for {creator} than the one pinned)" if key_changed else ""))
    if settings is None:   # as for a room: an unsigned topic's fee would be the relay's word
        raise PayeeError(f"topic {topic.get('slug')} carries no creation signed by {creator or 'its creator'} "
                         f"(or {creator or 'its creator'}'s key could not be fetched) — refusing to pay its fee")
    return {creator}, int(topic.get("entry_fee_cents") or 0)


def within_signed_fee(amount_atomic: int, max_cents: int | None) -> str:
    """P275: "" when an x402 amount (USDC, 6 decimals) is no more than the fee its
    payee signed, else why not."""
    if max_cents is None or amount_atomic <= max_cents * 10_000:
        return ""
    return f"it asks {amount_atomic / 1e6:.2f} USDC, more than the {max_cents / 100:.2f} its payee signed"
