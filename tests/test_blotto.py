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
                        lambda sid, action, payload, rationale, aid: sent.append((action, payload))
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


def test_a_game_with_chat_on_opens_a_table_for_its_players(client):
    """P142: table talk is opt-in — every message costs each player a turn."""
    from emporia import rooms as rooms_db
    from tests.test_platform_bridge import _agent_events
    sid, a, b = _game(client, {"chat": True})
    room = rooms_db.get_room_for_session(sid, db_path=rs.DATABASE_PATH)
    assert room and set(room.members) == {a, b} and room.room_type == "public"
    [started] = [e for e in _agent_events(b, "session_started") if e["session_id"] == sid]
    assert started["room_id"] == room.room_id
    quiet, _, _ = _game(client)
    assert rooms_db.get_room_for_session(quiet, db_path=rs.DATABASE_PATH) is None, "off by default"
