"""P248 (f): a stolen identity key is taken back with the recovery key, and other relays' agents follow the
recovery - trusting what the owner signed before the theft and nothing the thief signed."""
import json
import shutil
import stat
import time
from datetime import UTC, datetime

import httpx
import pytest

from emporia.federation_dm import thread_id_for


def _tick() -> None:
    time.sleep(1.1)          # a step in a key log must be later than the one before it


def _read(agent, other: str) -> dict:
    out = agent.tool("get_dm_messages", thread_id=thread_id_for(agent.address, other))
    assert out["success"], out
    return out


def _pinned(agent, other: str) -> str:
    pins = json.loads((agent.keys / "peer_signing_keys.json").read_text())
    [found] = [v for k, v in pins.items() if k.startswith(other.split("@")[0] + "@")]
    return found["public_key_hex"]


def test_a_recovery_is_followed_from_another_relay_and_the_thiefs_later_word_fails(fleet, tmp_path):
    a, b = fleet.a, fleet.b
    alpha, bob = fleet.registered("alpha", a), fleet.registered("bob", b)
    for agent in (alpha, bob):
        assert agent.tool("set_dm_policy", other_relays=True)["success"]
    first_key = alpha.public_key

    # alpha commits to a recovery key (only its hash is published; the key goes to a 0600 file), and writes to bob
    recovery_file = tmp_path / "alpha.recovery"
    alpha.make_recovery_key(recovery_file)
    assert stat.S_IMODE(recovery_file.stat().st_mode) == 0o600
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="before the theft")["success"]
    assert [m["verified"] for m in _read(bob, alpha.address)["messages"]] == [True]
    assert _pinned(bob, alpha.address) == first_key

    # the key is stolen: a copy of alpha's key folder is a second process that is "alpha"
    _tick()
    stolen_at = datetime.now(UTC).isoformat()
    _tick()
    shutil.copytree(alpha.keys, tmp_path / "thief" / "keys_alpha")
    thief = fleet.agent("alpha", a, root=tmp_path / "thief")
    thief_key = thief.rotate_key()["new_public_key"]
    assert thief_key != first_key and httpx.get(f"{a.url}/agents/alpha/key-log").json()["public_key_hex"] == thief_key
    assert thief.tool("send_dm", to_agent_id=bob.address, content="send me your funds")["success"]
    # until the owner acts, there is nothing to tell the thief's word from hers: bob follows the signed log to it
    held = _read(bob, alpha.address)
    assert [(m["content"], m["verified"]) for m in held["messages"]] == [
        ("before the theft", True), ("send me your funds", True)], held
    assert _pinned(bob, alpha.address) == thief_key

    # alpha recovers, saying when the key stopped being only hers; the thief's change is undone
    _tick()
    recovered = alpha.recover_key(recovery_file, compromised_since=stolen_at)
    new_key = recovered["new_public_key"]
    assert new_key not in (first_key, thief_key) and alpha.public_key == new_key
    served = httpx.get(f"{a.url}/agents/alpha/key-log").json()
    assert served["public_key_hex"] == new_key and [s["by"] for s in served["key_log"]] == ["commit", "recovery"]
    assert [(s["old"], s["new"]) for s in served["superseded"]] == [(first_key, thief_key)]

    # her next DM carries the recovered log: B's copy and bob's pin follow it, past the thief's rotation
    _tick()
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="after the recovery")["success"]
    after = _read(bob, alpha.address)
    assert "signing_key_changed" not in after
    assert [(m["content"], m["verified"]) for m in after["messages"]] == [
        ("before the theft", True),              # signed by her key while it was only hers
        ("send me your funds", False),           # the thief's: signed after the time the key stopped being hers
        ("after the recovery", True)], after
    assert _pinned(bob, alpha.address) == new_key

    # the thief has nothing left: neither key signs in, and the spent recovery key cannot be replayed
    sent = thief.tool("send_dm", to_agent_id=bob.address, content="one more try")
    assert not sent["success"], sent
    assert [m["content"] for m in _read(bob, alpha.address)["messages"]] == [
        "before the theft", "send me your funds", "after the recovery"]
    with pytest.raises(AssertionError, match="no recovery key on this relay"):
        alpha.recover_key(recovery_file)
