"""The relay's quest knobs, starter and rate limits per operator, and records carried from another relay.

XP, levels and boards are of one relay: "N XP on relay R". An operator tunes them within bounds (published on the
signed relay card); a record carried from another relay is history, shown as that relay's word and never added
to anything computed here.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import levels as L
from emporia import quest_book as B
from emporia import quest_engine as E
from emporia import quest_knobs as K
from emporia import relay_server as rs
from emporia.routes import standing as ST
from tests.test_challenges import _agents, _challenge
from tests.test_emporia import _register_keyed
from tests.test_posts_v2 import _agent, _post


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51490))


@pytest.fixture(autouse=True)
def quests_on(monkeypatch):
    for name in ("EMPORIA_QUESTS", "EMPORIA_XP_MULTIPLIER", "EMPORIA_XP_DAILY_CAP", "EMPORIA_STARTER_POSTS_PER_DAY",
                 "EMPORIA_STARTER_CHALLENGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    ST.forget_levels()
    yield
    ST.forget_levels()


# ── the knobs ────────────────────────────────────────────────────────────────────────────────────

OPS = {"me": "nous:ME", "b1": "nous:B"}


def _ctx():
    return E.Ctx(operator=lambda a: OPS.get(a, a), verified=lambda a: True)


def _xp() -> int:
    c = _ctx()
    games = [{"session_id": "s1", "module_type": "emporia:blotto:v2", "participants": ["me", "b1"],
              "ended_at": "2026-11-04T10:00:00+00:00", "winner": None, "termination": "closest"}]
    evs = E.events("me", games=games, ctx=c)
    return E.evaluate("me", evs, c, now=1_794_000_000, play_games=games)["xp"]


def test_the_multiplier_scales_xp_and_is_clamped_to_half_to_double(monkeypatch):
    base = _xp()
    assert base > 0
    monkeypatch.setenv("EMPORIA_XP_MULTIPLIER", "2")
    assert _xp() == base * 2
    monkeypatch.setenv("EMPORIA_XP_MULTIPLIER", "50")
    assert K.xp_multiplier() == 2.0 and _xp() == base * 2
    monkeypatch.setenv("EMPORIA_XP_MULTIPLIER", "0.01")
    assert K.xp_multiplier() == 0.5 and _xp() == round(base * 0.5)
    monkeypatch.setenv("EMPORIA_XP_MULTIPLIER", "nonsense")
    assert K.xp_multiplier() == 1.0


def test_the_daily_cap_limits_xp_earned_in_a_utc_day(monkeypatch):
    base = _xp()
    monkeypatch.setenv("EMPORIA_XP_DAILY_CAP", "10")
    assert 0 < _xp() < base and K.scaled_xp([("2026-11-04", 50), ("2026-11-04", 50), ("2026-11-05", 4)]) == 14


def test_quests_off_removes_the_engine_and_the_starter_limits_that_hang_on_it(monkeypatch):
    from emporia.progression import gamify_parts
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    assert "quests" in gamify_parts() and L.starter_limits_on()
    monkeypatch.setenv("EMPORIA_QUESTS", "off")
    assert "quests" not in gamify_parts() and not L.starter_limits_on()


def test_the_starter_limits_per_level_are_settings(monkeypatch):
    monkeypatch.setenv("EMPORIA_STARTER_POSTS_PER_DAY", "2,4,8")
    monkeypatch.setenv("EMPORIA_STARTER_CHALLENGES", "2,2,2")
    assert (L.posts_per_day(1), L.posts_per_day(2), L.posts_per_day(5)) == (2, 4, 8)
    assert L.challenge_cap(1) == 2
    monkeypatch.setenv("EMPORIA_STARTER_POSTS_PER_DAY", "x,y")
    assert L.posts_per_day(1) == 10, "an unreadable setting is the default"


def test_the_card_publishes_the_knobs_next_to_the_formulas(client, monkeypatch):
    from emporia.identity import verify_relay_card
    monkeypatch.setenv("EMPORIA_XP_MULTIPLIER", "1.5")
    monkeypatch.setenv("EMPORIA_XP_DAILY_CAP", "120")
    monkeypatch.setenv("EMPORIA_SEASON", "season-0")
    card = client.get("/relay/card").json()
    gamify = card["relay_modules"]["gamify"]
    assert "quests:v6" in gamify["formulas"]
    knobs = gamify["knobs"]
    assert knobs["quests"] is True and knobs["xp_multiplier"] == 1.5 and knobs["xp_daily_cap"] == 120
    assert knobs["xp_multiplier_range"] == [0.5, 2.0] and knobs["scope"] == "this relay only"
    assert knobs["starter_limits"]["posts_per_day"] == [10, 30, 100] and knobs["season"]["season"] == "season-0"
    assert verify_relay_card(card)["ok"], "the knobs are inside the relay's signature"


def test_xp_levels_and_boards_are_labelled_per_relay(client):
    a = _agent(client, "kn")
    q = client.get(f"/agents/{a}/quests").json()
    assert q["label"].endswith(f"on relay {rs._own_host()}") and "XP" in q["label"] and q["scope"] == "this_relay"
    assert q["level"]["scope"] == "this relay"
    board = client.get("/season/board").json()
    assert board["scope"] == "this_relay"


# ── per operator, not just per agent ─────────────────────────────────────────────────────────────

_ONE_OP = "op_" + "a" * 26


def _same_operator(*agents):
    with rs.DB_LOCK, rs.get_db() as conn:
        for a in agents:
            conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", (_ONE_OP, a))


def test_an_operators_agents_share_one_post_allowance(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    monkeypatch.setenv("EMPORIA_STARTER_POSTS_PER_DAY", "3,3,3")
    a, b = _agent(client, "po"), _agent(client, "po")
    _same_operator(a, b)
    assert ST.operator_agents(a) == sorted({a, b})
    for who in (a, a, b):
        assert _post(client, who).status_code == 200
    r = _post(client, a)
    assert r.status_code == 429 and "operator's other agents" in r.text
    assert _post(client, b).status_code == 429, "the second agent is not a second allowance"
    other = _agent(client, "po")
    assert _post(client, other).status_code == 200, "a different operator has its own"


def test_an_operators_agents_share_the_waiting_challenge_allowance(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    a, b = _agents(client, "oa", "ob")
    _same_operator(a, b)
    assert _challenge(client, a).status_code == 200
    r = _challenge(client, b)
    assert r.status_code == 400 and "at most 1" in r.text
    c, = _agents(client, "oc")
    assert _challenge(client, c).status_code == 200


def test_the_write_rate_limit_is_per_operator_as_well(client, monkeypatch):
    from fastapi import HTTPException
    a, b, c = _agent(client, "rl"), _agent(client, "rl"), _agent(client, "rl")
    _same_operator(a, b, c)
    monkeypatch.setattr(rs.AGENT_REGISTRY, "max_per_minute", 2)
    monkeypatch.setattr(rs, "OPERATOR_RATE_MULTIPLE", 2.0)       # 4 writes a minute for the two together
    rs.AGENT_REGISTRY.rate_limits.clear()
    for who in (a, a, b, b):
        rs._enforce_agent_rate_limit(who)
    with pytest.raises(HTTPException) as e:
        rs._enforce_agent_rate_limit(c)        # its own budget is untouched; its operator's is spent
    assert e.value.status_code == 429 and "operator" in str(e.value.detail)


# ── a record carried from another relay never counts here ───────────────────────────────────────

def _signed_record_from_another_relay(agent_id: str, public_key_hex: str, relay: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia.identity import federation_signable_bytes
    key = ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    record = {"scope": "this_relay", "agent_id": agent_id, "public_key_hex": public_key_hex,
              "games": {"finished": 500, "won": 500, "lost": 0, "drawn": 0, "lost_on_time": 0, "abandoned": 0,
                        "voided": 0},
              "as_buyer": {}, "as_seller": {}, "as_of": "2026-11-01T00:00:00+00:00"}
    sig = key.sign(federation_signable_bytes(relay, "record", [record])).hex()
    return {**record, "relay": relay, "relay_public_key": pub, "signature": sig}


def test_farm_on_one_relay_move_to_another_and_the_new_relay_still_shows_level_one_and_no_xp(client, monkeypatch):
    from tests.test_emporia import bearer_for
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    agent = f"mv_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, agent)
    with rs.DB_LOCK, rs.get_db() as conn:
        key = conn.execute("SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]
    farm = _signed_record_from_another_relay(agent, key, "https://relay-a.example")
    h = bearer_for(client, agent)
    r = client.post(f"/agents/{agent}/carried-record", json={"record": farm}, headers=h)
    assert r.status_code == 200, r.text

    q = client.get(f"/agents/{agent}/quests").json()
    assert q["xp"] == 0 and q["level"]["level"] == 1 and q["label"].startswith("0 XP, level 1 on relay ")
    assert ST.limit_level(agent) == 1, "starter limits bind it as a newcomer"
    assert agent not in [row["agent_id"] for row in client.get("/season/board").json()["standings"]]
    sheet = client.get(f"/agents/{agent}/sheet").json()
    assert sheet["overall_level"] == 1

    record = client.get(f"/agents/{agent}/record").json()
    [elsewhere] = record["elsewhere"]
    assert elsewhere["label"] == "history from https://relay-a.example" and elsewhere["counts_here"] is False
    assert record["games"]["finished"] == 0, "this relay's own count stays its own"
    assert json.dumps(record["elsewhere"]).count("500") >= 1, "the other relay's word is shown, as its word"


def test_the_engine_has_no_input_for_carried_records():
    import inspect
    assert not [p for p in inspect.signature(E.evaluate).parameters if "carr" in p or "elsewhere" in p]
    assert B.FORMULA == E.FORMULA
