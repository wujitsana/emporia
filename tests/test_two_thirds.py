"""P136: guess two-thirds of the average — 3-8 players, simultaneous, commit-reveal."""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import commit_reveal
from emporia import relay_server as rs
from emporia.module_sdk import SessionState
from emporia.modules.two_thirds import _verdict
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body
from tests.test_platform_bridge import _agent_events

GAME = "emporia:two-thirds:v1"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51221))


def _game(client, n=3, config=None):
    players = [f"tt{i}_{uuid.uuid4().hex[:6]}" for i in range(n)]
    for p in players:
        _register_keyed(client, p)
    sid = client.post("/sessions", json={"module_type": GAME, "creator_agent_id": players[0],
                                         "config": {"players": n, **(config or {})}},
                      headers={"x-emporia-agent-id": players[0]}).json()["session_id"]
    for p in players[1:]:
        r = client.post(f"/sessions/{sid}/join", json={"agent_id": p},
                        headers={"x-emporia-agent-id": p})
        assert r.status_code == 200, r.text
    return sid, players


def _act(client, sid, who, action, payload):
    body = _signed_action_body(client, sid, who, action, payload, "Judging how deep they think")
    return client.post(f"/sessions/{sid}/action", json=body)


def test_three_players_in_any_order_and_the_closest_wins(client):
    sid, (a, b, c) = _game(client)
    assert rs.load_session(sid)["status"] == "active"
    picks = {a: 50, b: 22, c: 0}          # average 24, target 16 → b (6 away) beats c (16)
    nonces = {p: commit_reveal.new_nonce() for p in picks}
    for p in (c, a, b):                   # any order: no turns
        r = _act(client, sid, p, "commit", {"commitment": commit_reveal.commitment(picks[p], nonces[p])})
        assert r.status_code == 200, r.text
    assert [e for e in _agent_events(a, "your_turn") if e["session_id"] == sid], \
        "everyone hears when the reveal phase opens"
    for p in (b, c):
        assert _act(client, sid, p, "reveal", {"move": picks[p], "nonce": nonces[p]}).status_code == 200
    r = _act(client, sid, a, "reveal", {"move": picks[a], "nonce": nonces[a]})
    out = r.json()["outcome"]
    assert (out["winner"], out["target"]) == (b, 16.0)
    assert rs.load_session(sid)["status"] == "completed"


def test_a_tie_for_closest_is_a_draw():
    assert _verdict({"a": 10, "b": 10, "c": 40})["winner"] is None
    assert _verdict({"a": 10, "b": 10, "c": 40})["termination"] == "tie"


def test_the_game_waits_for_the_players_its_creator_asked_for(client):
    players = [f"tw{i}_{uuid.uuid4().hex[:6]}" for i in range(3)]
    for p in players:
        _register_keyed(client, p)
    sid = client.post("/sessions", json={"module_type": GAME, "creator_agent_id": players[0],
                                         "config": {"players": 4}},
                      headers={"x-emporia-agent-id": players[0]}).json()["session_id"]
    for p in players[1:]:
        client.post(f"/sessions/{sid}/join", json={"agent_id": p}, headers={"x-emporia-agent-id": p})
    assert rs.load_session(sid)["status"] == "waiting", "three of four joined: not started"


def test_one_who_commits_and_does_not_reveal_is_out(client):
    sid, (a, b, c) = _game(client, config={"move_time_limit_s": 60})
    picks = {a: 30, b: 20, c: 90}
    nonces = {p: commit_reveal.new_nonce() for p in picks}
    for p in picks:
        _act(client, sid, p, "commit", {"commitment": commit_reveal.commitment(picks[p], nonces[p])})
    for p in (a, b):
        _act(client, sid, p, "reveal", {"move": picks[p], "nonce": nonces[p]})
    s = rs.load_session(sid)
    state = s["state"]
    state["data"]["phase_started_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    rs.save_session(session_id=sid, module_type=GAME, config=s["config"],
                    payment_rules=s["payment_rules"], state=SessionState.from_dict(state),
                    current_agent=s["current_agent"], step_number=s["step_number"], status="active",
                    participants=s["participants"], created_at=s["created_at"],
                    updated_at=datetime.now(UTC).isoformat())
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())
    assert rs.load_session(sid)["status"] == "completed"
    [res] = [r for r in client.get("/results", params={"agent_id": a}).json()["results"]
             if r["session_id"] == sid]
    assert res["winner"] == b and res["termination"] == "timeout", \
        "decided among those who revealed: target 2/3·25 ≈ 16.7, b is closest"


@pytest.mark.parametrize("config", [{"players": 2}, {"players": 9}, {"move_time_limit_s": 5}])
def test_bad_settings_are_refused(client, config):
    a = f"tx_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": GAME, "creator_agent_id": a, "config": config},
                    headers={"x-emporia-agent-id": a})
    assert r.status_code == 400
