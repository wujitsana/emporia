"""progression:v2 — review pass 2, P1, decided 11-06: a non-zero-sum game's rating (Bargain) keeps its
own ladder and board but stays out of the Craft level and the class."""
from emporia import progression as pg
from emporia.module_sdk import MODULE_REGISTRY

BARGAIN = "emporia:bargain:v1"
DAY = 86_400.0
START = 1_790_000_000.0


def _fair_bargains(days=14, per_pair_per_day=3):
    """Three operators trading ordinary 7–7 deals, three times a pair a day (review pass 2's F10 setup)."""
    agents = ["op_a", "op_b", "op_c"]
    pairs = [(a, b) for i, a in enumerate(agents) for b in agents[i + 1:]]
    games, n = [], 0
    for d in range(days):
        for a, b in pairs:
            for k in range(per_pair_per_day):
                n += 1
                games.append({"session_id": f"s{n:05d}", "module_type": BARGAIN, "participants": [a, b],
                              "winner": None, "termination": "deal", "rating_scores": {a: 0.7, b: 0.7},
                              "ended_at": START + d * DAY + k * 600})
    who = {a: pg.Who(operator=f"nous:{a}", verified=True, registered_at=START - 60 * DAY) for a in agents}
    return agents, games, who


def test_bargain_is_marked_non_zero_sum_and_the_set_plays_are_not():
    assert MODULE_REGISTRY[BARGAIN].ZERO_SUM is False
    assert pg.non_zero_sum() == frozenset({BARGAIN})
    assert MODULE_REGISTRY["emporia:blotto:v2"].ZERO_SUM is True


def test_fourteen_days_of_fair_bargains_leave_the_craft_level_unchanged():
    agents, games, who = _fair_bargains()
    table = pg.craft_ratings(games, who)
    now = START + 15 * DAY
    for a in agents:
        sheet = pg.base_sheet(a, table=table, deals=[], abandoned=[], registered_at=START - 60 * DAY,
                              now=now, who=who)
        bargain = next(c for c in sheet["craft"] if c["module_type"] == BARGAIN)
        assert bargain["rating"] > 1800 and not bargain["in_level"], "the ladder keeps its (drifting) rating"
        assert sheet["levels"]["craft"] == 1 and sheet["class"] != "Strategist", a
        assert sheet["formula"] == pg.SHEET_FORMULA == "progression:v2"
        old = pg.base_sheet(a, table=table, deals=[], abandoned=[], registered_at=START - 60 * DAY, now=now,
                            who=who, outside_level=frozenset())
        assert old["levels"]["craft"] >= 6, "under v1 the same play made a high-level Strategist (F10)"
    assert [r["agent_id"] for r in pg.ladder(table, BARGAIN, now)], "Bargain's own board stays"


def test_a_zero_sum_game_still_sets_the_level():
    a, b = "z_a", "z_b"
    who = {x: pg.Who(operator=f"nous:{x}", verified=True, registered_at=START - 60 * DAY) for x in (a, b)}
    games = [{"session_id": f"g{i}", "module_type": "emporia:blotto:v2", "participants": [a, b], "winner": a,
              "ended_at": START + i * DAY} for i in range(30)]
    table = pg.craft_ratings(games, who)
    sheet = pg.base_sheet(a, table=table, deals=[], abandoned=[], registered_at=START - 60 * DAY,
                          now=START + 31 * DAY, who=who)
    assert all(c["in_level"] for c in sheet["craft"])
