"""Games take no stakes of any kind: no money wagers, no staked XP, no Nerve (architecture-v2 §6).

Money rides on deals and paid entry only: no prize, no bounty on a game either. A game created with a price or a payment mode is refused,
and no game's rules or play guide speaks of stakes, wagers, refunds or a money pot.
"""
import re
import uuid

import pytest
from fastapi.testclient import TestClient

import emporia.relay_server  # noqa: F401
from emporia import playbook
from emporia.module_sdk import MODULE_REGISTRY, list_available_modules, load_bundled_modules
from emporia.relay_server import app
from tests.test_emporia import _register_keyed

load_bundled_modules()
GAMES = sorted(mt for mt, cls in MODULE_REGISTRY.items()
               if mt.startswith("emporia:") and cls.PRICE_MODEL is None
               and mt not in ("emporia:research:v1", "emporia:code-review:v1", "emporia:rand:v1"))


def _creator(client):
    a = f"ns_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    return a, {"x-emporia-agent-id": a}


@pytest.mark.parametrize("rules", [
    {"mode": "x402", "price": "1.00", "currency": "usd"},
    {"mode": "x402", "price": "1.00", "currency": "usd"},
    {"mode": "tempo", "price": "0.50", "currency": "usd"},
    {"mode": "x402", "price": "0"},
])
def test_creating_a_game_with_a_stake_is_refused(rules):
    client = TestClient(app, client=("127.0.0.1", 51230))
    a, headers = _creator(client)
    for module in ("emporia:blotto:v2", "emporia:chess:v1"):
        r = client.post("/sessions", json={"module_type": module, "creator_agent_id": a, "payment_rules": rules},
                        headers=headers)
        assert r.status_code == 400, (module, r.text)
        assert "takes no stakes" in r.text and "no money, no XP, no Nerve" in r.text


def test_a_free_game_is_created_and_joined_with_nothing_held():
    client = TestClient(app, client=("127.0.0.1", 51231))
    a, ha = _creator(client)
    made = client.post("/sessions", json={"module_type": "emporia:blotto:v2", "creator_agent_id": a}, headers=ha)
    assert made.status_code == 200, made.text
    assert made.json()["status"] == "waiting"


def test_a_two_player_game_never_waits_for_funding():
    from emporia.routes import sessions as s
    assert not hasattr(s, "_fund_wager") and not hasattr(s, "_settle_wager") and not hasattr(s, "_wager_rail")


def test_the_listing_carries_no_stake_model_for_a_game():
    listed = {m["module_type"]: m for m in list_available_modules()}
    assert "stakes_allowed" not in listed["emporia:blotto:v2"]
    for game in GAMES:
        assert listed[game]["price_model"] is None, game


def test_the_operator_settings_that_allowed_game_stakes_are_gone():
    from emporia import relay_config, relay_server
    names = {s.name for s in relay_config.SETTINGS}
    assert "EMPORIA_CARD_GAME_STAKES" not in names and "EMPORIA_ENGINE_GAME_STAKES" not in names
    assert not hasattr(relay_server, "card_game_stakes_allowed")


def test_the_playbook_has_no_stake_limit():
    assert "max_stake_cents" not in playbook.PRESETS["player"].as_dict()
    assert "EMPORIA_PLAYBOOK_MAX_STAKE_CENTS" not in playbook.SETTINGS


_MONEY_WORDS = re.compile(r"\b(stakes?|staked|staking|wagers?|refund(?:s|ed)?|pot)\b", re.I)


@pytest.mark.parametrize("game", GAMES)
def test_a_games_texts_say_nothing_of_stakes_wagers_or_a_money_pot(game):
    cls = MODULE_REGISTRY[game]
    for name, text in (("RULES", cls.RULES), ("HOW_TO_PLAY", cls.HOW_TO_PLAY)):
        found = [m.group(0) for m in _MONEY_WORDS.finditer(text)]
        if game == "emporia:cv-auction:v1":
            # the auction's "pot" is the common value V, a points concept, never money
            found = [w for w in found if w.lower() != "pot"]
            assert "pot" in text, "the auction's pot (the common value V) stays"
        # "no stakes" is the one allowed mention: a statement that there are none
        found = [w for w in found if not (w.lower().startswith("stake")
                                          and re.search(r"\bno stakes\b", text, re.I))]
        assert not found, f"{game} {name} speaks of money: {found}"


def test_the_auctions_pot_is_points_not_money():
    cls = MODULE_REGISTRY["emporia:cv-auction:v1"]
    text = cls.RULES + cls.HOW_TO_PLAY
    assert "V" in text and not re.search(r"\b(usd|dollars?|cents|usdc|\$)\b", text, re.I)


def test_the_frames_say_nothing_of_stakes():
    import inspect

    from emporia.plugins.platforms.emporia import frames
    src = inspect.getsource(frames)
    assert "wager" not in src.lower() and "stakes released" not in src.lower()


# ── no bounties on games either (2026-11-07) ──────────────────────────────────

def test_there_is_no_bounty_route():
    client = TestClient(app, client=("127.0.0.1", 51232))
    a, headers = _creator(client)
    r = client.post("/bounties", json={"title": "Prize", "description": "d", "prize_cents": 1000,
                                       "module_type": "emporia:chess:v1", "rail": "x402"}, headers=headers)
    assert r.status_code == 404
    assert client.get("/bounties").status_code == 404


def test_the_bounty_tools_settings_and_tables_are_gone():
    import importlib.util

    from emporia import mcp_server, relay_config, relay_server, schema_sql
    assert importlib.util.find_spec("emporia.bounties") is None
    assert importlib.util.find_spec("emporia.routes.bounties") is None
    for tool in ("post_bounty", "enter_bounty", "list_bounties", "start_bounty", "cancel_bounty",
                 "withdraw_bounty_entry", "settle_promise"):
        assert not hasattr(mcp_server, tool), tool
    names = {s.name for s in relay_config.SETTINGS}
    assert not names & {"EMPORIA_CARD_BOUNTY_PRIZES", "EMPORIA_ENGINE_GAME_BOUNTIES"}
    assert not hasattr(relay_server, "card_bounty_prizes_allowed")
    assert "bounty" not in schema_sql.SCHEMA_SQL.lower()


_PRIZE_WORDS = re.compile(r"\b(bount(?:y|ies)|prizes?)\b", re.I)


@pytest.mark.parametrize("game", GAMES)
def test_a_games_texts_say_nothing_of_prizes_or_bounties(game):
    cls = MODULE_REGISTRY[game]
    for name, text in (("RULES", cls.RULES), ("HOW_TO_PLAY", cls.HOW_TO_PLAY)):
        assert not _PRIZE_WORDS.findall(text), f"{game} {name} speaks of prizes or bounties"
