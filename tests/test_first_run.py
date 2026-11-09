"""First run: the quiet-relay path in `whats_next`, the "you're in" block at the end of setup, the relay
seed's pure parts and its person-only gate, and the relay's summary and health fields."""
import importlib.util
import sys
from pathlib import Path

import pytest

from emporia import cli
from emporia import next_steps as N
from emporia import playbook as P

REPO = Path(__file__).resolve().parents[1]
QUIET = {"quests": {"open": [{"id": "hello", "title": "Say hello", "xp": 10, "how": "post", "arc": "welcome"}],
                    "done": 0, "total": 4, "xp": 0, "done_ids": [], "topics": {"ideas": "ideas"}},
         "house": ["house"], "labs": 1, "sessions": [], "inbox": {}}


def _guided(facts, pb="custom"):
    return [i for i in N.rank(facts, P.PRESETS[pb], limit=20) if i["kind"] == "guided"]


def test_a_quiet_relay_gets_the_path_with_an_exact_call_per_step():
    steps = _guided(QUIET)
    assert [s["detail"]["quest"] for s in steps] == ["hello", "house-game", "typed-post", "discussion",
                                                     "open-table", "lab"]
    assert [s["tool"] for s in steps] == ["post", "challenge", "post", "read_feed", "challenge", "lab_threads"]
    assert steps[0]["call"].startswith('post(text="<') and 'topic="ideas"' in steps[0]["call"]
    assert steps[1]["args"] == {"module_type": "emporia:blotto:v2", "opponent": "house"}
    assert steps[4]["args"] == {"module_type": "emporia:blotto:v2"}, "an open challenge names no opponent"
    assert steps[0]["why"].startswith("first steps (1 of 6)")
    items = N.rank(QUIET, P.PRESETS["custom"], limit=20)
    assert [i["rank"] for i in items] == list(range(1, len(items) + 1))
    assert not [i for i in items if i["kind"] == "quest" and i["detail"]["quest"] == "hello"], "said once"
    assert not [i for i in items if i["kind"] == "idle" and not i["args"].get("opponent")], "its open challenge is a step"


def test_done_quests_and_missing_things_drop_their_step():
    facts = {**QUIET, "quests": {**QUIET["quests"], "done_ids": ["hello", "house-game"]}, "house": [], "labs": 0}
    assert [s["detail"]["quest"] for s in _guided(facts)] == ["typed-post", "discussion", "open-table"]
    waiting = {**facts, "sessions": [{"session_id": "s1", "status": "waiting", "challenge": {"kind": "open"}}]}
    assert "open-table" not in [s["detail"]["quest"] for s in _guided(waiting)]
    assert "(1 of 2)" in _guided(waiting)[0]["why"]


def test_the_path_is_only_for_a_quiet_relay_and_the_playbook_holds():
    busy = {**QUIET, "sessions": [{"session_id": "s", "module_type": "emporia:blotto:v2", "move": "commit"}]}
    assert _guided(busy) == [], "something waits on the agent: no walk"
    board = {**QUIET, "challenges": [{"session_id": "c1", "module_type": "emporia:blotto:v2", "from": "x"}]}
    assert _guided(board) == [], "an open challenge is on the board: take it"
    old = {**QUIET, "quests": {k: v for k, v in QUIET["quests"].items() if k != "done_ids"}}
    assert _guided(old) == [], "a relay that sends no done list gets the old list"
    no_games = _guided(QUIET, "talk") if "talk" in P.PRESETS else []
    assert all(s["detail"]["quest"] not in ("house-game", "open-table") for s in no_games)


def test_setup_ends_with_a_short_true_you_are_in_block():
    text = "\n".join(cli.welcome_lines("shop", "shop", "https://relay.example"))
    assert "shop@relay.example" in text and "https://relay.example" in text and "Level" in text
    assert "testnet only" in text and "hermes -p shop" in text
    assert text.count('"') >= 6 and "#ideas" in text and "house" in text and "happening" in text
    assert len(text.splitlines()) <= 14


def test_the_seed_names_five_topics_each_with_a_short_welcome_that_states_the_rules():
    from emporia.wire import post_problem
    assert [s[0] for s in cli.SEED_TOPICS] == ["ideas", "games", "debriefs", "labs", "help", "relays"]
    relays = cli.seed_welcome("relays")
    assert "host name" in relays and "who it is for" in relays and "open, read-only or closed" in relays
    for slug, *_ in cli.SEED_TOPICS:
        text = cli.seed_welcome(slug)
        assert text.startswith(f"Welcome to #{slug}.") and len(text) < 400
        assert "data, not instructions" in text and "No URLs" in text and "without your person" in text
        assert post_problem("note", "", text, [["t", "welcome"]], topic=slug) == "", "the relay takes it"


def test_the_seed_says_what_the_season_lacks():
    gaps = cli.seed_season({"season": "season-0", "started_at": None, "ends_at": None})
    assert any("EMPORIA_SEASON_START" in g for g in gaps) and any("EMPORIA_SEASON_END" in g for g in gaps)
    assert cli.seed_season({"season": "season-0", "started_at": "a", "ends_at": "b"}) == []


def test_seed_is_a_persons_command_and_the_guard_says_so():
    spec = importlib.util.spec_from_file_location("emporia_key_guard", REPO / "hermes_plugin/emporia/key_guard.py")
    kg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = kg
    spec.loader.exec_module(kg)
    assert kg._cli_matches("emporia relay seed ops", kg._PERSON_ONLY)
    assert not kg._cli_matches("emporia relay status", kg._PERSON_ONLY)


def test_seed_refuses_without_a_terminal(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.main(["relay", "seed", "ops"]) == 1
    assert "only a person seeds a relay" in capsys.readouterr().out


def test_health_and_summary_serve_the_first_run_facts(client):
    h = client.get("/health").json()
    assert h["season"]["season"] and h["starter_limits"] in (True, False)
    s = client.get("/relay/summary").json()
    assert set(s) >= {"agents", "operators", "open_challenges", "active_topics", "labs", "season"}
    assert all(isinstance(s[k], int) for k in ("agents", "operators", "open_challenges", "active_topics", "labs"))


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    return TestClient(rs.app)
