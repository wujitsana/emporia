"""P132: Colonel Blotto by commit-reveal — the game an engine cannot simply win."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import calls, commit_reveal
from emporia import relay_server as rs
from emporia.module_sdk import SessionState
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

BLOTTO = "emporia:blotto:v2"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51218))


def _game(client, config=None):
    a, b = f"ba_{uuid.uuid4().hex[:8]}", f"bb_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    r = client.post("/sessions", json={"module_type": BLOTTO, "creator_agent_id": a,
                                       "config": config or {}},
                    headers={"x-emporia-agent-id": a})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": b},
                    headers={"x-emporia-agent-id": b})
    assert r.status_code == 200, r.text
    return sid, a, b


def _act(client, sid, who, action, payload):
    body = _signed_action_body(client, sid, who, action, payload,
                               "Allocating to beat what they did last round")
    return client.post(f"/sessions/{sid}/action", json=body)


def _round(client, sid, a, b, move_a, move_b):
    na, nb = commit_reveal.new_nonce(), commit_reveal.new_nonce()
    for who, move, nonce in ((a, move_a, na), (b, move_b, nb)):
        r = _act(client, sid, who, "commit", {"commitment": calls.commitment(move, None, nonce)})
        assert r.status_code == 200, r.text
    for who, move, nonce in ((a, move_a, na), (b, move_b, nb)):
        r = _act(client, sid, who, "reveal", {"move": move, "nonce": nonce})
        assert r.status_code == 200, r.text
    return r


def test_a_best_of_three_is_decided_by_rounds_and_settles(client):
    sid, a, b = _game(client, {"rounds": 3})
    _round(client, sid, a, b, [30, 30, 30, 5, 5], [20, 20, 20, 20, 20])   # a: 3 fields
    s = rs.load_session(sid)
    assert s["status"] == "active" and s["state"]["data"]["wins"] == {a: 1, b: 0}
    assert s["state"]["data"]["history"][0]["allocations"][b] == [20, 20, 20, 20, 20], \
        "every past round is public — reading it is the skill"
    r = _round(client, sid, a, b, [34, 33, 33, 0, 0], [0, 0, 20, 40, 40])   # a: 3 fields to 2
    out = r.json()["outcome"]
    assert out["winner"] == a and out["rounds_won"] == {a: 2, b: 0}, "2–0 of 3 ends it early"
    assert rs.load_session(sid)["status"] == "completed"


def test_the_relay_holds_only_a_hash_until_the_reveal(client):
    sid, a, b = _game(client)
    move, nonce = [20, 20, 20, 20, 20], commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": calls.commitment(move, None, nonce)})
    stored = json.dumps(rs.load_session(sid)) + json.dumps(rs.get_session_actions(sid)
                                                           if hasattr(rs, "get_session_actions") else [])
    assert nonce not in stored and "[20, 20, 20, 20, 20]" not in stored


def test_a_reveal_that_differs_from_the_commitment_is_refused(client):
    sid, a, b = _game(client)
    na, nb = commit_reveal.new_nonce(), commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": calls.commitment([20] * 5, None, na)})
    _act(client, sid, b, "commit", {"commitment": calls.commitment([20] * 5, None, nb)})
    r = _act(client, sid, a, "reveal", {"move": [100, 0, 0, 0, 0], "nonce": na})
    assert r.status_code == 400 and "does not match" in r.text
    assert _act(client, sid, b, "reveal", {"move": [20] * 5, "nonce": nb}).status_code == 400, \
        "not b's turn"


def test_an_illegal_committed_move_cannot_be_revealed(client):
    sid, a, b = _game(client)
    na = commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": calls.commitment([99, 0, 0, 0, 0], None, na)})
    _act(client, sid, b, "commit", {"commitment": calls.commitment([20] * 5, None, "x" * 40)})
    r = _act(client, sid, a, "reveal", {"move": [99, 0, 0, 0, 0], "nonce": na})
    assert r.status_code == 400 and "exactly 100 troops" in r.text


def test_declining_to_reveal_loses_on_time(client):
    """b sees a's reveal, sees the round is lost, and stays silent: without the
    clock that would turn a loss into a refund."""
    sid, a, b = _game(client, {"rounds": 1, "move_time_limit_s": 60})
    na, nb = commit_reveal.new_nonce(), commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": calls.commitment([30, 30, 30, 5, 5], None, na)})
    _act(client, sid, b, "commit", {"commitment": calls.commitment([20] * 5, None, nb)})
    _act(client, sid, a, "reveal", {"move": [30, 30, 30, 5, 5], "nonce": na})
    s = rs.load_session(sid)
    state = s["state"]
    state["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    rs.save_session(session_id=sid, module_type=BLOTTO, config=s["config"],
                    payment_rules=s["payment_rules"], state=SessionState.from_dict(state),
                    current_agent=s["current_agent"], step_number=s["step_number"],
                    status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())
    from emporia.routes.sessions import sweep_service_deadlines
    assert asyncio.run(sweep_service_deadlines())["game_timeouts"] >= 1
    s = rs.load_session(sid)
    assert s["status"] == "completed"


def test_one_unreadable_session_does_not_stop_the_deadline_sweep(client, caplog):
    """gamify-all review: the sweep stopped at the first timed session it could not read, so
    one bad row left every later session (of that game and every game after it) never ended
    on time. Now the bad rows are skipped and logged, and the sweep goes on."""
    import logging
    sid, a, b = _game(client, {"rounds": 1, "move_time_limit_s": 60})
    na, nb = commit_reveal.new_nonce(), commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": calls.commitment([30, 30, 30, 5, 5], None, na)})
    _act(client, sid, b, "commit", {"commitment": calls.commitment([20] * 5, None, nb)})
    _act(client, sid, a, "reveal", {"move": [30, 30, 30, 5, 5], "nonce": na})
    s = rs.load_session(sid)
    state = s["state"]
    state["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    rs.save_session(session_id=sid, module_type=BLOTTO, config=s["config"],
                    payment_rules=s["payment_rules"], state=SessionState.from_dict(state),
                    current_agent=s["current_agent"], step_number=s["step_number"],
                    status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())
    # Two bad rows of the same game, older (so the sweep meets them first): one whose JSON
    # does not parse, one that parses but is no state the module can read.
    bad = [f"sess_bad_{uuid.uuid4().hex[:8]}" for _ in range(2)]
    with rs.DB_LOCK, rs.get_db() as conn:
        for sid_bad, state_json in zip(bad, ("{not json", "[]")):
            conn.execute("INSERT INTO sessions (session_id, module_type, config_json, payment_rules_json, "
                         "state_json, current_agent, step_number, status, participants_json, created_at, "
                         "updated_at) VALUES (?, ?, '{}', '{}', ?, ?, 0, 'active', ?, ?, ?)",
                         (sid_bad, BLOTTO, state_json, a, json.dumps([a, b]),
                          "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"))
        conn.commit()
    try:
        from emporia.routes.sessions import sweep_service_deadlines
        with caplog.at_level(logging.WARNING):
            counts = asyncio.run(sweep_service_deadlines())
        assert rs.load_session(sid)["status"] == "completed", "the good session after the bad ones was ended"
        assert counts["game_timeouts"] >= 1 and counts.get("skipped", 0) >= 1
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "deadline_sweep_unreadable_session" in logged and "deadline_sweep_skipped_session" in logged
    finally:
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.executemany("DELETE FROM sessions WHERE session_id = ?", [(x,) for x in bad])
            conn.commit()


@pytest.mark.parametrize("config", [{"battlefields": 2}, {"troops": 5}, {"rounds": 0},
                                    {"move_time_limit_s": 10}, {"rounds": "3"}])
def test_bad_settings_are_refused(client, config):
    a = f"bx_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": BLOTTO, "creator_agent_id": a,
                                       "config": config}, headers={"x-emporia-agent-id": a})
    assert r.status_code == 400


def test_commit_move_keeps_the_nonce_local_and_reveal_move_uses_it(tmp_path, monkeypatch):
    from emporia import identity
    from emporia import mcp_server as m
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(m, "submit_action",
                        lambda sid, action, payload, rationale, aid, say="": sent.append((action, payload))
                        or {"success": True})
    state = {"round": 1, "commitments": {}, "battlefields": 5, "troops": 100}
    monkeypatch.setattr(m, "_session_json", lambda sid, aid: {"module_type": BLOTTO, "state": {"data": state}})
    refused = m.commit_move("sess_x", [50, 50], "two fields only", agent_id="me")
    assert not refused["success"] and "5 whole numbers" in refused["error"] and "Nothing was committed" in refused["error"]
    assert not sent, "an illegal move is never committed: it could not be revealed, and loses on time"
    assert m.commit_move("sess_x", [20] * 5, "spread evenly across fields", agent_id="me")["success"]
    [(action, payload)] = sent
    assert action == "commit" and set(payload) == {"commitment", "called"}, "only the hash leaves"
    assert payload["called"] is False, "no call was made, and the commit says so"
    saved = m._commitments_file("me", "sess_x")                     # named for the relay host too (naming F7)
    assert saved.parent == tmp_path / "commitments" and saved.name.startswith("me.") and saved.name.endswith(".sess_x.json")
    assert oct(saved.stat().st_mode & 0o777) == "0o600"

    state["commitments"] = {"me": payload["commitment"]}
    assert "already committed" in m.commit_move("sess_x", [100, 0, 0, 0, 0], "change", agent_id="me")["error"]
    assert m.reveal_move("sess_x", "revealing", agent_id="me")["success"]
    action, reveal = sent[-1]
    assert action == "reveal" and reveal["move"] == [20] * 5
    assert calls.matches(payload["commitment"], reveal["move"], None, reveal["nonce"])


def _m5_game(tmp_path, monkeypatch):
    """A blotto round the MCP server sees through a stub relay that checks reveals."""
    from emporia import identity
    from emporia import mcp_server as m
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    state = {"round": 1, "commitments": {}, "revealed": {}, "battlefields": 5, "troops": 100}
    monkeypatch.setattr(m, "_session_json", lambda sid, aid: {"module_type": BLOTTO, "state": {"data": state}})
    monkeypatch.setattr(m, "_session_data", lambda sid, aid: state)
    return m, state


def _relay_reveal(state, payload):
    # Calls: a :v2 set play commits {move, call_pct}, so the stub checks the :v2 way.
    if calls.matches(state["commitments"].get("me", ""), payload["move"], payload.get("call_pct"), payload["nonce"]):
        state["revealed"]["me"] = payload["move"]
        return {"success": True}
    return {"error": "Relay 400", "detail": "This reveal does not match your commitment", "success": False}


def test_a_retry_with_another_move_while_the_first_is_in_flight_cannot_lose_the_game(tmp_path, monkeypatch):
    """Review follow-up M5: commit_move saved {move, nonce} before sending, over the round's
    entry. An agent that retried with move B while move A was still on its way (its answer
    lost) found no commitment on the relay, so B's nonce replaced A's; the relay kept A, the
    reveal sent B, matched nothing, and the game was lost on time."""
    m, state = _m5_game(tmp_path, monkeypatch)
    sent, retry = [], {}

    def relay(sid, action, payload, rationale, aid, say=""):
        sent.append((action, payload))
        if action == "reveal":
            return _relay_reveal(state, payload)
        if not retry:
            retry["started"] = True       # while A is on its way, the agent tries B
            retry["out"] = m.commit_move("sess_m5", [100, 0, 0, 0, 0], "a different plan entirely", agent_id="me")
            state["commitments"]["me"] = payload["commitment"]   # then A lands
            return {"success": True}
        return {"error": "Relay 400", "detail": "already committed", "success": False}
    monkeypatch.setattr(m, "submit_action", relay)

    assert m.commit_move("sess_m5", [20] * 5, "spread evenly across all fields", agent_id="me")["success"]
    assert not retry["out"]["success"] and "still waiting" in retry["out"]["error"], retry["out"]
    assert [a for a, _ in sent].count("commit") == 1, "the other move was never sent"
    out = m.reveal_move("sess_m5", "revealing the even spread", agent_id="me")
    assert out["success"], out
    assert state["revealed"]["me"] == [20] * 5


def test_reveal_says_so_when_the_saved_move_is_not_the_relays(tmp_path, monkeypatch):
    """M5: reveal_move sent the saved move without comparing its commitment with the one the
    relay holds, and the agent learnt only "does not match" from the relay."""
    m, state = _m5_game(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(m, "submit_action", lambda sid, action, payload, rationale, aid, say="":
                        sent.append(action) or {"success": True})
    assert m.commit_move("sess_m5b", [20] * 5, "spread evenly across all fields", agent_id="me")["success"]
    state["commitments"]["me"] = commit_reveal.commitment([100, 0, 0, 0, 0], commit_reveal.new_nonce())
    out = m.reveal_move("sess_m5b", "revealing the even spread", agent_id="me")
    assert not out["success"] and "not the one you committed" in out["error"], out
    assert sent == ["commit"], "nothing was revealed"
    state["commitments"] = {}
    out = m.reveal_move("sess_m5b", "revealing the even spread", agent_id="me")
    assert not out["success"] and "holds no commitment" in out["error"], out


def test_a_commit_the_relay_refused_can_be_replaced(tmp_path, monkeypatch):
    """M5: the entry is write-once only until the relay answers. A refusal (here: not this
    agent's turn) with no commitment held frees the round for another move."""
    m, state = _m5_game(tmp_path, monkeypatch)
    answers = iter([{"error": "Relay 400", "detail": "Not your turn", "success": False}, {"success": True}])
    sent = []
    monkeypatch.setattr(m, "submit_action", lambda sid, action, payload, rationale, aid, say="":
                        sent.append(payload) or next(answers))
    first = m.commit_move("sess_m5c", [20] * 5, "spread evenly across all fields", agent_id="me")
    assert not first["success"] and "Nothing is committed" in first["note"]
    assert m.commit_move("sess_m5c", [100, 0, 0, 0, 0], "everything on the first field", agent_id="me")["success"]
    assert sent[0] != sent[1]


def test_a_retry_with_another_call_while_the_first_is_in_flight_is_refused(tmp_path, monkeypatch):
    """M5 with calls: the call is sealed in the round's entry, so a retry with the same move
    but another call while the first commit is in flight is refused, and the reveal sends
    the call that was committed."""
    m, state = _m5_game(tmp_path, monkeypatch)
    sent, retry = [], {}

    def relay(sid, action, payload, rationale, aid, say=""):
        sent.append((action, payload))
        if action == "reveal":
            return _relay_reveal(state, payload)
        if not retry:
            retry["out"] = m.commit_move("sess_m5d", [20] * 5, "the same spread, braver", agent_id="me", call=0.9)
            state["commitments"]["me"] = payload["commitment"]
            return {"success": True}
        return {"error": "Relay 400", "detail": "already committed", "success": False}
    monkeypatch.setattr(m, "submit_action", relay)

    assert m.commit_move("sess_m5d", [20] * 5, "spread evenly across all fields", agent_id="me", call=0.6)["success"]
    assert not retry["out"]["success"] and "call 60%" in retry["out"]["error"], retry["out"]
    assert sent[0][1]["called"] is True and [a for a, _ in sent].count("commit") == 1
    again = m.commit_move("sess_m5d", [20] * 5, "spread evenly across all fields", agent_id="me", call=0.6)
    assert again.get("already_committed"), again
    out = m.reveal_move("sess_m5d", "revealing the even spread", agent_id="me")
    assert out["success"], out
    assert sent[-1][1]["call_pct"] == 60


def test_the_default_game_is_one_round_four_moves(client):
    sid, a, b = _game(client)
    r = _round(client, sid, a, b, [30, 30, 30, 5, 5], [20, 20, 20, 20, 20])
    assert r.json()["outcome"]["winner"] == a
    assert rs.load_session(sid)["step_number"] == 4


def test_every_accepted_move_comes_with_a_receipt_the_relay_signed(client):
    """P140: the relay cannot see a move early, but it could refuse one. A
    signed receipt proves it accepted the commit, and when."""
    from emporia import identity
    sid, a, b = _game(client)
    move, nonce = [20] * 5, commit_reveal.new_nonce()
    r = _act(client, sid, a, "commit", {"commitment": calls.commitment(move, None, nonce)})
    rec = r.json()["receipt"]
    assert (rec["session_id"], rec["agent_id"], rec["action_type"]) == (sid, a, "commit")
    assert identity.verify_action_receipt(rec, identity.get_relay_public_key_hex())
    forged = {**rec, "accepted_at": "2000-01-01T00:00:00+00:00"}
    assert not identity.verify_action_receipt(forged, identity.get_relay_public_key_hex())



def test_table_talk_lives_in_the_game(client):
    """P143: chat is part of the game — its visibility, its record, its players."""
    from tests.test_platform_bridge import _agent_events
    sid, a, b = _game(client, {"chat": True, "chat_limit": 2})
    hdr = lambda who: {"x-emporia-agent-id": who}  # noqa: E731
    r = client.post(f"/sessions/{sid}/chat", json={"content": "I'm going heavy on the left."}, headers=hdr(a))
    assert r.status_code == 200 and r.json()["remaining"] == 1
    [heard] = [e for e in _agent_events(b, "game_chat") if e["session_id"] == sid]
    assert heard["content"] == "I'm going heavy on the left." and heard["agent_id"] == a
    outsider = f"bo_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, outsider)
    assert client.post(f"/sessions/{sid}/chat", json={"content": "hi"}, headers=hdr(outsider)).status_code == 403
    assert client.get(f"/sessions/{sid}/chat", headers=hdr(outsider)).json()["messages"][0]["agent_id"] == a, \
        "a public game's talk is public, like its moves"
    client.post(f"/sessions/{sid}/chat", json={"content": "really."}, headers=hdr(a))
    assert client.post(f"/sessions/{sid}/chat", json={"content": "three"}, headers=hdr(a)).status_code == 429
    assert client.post(f"/sessions/{sid}/chat", json={"content": "x" * 501}, headers=hdr(b)).status_code == 400
    quiet, qa, _ = _game(client)
    assert client.post(f"/sessions/{quiet}/chat", json={"content": "hi"}, headers=hdr(qa)).status_code == 400, \
        "off unless the game was created with chat"


def test_a_line_said_with_a_move_arrives_in_the_same_notice(client):
    """P148: table talk with the move — signed with it, stored as table talk,
    and the next player wakes once, not twice."""
    from emporia import identity
    from tests.test_platform_bridge import _agent_events
    sid, a, b = _game(client, {"chat": True, "chat_limit": 1})
    move, nonce = [20] * 5, commit_reveal.new_nonce()
    body = _signed_action_body(client, sid, a, "commit", {"commitment": calls.commitment(move, None, nonce)},
                               "Spreading evenly", say="All in on the middle.")
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 200
    [turn] = [e for e in _agent_events(b, "your_turn") if e["session_id"] == sid]
    assert turn["last_action"]["say"] == "All in on the middle."
    assert not [e for e in _agent_events(b, "game_chat") if e["session_id"] == sid], "woken once"
    talk = client.get(f"/sessions/{sid}/chat").json()["messages"]
    assert [m["content"] for m in talk] == ["All in on the middle."]
    [entry] = [e for e in client.get(f"/sessions/{sid}/audit", headers={"x-emporia-agent-id": a}).json()["chain"]
               if e.get("say")]
    from emporia.wire import signed_action_payload
    key = client.get(f"/agents/{a}", headers={"x-emporia-agent-id": a}).json()["public_key_hex"]
    assert identity.verify(signed_action_payload(entry["stable_id"], "commit", entry["payload"], entry["rationale"], sid,
                                                 entry["step_number"], say=entry["say"]),
                           entry["signature"], key), "the line is signed with the move"
    # Over the limit, the move is refused whole — no half-applied action.
    body = _signed_action_body(client, sid, b, "commit", {"commitment": calls.commitment(move, None, nonce)},
                               "Spreading evenly", say="me too")
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 200, "b has its own limit"
    quiet, qa, _ = _game(client)
    body = _signed_action_body(client, quiet, qa, "commit", {"commitment": calls.commitment(move, None, nonce)},
                               "x" * 20, say="hello")
    assert client.post(f"/sessions/{quiet}/action", json=body).status_code == 400, "no chat in this game"
    assert rs.load_session(quiet)["step_number"] == 0, "and the move did not happen"


def test_validate_turn_checks_a_move_against_the_rules():
    """Found by the end-to-end run: validate_turn called the module's methods on
    its class, so every call raised TypeError."""
    from emporia import mcp_server as m
    from emporia.modules.blotto import BlottoV2Module
    state = BlottoV2Module().initial_state(["a", "b"], {}).to_dict()
    ok = m.validate_turn(BLOTTO, json.dumps(state), "a", "commit", {"commitment": "0" * 64},
                         "committing a spread that beats their last")
    assert ok["success"] and ok["new_state"]["current_agent"] == "b"
    wrong = m.validate_turn(BLOTTO, json.dumps(state), "a", "reveal", {"move": [1], "nonce": "n"},
                            "revealing before anyone has committed")
    assert not wrong["success"] and "commit phase" in wrong["error"]
