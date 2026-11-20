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


def test_the_hash_and_standing_ignore_records_after_the_window_end():
    c = ctx()
    win = {"started_at": "2026-11-01T00:00:00+00:00", "ends_at": "2026-11-10T00:00:00+00:00"}
    games = [game("a", "b", "2026-10-30T10:00:00+00:00", "s1"), game("a", "b", "2026-11-03T10:00:00+00:00", "s2")]

    def frozen(gs):
        per = {x: (E.events(x, games=gs, ctx=c), gs) for x in ("a", "b")}
        inside = E.season_records(per, win)
        return E.records_hash(inside), E.season_standings(inside, c, win, NOW)
    base_hash, base_standing = frozen(games)
    late = games + [game("a", "b", "2026-11-20T10:00:00+00:00", "s3"), game("a", "b", "2026-12-01T10:00:00+00:00", "s4")]
    assert frozen(late) == (base_hash, base_standing), "records after the end change neither the hash nor the standing"
    # the standing is the same at any clock after the end
    per = {x: (E.events(x, games=late, ctx=c), late) for x in ("a", "b")}
    assert E.season_standings(E.season_records(per, win), c, win, NOW + 10**7) == base_standing
    # not vacuous: a record inside the window (or before the start, which feeds the base) changes the hash
    assert frozen(games + [game("a", "b", "2026-11-05T10:00:00+00:00", "s5")])[0] != base_hash
    assert frozen(games + [game("a", "b", "2026-10-29T10:00:00+00:00", "s6")])[0] != base_hash


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
    served = [{k: v for k, v in row.items() if k != "label"} for row in final["standings"]]   # served rows gain words
    # the board serves the top `limit` rows (25); the stored standing keeps everyone
    assert stored["standings"][:len(served)] == served and stored["records_hash"] == final["records_hash"]
    # anyone with the records recomputes it and gets the same standing and the same hash, even with a
    # record added after the window's end (2021)
    per, c, exp = ST.season_inputs()
    for a in [a for a in per if a.endswith(f"_{tag}")]:
        evs, gs = per[a]
        per[a] = (evs + [{"event": "game", "at": "2030-01-01T00:00:00+00:00", "opponents": ["x"], "won": True}],
                  gs + [game(a, "x", "2030-01-01T00:00:00+00:00", f"late-{tag}")])
    again = ST._season_standing_body(ST.progression.season(), per, c, exp, NOW)
    assert again["records_hash"] == final["records_hash"] and again["standings"] == stored["standings"]
    assert again["window"] == final["window"]


def test_review_an_expedition_after_the_end_changes_neither_hash_nor_standing():
    """A week's quest is named by the newest expedition posted before that week ended; with the season ending
    mid-week, one posted after the end must be cut like a late game."""
    from emporia import quest_book as B
    c = ctx()
    win = {"started_at": "2026-11-01T00:00:00+00:00", "ends_at": "2026-11-10T00:00:00+00:00"}   # a Tuesday
    games = [game("a", "b", "2026-11-03T10:00:00+00:00", "s2"), game("a", "b", "2026-11-09T10:00:00+00:00", "s3")]
    per = {x: (E.events(x, games=games, ctx=c), games) for x in ("a", "b")}
    default, _ = E.quest_for_week("2026-W46", [])
    other = next(w["id"] for w in B.WEEKLY if w["id"] != default["id"])
    late = {"post_id": "p1", "created_at": "2026-11-12T10:00:00+00:00", "content": f"Title\nquest: {other}"}
    assert E.quest_for_week("2026-W46", [late])[1] == "expedition", "uncut, the late post names the last week's quest"

    def frozen(exp):
        cut = E.season_expeditions(exp, win)
        return E.records_hash(E.season_records(per, win), cut), E.season_standings(E.season_records(per, win), c, win,
                                                                                 NOW, cut)
    assert frozen([late]) == frozen([])
    early = {**late, "post_id": "p0", "created_at": "2026-11-04T10:00:00+00:00"}
    assert frozen([early])[0] != frozen([])[0], "an expedition inside the window is covered by the hash"


def test_audit_f4_the_final_is_computed_outside_the_database_lock_and_season_is_kept(monkeypatch):
    """The first read after the end computed the final while holding the database lock, on the event loop. It is
    computed in a thread with the lock free, and /season is kept for EMPORIA_PUBLIC_CACHE_SECONDS like the rest."""
    client = TestClient(rs.app)
    tag = uuid.uuid4().hex[:6]
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_SEASON", f"season-{tag}")
    monkeypatch.setenv("EMPORIA_SEASON_START", "2020-01-01T00:00:00+00:00")
    monkeypatch.setenv("EMPORIA_SEASON_END", "2021-01-01T00:00:00+00:00")
    held = []
    real = ST.season_inputs

    def spy():
        held.append(rs.DB_LOCK._is_owned())
        return real()
    monkeypatch.setattr(ST, "season_inputs", spy)
    assert client.get("/season/board").json()["final"] is True
    assert held == [False], "computed with the lock free"

    monkeypatch.setenv("EMPORIA_PUBLIC_CACHE_SECONDS", "30")
    ST._PUBLIC_CACHE.clear()
    calls = []
    real_body = ST._season_body
    monkeypatch.setattr(ST, "_season_body", lambda limit: calls.append(limit) or real_body(limit))
    first = client.get("/season").json()
    assert client.get("/season").json() == first and calls == [25]
    ST._PUBLIC_CACHE.clear()


def test_audit_f10_a_label_change_to_one_of_the_same_length_moves_the_craft_cache_key():
    tag = uuid.uuid4().hex[:6]
    a = f"lk_a_{tag}"
    _register(a)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", ("op_" + "a" * 26, a))
        before = ST._agents_key(conn)
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", ("op_" + "b" * 26, a))
        after = ST._agents_key(conn)
        conn.execute("UPDATE authorized_agents SET operator_label = '' WHERE agent_id = ?", (a,))
    assert before != after and before[0] == after[0]
