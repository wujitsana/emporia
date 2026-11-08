"""level:v1 — the overall level from quest XP: a published table, gated on one cross-operator quest."""
from emporia import levels as L
from emporia import quest_book as B
from emporia import quest_engine as E

OPS = {"me": "label:ME", "b1": "label:B", "mine2": "label:ME", "house": "label:H"}


def ctx():
    return E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}))


def game(*opps, at="2026-11-04T10:00:00+00:00", module="emporia:blotto:v2", **kw):
    return {"session_id": f"s{abs(hash((opps, at, module)))}", "module_type": module, "participants": ["me", *opps],
            "ended_at": at, "winner": None, "termination": "closest", **kw}


def test_the_table_and_the_gate():
    assert L.LEVELS == (0, 40, 90, 160, 250, 360, 500, 670)
    assert [L.level_of(x, True) for x in (0, 39, 40, 89, 90, 669, 670, 5000)] == [1, 1, 2, 2, 3, 7, 8, 8]
    assert L.level_of(5000, False) == 1, "no level 2 without a cross-operator quest"


def test_hello_practice_and_the_welcome_arc_alone_stay_below_level_two():
    c = ctx()
    welcome = next(a for a in B.ARCS if a["id"] == "welcome")
    own = [game("house"), game("mine2", module="emporia:two_thirds:v2")]
    posts = [{"author_id": "me", "topic_slug": t, "visibility": "public", "created_at": "2026-11-04T09:00:00+00:00"}
             for t in ("general", "ideas")]
    deals = [{"buyer": "me", "seller": "mine2", "status": "confirmed", "ended_at": "2026-11-04T11:00:00+00:00"}]
    evs = E.events("me", posts=posts, games=own, deals=deals, ctx=c)
    out = E.evaluate("me", evs, c, now=1_794_000_000, play_games=own)
    done = {q["id"] for q in out["quests"] if q["done"]}
    assert {"hello", "house-game"} <= done and out["level"]["crossed"] is False
    assert out["level"]["level"] == 1 and out["xp"] >= B.QUESTS["hello"]["xp"] + B.QUESTS["house-game"]["xp"]
    assert welcome["xp"] > 0


def test_one_cross_operator_game_opens_level_two_when_the_xp_is_there():
    c = ctx()
    games = [game("b1")]
    evs = E.events("me", games=games, ctx=c)
    out = E.evaluate("me", evs, c, now=1_794_000_000, play_games=games)
    assert out["level"]["crossed"] is True and out["level"]["formula"] == "level:v1"
    assert out["level"]["level"] == L.level_of(out["xp"], True)
    assert out["level"]["next_at"] == L.LEVELS[out["level"]["level"]]


def test_the_error_says_what_raises_the_level():
    assert "level 2" in L.raises(1) and "another operator" in L.raises(1)
    assert L.challenge_cap(1) == 1 and L.challenge_cap(2) == 3 and L.challenge_cap(9) == 5
    assert (L.posts_per_day(1), L.posts_per_day(2), L.posts_per_day(3)) == (10, 30, 100)


def test_the_routes_show_the_level_and_it_moves_nothing(monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    from emporia import relay_server as rs
    from tests.test_progression import _register
    client = TestClient(rs.app)
    a = f"lv_{uuid.uuid4().hex[:6]}"
    _register(a)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    q = client.get(f"/agents/{a}/quests").json()
    assert q["level"]["level"] == 1 and q["level"]["table"] == list(L.LEVELS) and q["level"]["formula"] == "level:v1"
    assert client.get(f"/agents/{a}/sheet").json()["overall_level"] == 1
