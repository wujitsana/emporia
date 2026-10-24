"""P132: Colonel Blotto by commit-reveal — the game an engine cannot simply win."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import commit_reveal
from emporia import relay_server as rs
from emporia.module_sdk import SessionState
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

BLOTTO = "emporia:blotto:v1"


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
        r = _act(client, sid, who, "commit", {"commitment": commit_reveal.commitment(move, nonce)})
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
    _act(client, sid, a, "commit", {"commitment": commit_reveal.commitment(move, nonce)})
    stored = json.dumps(rs.load_session(sid)) + json.dumps(rs.get_session_actions(sid)
                                                           if hasattr(rs, "get_session_actions") else [])
    assert nonce not in stored and "[20, 20, 20, 20, 20]" not in stored


def test_a_reveal_that_differs_from_the_commitment_is_refused(client):
    sid, a, b = _game(client)
    na, nb = commit_reveal.new_nonce(), commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": commit_reveal.commitment([20] * 5, na)})
    _act(client, sid, b, "commit", {"commitment": commit_reveal.commitment([20] * 5, nb)})
    r = _act(client, sid, a, "reveal", {"move": [100, 0, 0, 0, 0], "nonce": na})
    assert r.status_code == 400 and "does not match" in r.text
    assert _act(client, sid, b, "reveal", {"move": [20] * 5, "nonce": nb}).status_code == 400, \
        "not b's turn"


def test_an_illegal_committed_move_cannot_be_revealed(client):
    sid, a, b = _game(client)
    na = commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": commit_reveal.commitment([99, 0, 0, 0, 0], na)})
    _act(client, sid, b, "commit", {"commitment": commit_reveal.commitment([20] * 5, "x" * 40)})
    r = _act(client, sid, a, "reveal", {"move": [99, 0, 0, 0, 0], "nonce": na})
    assert r.status_code == 400 and "exactly 100 troops" in r.text


def test_declining_to_reveal_loses_on_time(client):
    """b sees a's reveal, sees the round is lost, and stays silent: without the
    clock that would turn a loss into a refund."""
    sid, a, b = _game(client, {"rounds": 1, "move_time_limit_s": 60})
    na, nb = commit_reveal.new_nonce(), commit_reveal.new_nonce()
    _act(client, sid, a, "commit", {"commitment": commit_reveal.commitment([30, 30, 30, 5, 5], na)})
    _act(client, sid, b, "commit", {"commitment": commit_reveal.commitment([20] * 5, nb)})
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
    state = {"round": 1, "commitments": {}}
    monkeypatch.setattr(m, "_session_data", lambda sid, aid: state)
    assert m.commit_move("sess_x", [20] * 5, "spread evenly", agent_id="me")["success"]
    [(action, payload)] = sent
    assert action == "commit" and set(payload) == {"commitment"}, "only the hash leaves"
    saved = tmp_path / "commitments" / "me.sess_x.json"
    assert oct(saved.stat().st_mode & 0o777) == "0o600"

    state["commitments"] = {"me": payload["commitment"]}
    assert "already committed" in m.commit_move("sess_x", [100, 0, 0, 0, 0], "change", agent_id="me")["error"]
    assert m.reveal_move("sess_x", "revealing", agent_id="me")["success"]
    action, reveal = sent[-1]
    assert action == "reveal" and reveal["move"] == [20] * 5
    assert commit_reveal.matches(payload["commitment"], reveal["move"], reveal["nonce"])


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
    r = _act(client, sid, a, "commit", {"commitment": commit_reveal.commitment(move, nonce)})
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
    body = _signed_action_body(client, sid, a, "commit", {"commitment": commit_reveal.commitment(move, nonce)},
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
    assert identity.verify(signed_action_payload(a, "commit", entry["payload"], entry["rationale"], sid,
                                                 entry["step_number"], say=entry["say"]),
                           entry["signature"], key), "the line is signed with the move"
    # Over the limit, the move is refused whole — no half-applied action.
    body = _signed_action_body(client, sid, b, "commit", {"commitment": commit_reveal.commitment(move, nonce)},
                               "Spreading evenly", say="me too")
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 200, "b has its own limit"
    quiet, qa, _ = _game(client)
    body = _signed_action_body(client, quiet, qa, "commit", {"commitment": commit_reveal.commitment(move, nonce)},
                               "x" * 20, say="hello")
    assert client.post(f"/sessions/{quiet}/action", json=body).status_code == 400, "no chat in this game"
    assert rs.load_session(quiet)["step_number"] == 0, "and the move did not happen"
