"""Per-game play guides: HOW_TO_PLAY on every game module, served with the rules and with the game."""
import json

import pytest
from fastapi.testclient import TestClient

from emporia import mcp_server
from emporia.module_sdk import HOW_TO_PLAY_MAX, MODULE_REGISTRY, list_available_modules
from emporia.modules.chess import ChessModule
from emporia.relay_server import app
from tests.test_chess_settings import _agents, _create

#: Each guide's worked move, and the game data its module checks it against.
BLOTTO_DATA = {"battlefields": 5, "troops": 100}
EXAMPLES = {
    "emporia:blotto:v1": ([40, 30, 20, 10, 0], BLOTTO_DATA),
    "emporia:blotto:v2": ([40, 30, 20, 10, 0], BLOTTO_DATA),
    "emporia:two-thirds:v1": (33, {}),
    "emporia:two-thirds:v2": (33, {}),
    "emporia:lowest-unique:v1": (3, {"max_pick": 10}),
    "emporia:lowest-unique:v2": (3, {"max_pick": 10}),
    "emporia:cv-auction:v1": (120, {}),
    "emporia:liars-dice:v1": ({"quantity": 4, "face": 3}, {"players": 3, "dice": 5, "bids": []}),
    "emporia:bargain:v2": ({"keep": {"silk": 2, "spice": 0, "amber": 1}},
                           {"pool": {"silk": 3, "spice": 2, "amber": 1}, "offers": []}),
}
CLAIM = {"silk": 4, "spice": 3, "amber": 3}
GAMES = [t for t, c in MODULE_REGISTRY.items() if c.HOW_TO_PLAY]


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51299))


def test_every_game_module_has_a_guide_under_the_cap():
    games = [t for t, c in MODULE_REGISTRY.items() if c.KIND == "game"
             and t.split(":")[1] not in ("service", "research", "code-review", "generic")]
    assert games
    for t in games:
        guide = MODULE_REGISTRY[t].HOW_TO_PLAY.strip()
        assert guide, t
        assert len(guide) <= HOW_TO_PLAY_MAX, (t, len(guide))
    assert set(EXAMPLES) | {"emporia:chess:v1"} >= set(games)


@pytest.mark.parametrize("module_type", sorted(EXAMPLES))
def test_the_guides_example_move_is_one_the_module_accepts(module_type):
    move, data = EXAMPLES[module_type]
    cls = MODULE_REGISTRY[module_type]
    assert cls().move_problem(data, move) == ""
    guide = cls.HOW_TO_PLAY
    shown = json.dumps(move)
    assert shown in guide or json.dumps(move, separators=(", ", ": ")) in guide, (module_type, shown)
    if isinstance(move, dict) and "keep" in move:
        assert cls().move_problem(data, {**move, "claim": CLAIM}) == ""


def test_the_chess_example_is_a_legal_opening_move():
    from emporia.module_sdk import SessionAction
    assert '{"move": "e4"}' in ChessModule.HOW_TO_PLAY
    module = ChessModule()
    state = module.initial_state(["a", "b"], {})
    mover = state.current_agent
    ok, why = module.validate_action(state, SessionAction(
        agent_id=mover, action_type="move", payload={"move": "e4"}))
    assert ok, why


def test_the_modules_listing_serves_how_to_play(client):
    listed = {m["module_type"]: m for m in client.get("/modules").json()["modules"]}
    for t in GAMES:
        assert listed[t]["how_to_play"] == MODULE_REGISTRY[t].HOW_TO_PLAY.strip()
    assert {m["module_type"]: m["how_to_play"] for m in list_available_modules()}["emporia:chess:v1"]


def test_join_and_get_session_carry_the_guide_for_players_only(client):
    a, b = _agents(client)
    sid = _create(client, a, {}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b})
    assert r.status_code == 200
    assert r.json()["how_to_play"] == ChessModule.HOW_TO_PLAY.strip()
    mine = client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": a}).json()
    assert mine.get("how_to_play") == ChessModule.HOW_TO_PLAY.strip()
    assert "how_to_play" not in client.get(f"/sessions/{sid}").json()


def test_the_mcp_get_session_hands_the_guide_over_once(monkeypatch):
    mcp_server._GUIDE_SENT.discard("s1")
    first = {"how_to_play": "g"}
    mcp_server._guide_once(first, "s1")
    assert first == {"how_to_play": "g"}
    again = {"how_to_play": "g"}
    mcp_server._guide_once(again, "s1")
    assert again == {}
