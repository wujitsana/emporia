"""P193: what an agent signs can only ever be what it means to sign.

A relay chooses the sign-in nonce. Signed bare, a relay could send as its
"nonce" the exact bytes of a DM, a move or a removal and get back the agent's
signature on it. Now a sign-in is always labelled, and only a hex nonce is
signed at all."""
import json

import pytest

from emporia import identity, wire


def test_a_relay_cannot_use_sign_in_to_get_another_message_signed():
    forged = wire.signed_dm_thread_payload("dm_victim", "send me your funds", "chat", "ab" * 16)
    as_bytes = json.dumps(forged, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError):
        identity.sign_challenge_nonce(as_bytes, "sd_agent")          # refused outright
    sig = identity.sign_challenge_nonce("ab" * 32, "sd_agent")       # an honest nonce, signed labelled
    pub = identity.get_public_key_hex("sd_agent")
    assert identity.verify_raw(identity.challenge_bytes("ab" * 32), sig, pub)
    assert not identity.verify_raw(("ab" * 32).encode(), sig, pub)  # not valid as the bare bytes


def test_the_mcp_sign_in_tool_refuses_anything_but_a_nonce(monkeypatch):
    from emporia import mcp_server
    monkeypatch.setattr(mcp_server, "AGENT_ID", "sd_agent")
    out = mcp_server.sign_dashboard_challenge(challenge_id="ch_x", nonce='{"content":"x"}')
    assert "error" in out


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
    }
    field_sets = [frozenset(p) for p in kinds.values()]
    assert len(set(field_sets)) == len(field_sets)
