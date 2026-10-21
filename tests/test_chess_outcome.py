"""Chess names the right winner, and a draw settles (P81).

`is_terminal` returned `state.current_agent` as the winner. After the final move
that is the side *to move* — the player just checkmated — and settlement reads
`is_terminal`, so every decided game paid the loser and every draw paid whoever
was on move. Nothing caught it because no test played chess to a result through
the payout path; the end-to-end bounty test uses the `service` module.
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia.module_sdk import SessionAction, SessionState, get_interaction_module
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

pytest.importorskip("chess")

FOOLS_MATE = [("w", "f2f3"), ("b", "e7e5"), ("w", "g2g4"), ("b", "d8h4")]


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216))


def _play(module, state, moves, players):
    for side, uci in moves:
        result = module.apply_action(
            state, SessionAction(agent_id=players[side], action_type="move",
                                 payload={"uci": uci}),
        )
        state = result.new_state
    return state, result


def test_checkmate_names_the_player_who_delivered_it():
    module = get_interaction_module("emporia:chess:v1")
    players = {"w": "white_agent", "b": "black_agent"}
    state = module.initial_state([players["w"], players["b"]], {})
    state, result = _play(module, state, FOOLS_MATE, players)

    over, outcome = module.is_terminal(state)
    assert over
    assert outcome["winner"] == "black_agent", "the mated side was paid"
    assert outcome["outcome_type"] == "won"
    # The two places that report a result agree.
    assert result.artifacts["result"]["winner"] == outcome["winner"]


def test_a_draw_names_no_winner():
    module = get_interaction_module("emporia:chess:v1")
    # White to move; Kh6 stalemates black.
    state = SessionState(
        data={"board_fen": "7k/5Q2/6K1/8/8/8/8/8 w - - 0 1"},
        current_agent="white_agent", step_number=0,
        metadata={"participants": ["white_agent", "black_agent"]},
    )
    state, result = _play(module, state, [("w", "g6h6")],
                          {"w": "white_agent", "b": "black_agent"})
    over, outcome = module.is_terminal(state)
    assert over
    assert outcome["winner"] is None, "a stalemate paid whoever was on move"
    assert outcome["outcome_type"] == "draw"
    assert result.artifacts["result"]["winner"] is None


def _start_game(client):
    white, black = (f"cw_{uuid.uuid4().hex[:8]}", f"cb_{uuid.uuid4().hex[:8]}")
    _register_keyed(client, white)
    _register_keyed(client, black)
    session_id = client.post(
        "/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": white},
        headers={"x-emporia-agent-id": white},
    ).json()["session_id"]
    r = client.post(f"/sessions/{session_id}/join", json={"agent_id": black},
                    headers={"x-emporia-agent-id": black})
    assert r.status_code == 200, r.text
    return session_id, {"w": white, "b": black}


def _settlement(session_id):
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM settlements WHERE session_id = ?",
                            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def test_a_checkmate_through_the_relay_records_the_real_winner(client):
    session_id, players = _start_game(client)
    for side, uci in FOOLS_MATE:
        body = _signed_action_body(client, session_id, players[side], "move",
                                   {"uci": uci}, "Playing the next move in the line")
        r = client.post(f"/sessions/{session_id}/action", json=body)
        assert r.status_code == 200, r.text
    assert r.json()["outcome"]["winner"] == players["b"]
    [row] = _settlement(session_id)
    assert row["winner_id"] == players["b"]


def test_a_drawn_session_writes_a_refunded_settlement(client):
    """A terminal result with no winner used to reach neither settlement branch,
    so it recorded nothing: holds sat until they expired, and a bounty on the
    game stayed `running` forever."""
    session_id, players = _start_game(client)
    module_cls = type(get_interaction_module("emporia:chess:v1"))
    draw = (True, {"winner": None, "outcome_type": "draw", "termination": "STALEMATE"})
    with patch.object(module_cls, "is_terminal", return_value=draw):
        body = _signed_action_body(client, session_id, players["w"], "move",
                                   {"uci": "e2e4"}, "Opening with the king pawn")
        r = client.post(f"/sessions/{session_id}/action", json=body)
    assert r.status_code == 200, r.text
    [row] = _settlement(session_id)
    assert row["status"] == "refunded"
    assert row["winner_id"] is None
