"""P248 (c): an agent visits another relay with its own key, plays there, and both sides can check the game."""
import httpx


def _move(agent, sid: str, uci: str, relay: str = "") -> dict:
    kwargs = {"relay": relay} if relay else {}
    r = agent.tool("submit_action", session_id=sid, action_type="move", payload={"uci": uci},
                   rationale=f"playing {uci} to develop and keep the king safe", **kwargs)
    assert r["success"], r
    return r


def test_visiting_registers_the_same_key_on_another_relay_and_a_game_there_verifies_from_both_sides(fleet):
    a, b = fleet.a, fleet.b
    # the operator of alpha's profile decides where she may go: B is on her list, C is not
    alpha = fleet.registered("alpha", a, EMPORIA_VISIT_POLICY="listed", EMPORIA_VISIT_RELAYS=b.url)
    bob = fleet.registered("bob", b)
    home_card = httpx.get(f"{a.url}/agents/alpha/card").json()["card"]

    # she cannot act on B before visiting it, nor visit a relay her operator did not list
    early = alpha.tool("create_session", module_type="emporia:chess:v1", agent_id="alpha", relay=b.url)
    assert not early["success"] and "not visiting" in early["error"], early
    refused = alpha.tool("visit_relay", relay=fleet.c.url)
    assert not refused["success"] and "not among the relays this agent may visit" in refused["error"], refused
    assert httpx.get(f"{fleet.c.url}/agents/alpha/card").status_code in (404, 401, 403)

    visit = alpha.tool("visit_relay", relay=b.url)
    assert visit["success"] and visit["address"] == f"alpha@{b.host}", visit

    # the same agent on B: the same key and the same stable id, not a namesake
    on_a = httpx.get(f"{a.url}/agents/alpha/card").json()["card"]
    on_b = httpx.get(f"{b.url}/agents/alpha/card").json()["card"]
    assert on_b["public_key_hex"] == on_a["public_key_hex"] == alpha.public_key == home_card["public_key_hex"]
    assert on_b["fingerprint"] == on_a["fingerprint"]
    assert on_b["relay_list"]["home"] == a.url          # B shows where she lives: still A

    # she makes a free chess game on B, bob (who lives there) joins, and they play to a result
    made = alpha.tool("create_session", module_type="emporia:chess:v1", agent_id="alpha", relay=b.url)
    assert made["success"], made
    sid = made["session_id"]
    assert httpx.get(f"{a.url}/sessions/{sid}").status_code == 404      # nothing of it on her home relay
    joined = bob.tool("join_session", session_id=sid, agent_id="bob")
    assert joined["success"], joined
    _move(alpha, sid, "f2f3", relay=b.url)
    _move(bob, sid, "e7e5")
    _move(alpha, sid, "g2g4", relay=b.url)
    last = _move(bob, sid, "d8h4")                                       # fool's mate
    assert last["success"]

    # each side checks the game without taking B's word for it: the log, every move's signature against
    # the player's own key, and the result B declared against a replay of the moves
    for who, relay in ((alpha, b.url), (bob, "")):
        kwargs = {"relay": relay} if relay else {}
        checked = who.tool("verify_session", session_id=sid, agent_id=who.name, **kwargs)
        assert checked["success"] and checked["verdict"] == "ok" and checked["failures"] == 0, (who.name, checked)
        assert not checked["problems"] and "signing_key_changed" not in checked, checked
    state = bob.tool("get_session", session_id=sid)
    assert state["status"] == "completed", state
