"""P137b: lowest unique pick — the second pick game on the shared base."""
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import commit_reveal
from emporia import relay_server as rs
from emporia.modules.lowest_unique import _verdict
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

GAME = "emporia:lowest-unique:v1"


def test_the_lowest_number_only_one_player_picked_wins():
    assert _verdict({"a": 1, "b": 1, "c": 2, "d": 3})["winner"] == "c"
    assert _verdict({"a": 1, "b": 1, "c": 2, "d": 2})["outcome_type"] == "draw"
    assert _verdict({"a": 5, "b": 1, "c": 1, "d": 1})["winning_pick"] == 5


def test_a_four_player_game_through_the_relay():
    client = TestClient(app, client=("127.0.0.1", 51222))
    players = [f"lu{i}_{uuid.uuid4().hex[:6]}" for i in range(4)]
    for p in players:
        _register_keyed(client, p)
    sid = client.post("/sessions", json={"module_type": GAME, "creator_agent_id": players[0]},
                      headers={"x-emporia-agent-id": players[0]}).json()["session_id"]
    for p in players[1:]:
        assert client.post(f"/sessions/{sid}/join", json={"agent_id": p},
                           headers={"x-emporia-agent-id": p}).status_code == 200
    assert rs.load_session(sid)["status"] == "active", "default: starts at four"
    picks = dict(zip(players, (1, 1, 3, 2)))
    nonces = {p: commit_reveal.new_nonce() for p in players}

    def act(who, action, payload):
        body = _signed_action_body(client, sid, who, action, payload, "Picking what they will avoid")
        return client.post(f"/sessions/{sid}/action", json=body)
    for p in players:
        assert act(p, "commit", {"commitment": commit_reveal.commitment(picks[p], nonces[p])}).status_code == 200
    for p in players:
        r = act(p, "reveal", {"move": picks[p], "nonce": nonces[p]})
        assert r.status_code == 200, r.text
    assert r.json()["outcome"]["winner"] == players[3], "2 is the lowest picked once"


@pytest.mark.parametrize("pick", [0, 11])
def test_a_pick_outside_the_range_cannot_be_revealed(pick):
    from emporia.module_sdk import SessionAction, SessionState
    from emporia.modules.lowest_unique import LowestUniqueModule
    m = LowestUniqueModule()
    st = m.initial_state(["a", "b", "c", "d"], {})
    nonce = commit_reveal.new_nonce()
    for p in "abcd":
        st = m.apply_action(st, SessionAction(agent_id=p, action_type="commit", payload={
            "commitment": commit_reveal.commitment(pick if p == "a" else 1, nonce)})).new_state
    ok, why = m.validate_action(st, SessionAction(agent_id="a", action_type="reveal",
                                                  payload={"move": pick, "nonce": nonce}))
    assert not ok and "1–10" in why
