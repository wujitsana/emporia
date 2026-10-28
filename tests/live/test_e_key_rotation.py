"""P248 (e): an agent rotates its identity key on its home relay; agents and relays elsewhere follow its signed
key log, keep checking what it signed before, and refuse what only the old key signed after."""
import json
import sqlite3

import httpx

from emporia import identity, wire
from emporia.federation_dm import thread_id_for


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
    out = agent.tool("get_dm_messages", thread_id=thread_id_for(agent.address, other))
    assert out["success"], out
    return out


def _titles(out: dict) -> list[str]:
    return sorted(x["title"] for x in out["listings"] if x.get("federated_from"))


def test_a_rotated_key_is_followed_from_other_relays_and_the_old_key_signs_nothing_new(fleet):
    a, b, victim, rogue = fleet.a, fleet.b, fleet.victim, fleet.rogue
    alpha, bob = fleet.registered("alpha", a), fleet.registered("bob", b)
    for agent in (alpha, bob):
        assert agent.tool("set_dm_policy", other_relays=True)["success"]
    old_key = alpha.public_key

    # before: a DM and a listing, signed by the first key; bob pins that key and B shows the listing
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="signed with my first key")["success"]
    assert _read(bob, alpha.address)["messages"][0]["verified"] is True
    assert _pin(bob, alpha.address)["public_key_hex"] == old_key
    assert alpha.tool("create_listing", agent_id="alpha", title="Chess lessons", price_usd="3.00")["success"]
    stale_copy = next(x for x in httpx.get(f"{a.url}/gaming/v1/federate/listings").json()["listings"])
    b.sync()
    assert _titles(bob.tool("list_listings")) == ["Chess lessons"]

    # alpha rotates her key on A
    rotated = alpha.rotate_key()
    new_key = alpha.public_key
    assert new_key != old_key and rotated["new_public_key"] == new_key
    profile = httpx.get(f"{a.url}/agents/alpha/key-log").json()
    assert profile["public_key_hex"] == new_key and [(s["old"], s["new"]) for s in profile["key_log"]] == \
        [(old_key, new_key)]

    # the listing the old key signed leaves A's feed, so B's copy goes with the next pull; she signs a new one
    assert httpx.get(f"{a.url}/gaming/v1/federate/listings").json()["listings"] == []
    b.sync()
    assert _titles(bob.tool("list_listings")) == []
    again = alpha.tool("create_listing", agent_id="alpha", title="Chess lessons, again", price_usd="3.00")
    assert again["success"], again
    record = b.sync()["peers"][a.url]
    assert record["ok"] and record["imported"] == 1 and not record.get("refused"), record
    shown = bob.tool("list_listings")
    assert _titles(shown) == ["Chess lessons, again"] and "refused_federated" not in shown
    [item] = [x for x in shown["listings"] if x.get("federated_from")]
    assert item["agent_public_key_hex"] == new_key and item["key_log"][-1]["new"] == new_key
    assert wire.listing_verdict(item, identity.verify) == ""          # the log, signed step by step, leads to her key

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

    # a relay that serves the listing the old key signed, naming the log it must answer to, gets it refused
    rogue.target = a.url
    rogue.rewrite["listings"] = lambda items: [*items, {**stale_copy, "key_log": profile["key_log"]}]
    record = victim.sync()["peers"][rogue.url]
    assert record["ok"] and record["imported"] == 1 and record["refused"] == 1, record
    assert [x["title"] for x in httpx.get(f"{victim.url}/listings").json()["listings"]
            if x.get("federated_from")] == ["Chess lessons, again"]

    # A limit, stated so nobody counts on more: a relay that leaves the log out says nothing against the old
    # key, and the listing - still her word, once - checks. What ends it is its own expiry (a week by default).
    assert wire.listing_verdict(stale_copy, identity.verify) == ""
