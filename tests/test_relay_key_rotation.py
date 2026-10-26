"""P191: a relay rotates its key; whoever pinned the old one follows the signed
hand-over — and a bare change is still refused."""
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity


@pytest.fixture
def relay_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(identity, "_RELAY_KEYPAIR_CACHE", None)
    yield tmp_path
    identity._RELAY_KEYPAIR_CACHE = None


def test_a_rotation_is_a_signed_hand_over_and_the_old_private_key_goes(relay_keys):
    first = identity.get_relay_public_key_hex()
    step = identity.rotate_relay_key()
    second = identity.get_relay_public_key_hex()
    assert step["old"] == first and step["new"] == second != first
    assert identity.follows_rotation(first, second, identity.relay_key_history()) == [first, second]
    third = identity.rotate_relay_key()["new"]
    assert identity.follows_rotation(first, third, identity.relay_key_history()) == [first, second, third]
    assert len(list(relay_keys.glob("_relay_identity.priv*"))) == 1


def test_a_hand_over_not_signed_by_the_pinned_key_is_not_followed(relay_keys):
    pinned = identity.get_relay_public_key_hex()
    impostor = ed25519.Ed25519PrivateKey.generate()
    attacker = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    forged = {"old": pinned, "new": attacker, "at": "2026-10-26T00:00:00+00:00"}
    forged["signature"] = impostor.sign(identity.rotation_bytes(forged["old"], attacker, forged["at"])).hex()
    assert identity.follows_rotation(pinned, attacker, [forged]) == []


def test_an_agent_follows_its_relays_rotation_and_keeps_trusting_old_receipts(relay_keys, monkeypatch, tmp_path):
    from emporia import mcp_server
    first = identity.get_relay_public_key_hex()
    pins = tmp_path / "agent"
    pins.mkdir()
    monkeypatch.setattr(identity, "KEY_DIR", pins)            # the agent's own keys folder, now
    assert mcp_server._trusted_relay_keys("http://r.test", first, []) == ([first], False)
    monkeypatch.setattr(identity, "KEY_DIR", relay_keys)
    step = identity.rotate_relay_key()
    history = identity.relay_key_history()
    monkeypatch.setattr(identity, "KEY_DIR", pins)
    keys, changed = mcp_server._trusted_relay_keys("http://r.test", step["new"], history)
    assert keys == [first, step["new"]] and not changed
    assert json.loads((pins / "relay_pins.json").read_text())["http://r.test"] == step["new"]
    # once re-pinned, the chain still reaches back to the first key, for old receipts
    assert mcp_server._trusted_relay_keys("http://r.test", step["new"], history)[0] == [first, step["new"]]
    stranger = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    assert mcp_server._trusted_relay_keys("http://r.test", stranger, history)[1] is True
