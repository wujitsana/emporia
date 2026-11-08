"""season:v1/xp — XP earned in the season's window, the live board, and the signed, frozen final standing."""
import json
import uuid

from fastapi.testclient import TestClient

from emporia import quest_engine as E
from emporia import relay_server as rs
from emporia.routes import standing as ST
from tests.test_progression import _register

NOW = 1_794_000_000
OPS = {"a": "label:A", "b": "label:B"}


def ctx():
    return E.Ctx(operator=lambda x: OPS.get(x, x))


def game(a, b, at, sid):
    return {"session_id": sid, "module_type": "emporia:blotto:v2", "participants": [a, b], "ended_at": at,
            "winner": None, "termination": "rounds"}


def test_season_xp_counts_only_records_inside_the_window():
    c = ctx()
    games = [game("a", "b", "2026-10-30T10:00:00+00:00", "s1"), game("a", "b", "2026-11-03T10:00:00+00:00", "s2"),
             game("a", "b", "2026-11-20T10:00:00+00:00", "s3")]
    evs = E.events("a", games=games, ctx=c)
    win = {"started_at": "2026-11-01T00:00:00+00:00", "ends_at": "2026-11-10T00:00:00+00:00"}
    inside = E.season_row("a", evs, games, c, win, now=NOW)
    everything = E.season_row("a", evs, games, c, {}, now=NOW)
    before = E.season_row("a", evs, games, c, {"ends_at": win["started_at"]}, now=NOW)
    assert 0 < inside["xp"] <= everything["xp"]
    assert before["xp"] > 0, "the first game opened quests before the window"
    assert E.season_row("a", evs, games, c, {"started_at": "2026-12-01T00:00:00+00:00"}, now=NOW)["xp"] == 0
    # the whole history split at the window's edges adds up: before + inside + after = everything
    after = E.season_row("a", evs, games, c, {"started_at": win["ends_at"]}, now=NOW)
    assert before["xp"] + inside["xp"] + after["xp"] == everything["xp"]


def test_standings_and_the_records_hash_do_not_depend_on_order():
    c = ctx()
    games = [game("a", "b", "2026-11-03T10:00:00+00:00", "s2")]
    per = {"a": (E.events("a", games=games, ctx=c), games), "b": (E.events("b", games=games, ctx=c), games)}
    win = {"started_at": "2026-11-01T00:00:00+00:00", "ends_at": "2026-11-10T00:00:00+00:00"}
    one = E.season_standings(per, c, win, NOW)
    flipped = dict(reversed(list(per.items())))
    assert one == E.season_standings(flipped, c, win, NOW) and E.records_hash(per) == E.records_hash(flipped)
    assert [r["rank"] for r in one] == [1, 2] and one[0]["season_xp"] >= one[1]["season_xp"]


def test_the_final_standing_is_signed_frozen_and_recomputes(monkeypatch):
    client = TestClient(rs.app)
    tag = uuid.uuid4().hex[:6]
    for x in (f"sb_a_{tag}", f"sb_b_{tag}"):
        _register(x)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_SEASON", f"season-{tag}")
    monkeypatch.setenv("EMPORIA_SEASON_START", "2020-01-01T00:00:00+00:00")
    monkeypatch.setenv("EMPORIA_SEASON_END", "2999-01-01T00:00:00+00:00")
    live = client.get("/season/board").json()
    assert live["final"] is False and live["formula"] == E.SEASON_FORMULA and "signature" not in live
    monkeypatch.setenv("EMPORIA_SEASON_END", "2021-01-01T00:00:00+00:00")
    final = client.get("/season/board").json()
    assert final["final"] is True and final["signature"] and final["season"] == f"season-{tag}"
    assert set(final) >= {"season", "formula", "window", "records_hash", "standings"}
    assert client.get("/season/board").json() == final, "frozen: served again identically"
    with rs.DB_LOCK, rs.get_db() as conn:
        stored = json.loads(conn.execute("SELECT body_json FROM season_finals WHERE season = ?",
                                         (f"season-{tag}",)).fetchone()[0])
    assert stored["standings"] == final["standings"] and stored["records_hash"] == final["records_hash"]
    # anyone with the records recomputes it and gets the same standing and the same hash
    per, c, exp = ST.season_inputs()
    again = ST._season_standing_body(ST.progression.season(), per, c, exp, NOW)
    assert again["records_hash"] == final["records_hash"] and again["standings"] == stored["standings"]
    assert again["window"] == final["window"]
