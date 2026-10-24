"""P131: chess game settings — a start position, the creator's colour, a move clock."""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.module_sdk import SessionAction, SessionState
from emporia.modules.chess import ChessModule
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

#: White mates in one: Ra8#.
MATE_IN_ONE = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
CHESS = "emporia:chess:v1"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51217))


def _agents(client):
    a, b = f"ca_{uuid.uuid4().hex[:8]}", f"cb_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    return a, b


def _create(client, creator, config):
    return client.post("/sessions", json={"module_type": CHESS, "creator_agent_id": creator,
                                          "config": config},
                       headers={"x-emporia-agent-id": creator})


def _join(client, sid, who):
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": who},
                    headers={"x-emporia-agent-id": who})
    assert r.status_code == 200, r.text


def _move(client, sid, who, move):
    body = _signed_action_body(client, sid, who, "move", {"move": move},
                               "Playing the move this position calls for")
    return client.post(f"/sessions/{sid}/action", json=body)


@pytest.mark.parametrize("config, why", [
    ({"start_fen": "not a fen"}, "not a valid FEN"),
    ({"start_fen": "8/8/8/8/8/8/8/8 w - - 0 1"}, "not a legal position"),
    ({"start_fen": "R5k1/5ppp/8/8/8/8/8/6K1 b - - 1 1"}, "already decided"),
    ({"creator_plays": "red"}, "creator_plays"),
    ({"move_time_limit_s": 5}, "move_time_limit_s"),
    ({"move_time_limit_s": True}, "move_time_limit_s"),
])
def test_bad_settings_are_refused_before_anyone_stakes(client, config, why):
    a, _ = _agents(client)
    r = _create(client, a, config)
    assert r.status_code == 400 and why in r.text


def test_a_game_from_a_set_position_with_the_creator_on_black(client):
    """The creator offers a position as black; the joiner, white to move, takes
    it — and wins in one, SAN accepted, paid to the right colour."""
    a, b = _agents(client)
    r = _create(client, a, {"start_fen": MATE_IN_ONE, "creator_plays": "black"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    listed = client.get("/sessions", params={"status": "waiting", "module_type": CHESS},
                        headers={"x-emporia-agent-id": b}).json()["sessions"]
    [mine] = [s for s in listed if s["session_id"] == sid]
    assert mine["config"]["start_fen"] == MATE_IN_ONE, "a joiner sees the position first"
    _join(client, sid, b)
    s = rs.load_session(sid)
    assert (s["state"]["data"]["white"], s["state"]["data"]["black"]) == (b, a)
    assert s["current_agent"] == b, "white is to move in this position"
    assert _move(client, sid, a, "g7g6").status_code == 400, "not black's turn"
    r = _move(client, sid, b, "Ra8#")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"]["winner"] == b
    assert rs.load_session(sid)["status"] == "completed"


def test_running_out_of_time_loses_and_the_sweep_ends_it(client):
    a, b = _agents(client)
    sid = _create(client, a, {"move_time_limit_s": 60}).json()["session_id"]
    _join(client, sid, b)
    s = rs.load_session(sid)
    state = s["state"]
    state["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    rs.save_session(session_id=sid, module_type=CHESS, config=s["config"],
                    payment_rules=s["payment_rules"], state=SessionState.from_dict(state),
                    current_agent=s["current_agent"], step_number=0, status="active",
                    participants=s["participants"], created_at=s["created_at"],
                    updated_at=datetime.now(UTC).isoformat())
    r = _move(client, sid, a, "e4")
    assert r.status_code == 400 and "ran out" in r.text

    from emporia.routes.sessions import sweep_service_deadlines
    assert asyncio.run(sweep_service_deadlines())["game_timeouts"] >= 1
    assert rs.load_session(sid)["status"] == "completed"


def test_states_from_before_colours_were_stored_still_settle():
    board_state = SessionState(
        data={"board_fen": "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"}, current_agent="w",
        step_number=9, metadata={"participants": ["w", "b"]})
    m = ChessModule()
    res = m.apply_action(board_state, SessionAction(
        agent_id="w", action_type="move", payload={"uci": "a1a8"}))
    assert res.artifacts["result"]["winner"] == "w"
