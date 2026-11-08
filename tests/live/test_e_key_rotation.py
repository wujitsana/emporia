"""P248 (e): an agent rotates its identity key on its home relay; agents and relays elsewhere follow its signed
key log, keep checking what it signed before, and refuse what only the old key signed after."""
import json
import sqlite3

import httpx

from tests.live.threads import thread_id_for


def _pin(agent, other: str) -> dict:
    """The signing key `agent` has pinned for `other` (an address)."""
    pins = json.loads((agent.keys / "peer_signing_keys.json").read_text())
    [found] = [v for k, v in pins.items() if k.startswith(other.split("@")[0] + "@")]
    return found


def _remote_copy(relay, address: str) -> dict:
    """What a relay holds of an agent on another relay (the copy pinned when a DM from it arrived)."""
    db = sqlite3.connect(f"file:{relay.root / 'relay.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return dict(db.execute("SELECT * FROM remote_agents WHERE address = ?", (address,)).fetchone())


def _read(agent, other: str) -> dict:
    out = agent.tool("get_dm_messages", thread_id=thread_id_for(agent, other))
    assert out["success"], out
    return out


def test_a_rotated_key_is_followed_from_other_relays_and_the_old_key_signs_nothing_new(fleet):
    a, b = fleet.a, fleet.b
    alpha, bob = fleet.registered("alpha", a), fleet.registered("bob", b)
    for agent in (alpha, bob):
        assert agent.tool("set_dm_policy", other_relays=True)["success"]
    old_key = alpha.public_key

    # before: a DM signed by the first key; bob pins that key
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="signed with my first key")["success"]
    assert _read(bob, alpha.address)["messages"][0]["verified"] is True
    assert _pin(bob, alpha.address)["public_key_hex"] == old_key

    # alpha rotates her key on A
    rotated = alpha.rotate_key()
    new_key = alpha.public_key
    assert new_key != old_key and rotated["new_public_key"] == new_key
    profile = httpx.get(f"{a.url}/agents/alpha/key-log").json()
    assert profile["public_key_hex"] == new_key and [(s["old"], s["new"]) for s in profile["key_log"]] == \
        [(old_key, new_key)]

    # her next DM carries the signed log, so B's own copy of her follows it (a bare new key would be refused) ...
    assert _remote_copy(b, alpha.address)["public_key_hex"] == old_key
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="signed with my second key")["success"]
    copy = _remote_copy(b, alpha.address)
    assert copy["public_key_hex"] == new_key and json.loads(copy["key_log_json"]) == profile["key_log"]

    # ... and bob's client follows the same log: its pin moves, and the DM the old key signed still verifies
    after = _read(bob, alpha.address)
    assert "signing_key_changed" not in after
    assert [(m["content"], m["verified"]) for m in after["messages"]] == [
        ("signed with my first key", True), ("signed with my second key", True)], after
    assert _pin(bob, alpha.address)["public_key_hex"] == new_key
    assert bob.tool("send_dm", to_agent_id=alpha.address, content="got both")["success"]
    assert [m["verified"] for m in _read(alpha, bob.address)["messages"]] == [True, True, True]
