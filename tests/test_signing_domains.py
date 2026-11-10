"""P193: what an agent signs can only ever be what it means to sign.

A relay chooses the sign-in nonce. Signed bare, a relay could send as its
"nonce" the exact bytes of a DM, a move or a removal and get back the agent's
signature on it. Now a sign-in is always labelled, and only a hex nonce is
signed at all."""
import json

import pytest

from emporia import identity, wire


def test_a_relay_cannot_use_sign_in_to_get_another_message_signed():
    from emporia import signing
    forged = wire.signed_dm_thread_payload("dm_victim", "send me your funds", "chat", "ab" * 16)
    as_bytes = json.dumps(forged, sort_keys=True, separators=(",", ":"))
    pub = identity.get_public_key_hex("sd_agent")
    with pytest.raises(signing.Refused):
        signing.sign_in("sd_agent", as_bytes, "https://relay.example", pub)      # refused outright
    sig = signing.sign_in("sd_agent", "ab" * 32, "https://relay.example", pub)   # an honest nonce, labelled
    assert identity.verify(wire.signed_registration_payload("ab" * 32, "https://relay.example", "sd_agent", pub),
                           sig, pub)
    # not valid for another relay, another name, or as a dashboard sign-in
    assert not identity.verify(wire.signed_registration_payload("ab" * 32, "https://other.example", "sd_agent", pub),
                               sig, pub)
    assert not identity.verify(wire.signed_registration_payload("ab" * 32, "https://relay.example", "someone", pub),
                               sig, pub)
    assert not identity.verify(wire.signed_dashboard_sign_in_payload("ab" * 32, "https://relay.example"), sig, pub)


def test_every_signed_payload_kind_has_its_own_field_set():
    """No signature over one kind of message can be read as another: the
    verifier rebuilds a specific set of fields, and no two kinds share one.
    New kinds also carry a `type` (docs/protocol.md)."""
    kinds = {
        "action": wire.signed_action_payload("a", "move", {}, "r", "s", 1),
        "room_message": wire.signed_room_message_payload("r", "c", "chat", "n"),
        "dm_message": wire.signed_dm_thread_payload("t", "c", "chat", "n"),
        "direct_message": wire.signed_dm_envelope("b", "chat", None, {}),
        "encryption_key": wire.signed_encryption_key_payload("a", "k"),
        "relay_list": wire.signed_relay_list_payload("a", "h", [], "", "t"),
        "delete": wire.signed_delete_payload("k", "i", "b", "t"),
        "dashboard_sign_in": wire.signed_dashboard_sign_in_payload("ab" * 16, "http://relay.test"),
        "registration": wire.signed_registration_payload("ab" * 16, "http://relay.test", "a", "k"),
    }
    field_sets = [frozenset(p) for p in kinds.values()]
    assert len(set(field_sets)) == len(field_sets)
