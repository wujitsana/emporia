"""quests:v8 play XP (`play:v1`): 2 XP each for a finished public game against another operator's agent,
capped, and the farms it must fail. Pure: records in, XP out."""
import random

from emporia import quest_engine as E

NOW = 1_794_000_000          # 2026-11-06 UTC
DAY = "2026-11-06"
OPS = {"me": "label:ME", "mine2": "label:ME", "mine3": "label:ME", "b1": "label:B", "c1": "label:C", "d1": "label:D",
       "e1": "label:E", "f1": "label:F", "g1": "label:G", "h1": "label:H", "k1": "k1", "k2": "k2", "house": "label:HOUSE"}
_n = [0]


def ctx():
    return E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}))


def g(a, b, at=f"{DAY}T10:00:00+00:00", termination="closest", **kw):
    _n[0] += 1
    return {"session_id": f"s{_n[0]:05d}", "participants": [a, b], "ended_at": at, "termination": termination, **kw}


def play(games, agent="me", c=None):
    return E.play_xp(agent, games, c or ctx(), NOW)


def test_a_finished_game_against_another_operator_pays_two_each_win_or_lose():
    won, lost = g("me", "b1", winner="me"), g("me", "c1", winner="c1")
    assert play([won, lost])["xp"] == 4
    assert play([won], agent="b1")["xp"] == 2          # the loser is paid too
    one = play([g("me", "b1")])
    assert one["today"] == 2 and one["week"] == 2


def test_it_adds_to_xp_and_never_lowers_it():
    c = ctx()
    games = [g("me", "b1")]
    base = E.evaluate("me", [], c, now=NOW)
    out = E.evaluate("me", [], c, now=NOW, play_games=games)
    assert out["xp"] == base["xp"] + 2 and out["play"]["xp"] == 2 and out["formula"] == "quests:v8"


def test_xp_with_play_is_never_below_xp_without_it_over_generated_records():
    rng = random.Random(11)
    people = [a for a in OPS if a != "me"]
    c = ctx()
    for _ in range(150):
        games = []
        for _ in range(rng.randint(0, 12)):
            pair = rng.sample(["me", *people[:4]], 2) if rng.random() < .3 else ["me", rng.choice(people)]
            games.append(g(*pair, at=f"2026-11-0{rng.randint(1, 6)}T1{rng.randint(0, 9)}:00:00+00:00",
                           termination=rng.choice(["closest", "deal", "void", "forfeit", "timeout", "tie", None]),
                           refused_by=rng.choice([None, "me", "b1"])))
        evs = E.events("me", games=games, ctx=c)
        assert E.evaluate("me", evs, c, now=NOW, play_games=games)["xp"] >= E.evaluate("me", evs, c, now=NOW)["xp"]


# ── farms ───────────────────────────────────────────────────────────────────

def test_farm_own_operators_agents_pay_nothing():
    assert play([g("me", "mine2") for _ in range(5)])["xp"] == 0


def test_farm_the_house_pays_nothing():
    assert play([g("me", "house") for _ in range(5)])["xp"] == 0


def test_farm_one_agent_stops_at_ten_a_day():
    ops = ["b1", "c1", "d1", "e1", "f1", "g1", "h1"]
    many = [g("me", o, at=f"{DAY}T{h:02d}:00:00+00:00") for h, o in enumerate(ops)]
    out = play(many)
    assert out["xp"] == 10 and out["today"] == 10 and out["day_cap"] == 10, "five games a day for one agent"


def test_farm_many_agents_of_one_operator_stop_at_twenty_in_completion_order():
    ops = ["b1", "c1", "d1", "e1", "f1"]
    games = [g(a, o, at=f"{DAY}T{i:02d}:{j:02d}:00+00:00") for i, a in enumerate(["me", "mine2", "mine3"])
             for j, o in enumerate(ops)]
    got = {a: play(games, agent=a)["xp"] for a in ("me", "mine2", "mine3")}
    assert got == {"me": 10, "mine2": 10, "mine3": 0}, "completion order decides who gets the cap"
    out = play(games)
    assert out["operator_today"] == 20 and out["operator_cap"] == 20


def test_farm_repeat_games_against_one_operator_pay_three_a_week():
    games = [g("me", "b1", at=f"2026-11-0{d}T10:00:00+00:00") for d in (2, 3, 4, 5)]     # one ISO week (W45)
    assert play(games)["xp"] == 6
    assert play([*games, g("me", "b1", at="2026-11-09T10:00:00+00:00")])["xp"] == 8, "a new ISO week pays again"
    assert play(games, agent="b1")["xp"] == 6, "the same cap binds the other side"


def test_farm_abandoned_voided_and_late_games_pay_nothing():
    for t in ("void", "late_commit", "unrevealed", "no_reveals", "collision", "timeout", None):
        assert play([g("me", "b1", termination=t)])["xp"] == 0, t
    assert play([g("me", "b1", abandoned_by="b1")])["xp"] == 0


def test_a_forfeit_pays_the_player_who_revealed_not_the_refuser():
    f = g("me", "b1", termination="forfeit", refused_by="b1")
    assert play([f])["xp"] == 2
    assert play([f], agent="b1")["xp"] == 0
    assert play([g("me", "b1", termination="forfeit")])["xp"] == 0, "a forfeit that names no refuser pays nobody"
