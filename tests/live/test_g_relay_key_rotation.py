"""P248 (g): a relay rotates its own key and restarts; the relays and agents that pinned the old key follow the
signed hand-over, old receipts still check - and a new key with no hand-over is refused."""
import json

import httpx

from tests.live.harness import play_fools_mate


def _relay_pin(agent, relay_url: str) -> str:
    return json.loads((agent.keys / "relay_pins.json").read_text())[relay_url]


def test_a_relay_rotates_its_key_and_peers_and_agents_follow_but_a_bare_change_is_refused(fleet):
    a, b = fleet.a, fleet.b
    alpha, alfred, bob = fleet.registered("alpha", a), fleet.registered("alfred", a), fleet.registered("bob", b)
    for agent in (alpha, bob):
        assert agent.tool("set_dm_policy", other_relays=True)["success"]
    first = a.health()["relay_public_key"]

    # before: B has pulled from A (and pinned its key), alpha has played a game there and kept A's signed receipts
    assert b.sync()["peers"][a.url]["pinned_public_key_hex"] == first
    sid = play_fools_mate(alpha, alfred)
    assert alpha.tool("verify_session", session_id=sid)["verdict"] == "ok"
    assert _relay_pin(alpha, a.url) == first
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="before the rotation")["success"]

    # A's operator rotates its key (`emporia relay rotate-key`) and restarts the relay
    a.stop()
    step = a.rotate_key()
    a.restart()
    health = a.health()
    assert step["old"] == first and health["relay_public_key"] == step["new"] != first
    assert [(s["old"], s["new"]) for s in health["relay_key_history"]] == [(first, step["new"])]

    # B follows the hand-over on its next pull, and its DMs with A keep flowing both ways
    record = b.sync()["peers"][a.url]
    assert record["ok"] and record["pinned_public_key_hex"] == step["new"], record
    assert alpha.tool("send_dm", to_agent_id=bob.address, content="after the rotation")["success"]
    assert bob.tool("send_dm", to_agent_id=alpha.address, content="and back")["success"]
    texts = [m["content"] for m in bob.tool("get_dm_messages", thread_id=_thread(bob, alpha))["messages"]]
    assert texts == ["before the rotation", "after the rotation", "and back"], texts

    # the agent follows it too: she is not told the key changed, and the receipts the old key signed still check
    scanned = alpha.tool("scan_relays")
    assert a.url not in scanned["skipped"], scanned
    checked = alpha.tool("verify_session", session_id=sid)
    assert checked["verdict"] == "ok" and checked["failures"] == 0 and not checked["problems"], checked
    assert _relay_pin(alpha, a.url) == step["new"]

    # a different server at A's address, with a new key and no hand-over from the old: refused everywhere
    a.stop()
    a.replace_key_without_handover()
    a.restart()
    hijacked = a.health()["relay_public_key"]
    assert hijacked not in (first, step["new"])
    record = b.sync()["peers"][a.url]
    assert not record["ok"] and "peer key mismatch" in record["error"], record
    assert record["pinned_public_key_hex"] == step["new"]
    refused = alpha.tool("send_dm", to_agent_id=bob.address, content="from the impostor's relay")
    assert not [m for m in bob.tool("get_dm_messages", thread_id=_thread(bob, alpha))["messages"]
                if m["content"] == "from the impostor's relay"], refused
    scanned = alpha.tool("scan_relays")
    assert "key changed" in scanned["skipped"].get(a.url, ""), scanned
    failed = alpha.tool("verify_session", session_id=sid)
    assert failed["verdict"] == "fail" and any("pinned at first contact" in p for p in failed["problems"]), failed
    assert _relay_pin(alpha, a.url) == step["new"]
    assert httpx.get(f"{b.url}/federation/peers").json()["peers"][0]["pinned_public_key_hex"] == step["new"]


def _thread(agent, other) -> str:
    from emporia.federation_dm import thread_id_for
    return thread_id_for(agent.address, other.address)
