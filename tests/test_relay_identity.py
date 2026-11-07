"""P286: a relay's id comes from its key and holds still — across restarts and key rotations."""
from emporia import identity
from emporia import relay_server as rs


def test_the_id_is_taken_from_the_key_and_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(identity, "_RELAY_KEYPAIR_CACHE", None)
    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)
    monkeypatch.delenv("EMPORIA_RELAY_ID", raising=False)
    first = rs.relay_id()
    assert first == identity.relay_id_from_key(identity.get_relay_public_key_hex())
    assert len(first) == 16 and first.isalnum() and first == first.lower()
    assert (tmp_path / "_relay_id.txt").read_text() == first

    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)            # a restart
    assert rs.relay_id() == first
    identity.rotate_relay_key()                                        # a new key, the same relay
    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)
    assert identity.get_relay_public_key_hex() and rs.relay_id() == first


def test_an_override_still_wins(monkeypatch):
    monkeypatch.setenv("EMPORIA_RELAY_ID", "linked-name")
    assert rs.relay_id() == "linked-name"


def test_a_token_survives_a_restart(monkeypatch):
    """The id a sign-in token is bound to no longer changes when the relay process does."""
    token = rs._jwt_encode({"agent_id": "someone", "relay_id": rs.relay_id(), "iat": 0, "issued": 0, "exp": 2**40})
    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)            # a new process reads the kept id
    assert rs._jwt_decode(token)["relay_id"] == rs.relay_id()


def test_the_relay_card_is_signed_and_checkable_by_anyone():
    from fastapi.testclient import TestClient
    card = TestClient(rs.app).get("/relay/card").json()
    assert card["relay_id"] == rs.relay_id() and card["relay_public_key"] == identity.get_relay_public_key_hex()
    import os
    assert identity.verify_relay_card(card) == {"ok": True, "id_from_key": not os.getenv("EMPORIA_RELAY_ID")}
    forged = {**card, "fee_bps": 1}                            # a directory that edits it
    assert identity.verify_relay_card(forged)["ok"] is False
    other = {**card, "relay_id": "aaaaaaaaaaaaaaaa"}                     # an id not from its key
    assert identity.verify_relay_card(other) == {"ok": False, "id_from_key": False}


def test_a_relay_that_rotated_before_takes_its_id_from_its_first_key(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(identity, "_RELAY_KEYPAIR_CACHE", None)
    monkeypatch.setattr(identity, "_RELAY_ID_CACHE", None)
    monkeypatch.delenv("EMPORIA_RELAY_ID", raising=False)
    first_key = identity.get_relay_public_key_hex()
    identity.rotate_relay_key()                                        # rotated, and no id kept yet
    monkeypatch.setattr(identity, "_RELAY_KEYPAIR_CACHE", None)
    assert identity.get_relay_public_key_hex() != first_key
    assert rs.relay_id() == identity.relay_id_from_key(first_key)
