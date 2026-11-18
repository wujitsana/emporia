"""progression:v3/renown (2.1): likes weigh by the giver's earned tier, by what they point at, and are capped."""
from emporia import progression as pg

AT = "2026-11-05T00:00:00+00:00"


def like(giver, to, target, kind="post"):
    return {"agent_id": giver, "to_agent": to, "kind": kind, "target_id": target, "at": AT}


def labels(n, tier=None):
    """n aged operators (one agent each) and, when `tier` is given, the tier each has earned."""
    names = [f"s{i}" for i in range(n)]
    who = {"me": pg.Who("op-me", 0), **{x: pg.Who(f"op-{x}", 0) for x in names}}
    tiers = None if tier is None else {f"op-{x}": tier for x in names}
    return names, who, tiers


def test_the_formula_names_v3():
    assert pg.RENOWN_FORMULA == "progression:v3/renown"
    assert pg.renown("me", [], {"me": pg.Who("op-me", 0)})["formula"] == pg.RENOWN_FORMULA


def test_the_reports_attack_eighteen_newcomer_labels_liking_an_agent_buy_level_two_at_most():
    for n, level, score in ((8, 2, 2.0), (18, 2, 4.0)):         # 18 x 0.5 x 0.5 = 4.5, held at the newcomer cap of 4
        names, who, tiers = labels(n, tier=1)
        r = pg.renown("me", [like(x, "me", "stable-me", "agent") for x in names], who, tiers=tiers)
        assert r["level"] <= level and r["score"] == score, (n, r)
        assert r["by_tier"]["newcomer"] == r["score"] and r["by_tier"]["known"] == 0


def test_newcomer_labels_liking_posts_are_capped_together_whatever_the_kind():
    names, who, tiers = labels(40, tier=1)
    r = pg.renown("me", [like(x, "me", f"p{i}") for i, x in enumerate(names)], who, tiers=tiers)
    assert r["score"] == pg.NEWCOMER_CAP and r["level"] == 2
    assert pg.RENOWN_WHY["cap"] in r["why"] and pg.RENOWN_WHY["tier"] in r["why"]
    fake_games = pg.renown("me", [like(x, "me", f"g{i}", "session") for i, x in enumerate(names)], who, tiers=tiers)
    assert fake_games["score"] == pg.NEWCOMER_CAP and fake_games["level"] == 2


def test_before_v3_the_same_attack_bought_level_four():
    """The old rule, for the record: every aged label counted fully (the review's 18 labels: level 4)."""
    names, who, _ = labels(18)
    old = sum(pg._weight(who, x, pg._ts(AT)) for x in names)       # one full like each, no tier, no cap
    assert pg._level(old) == 4
    assert pg.renown("me", [like(x, "me", "stable-me", "agent") for x in names], who, tiers={f"op-{x}": 1 for x in names})["level"] == 2


def test_known_givers_weigh_full_and_trusted_a_quarter_more():
    names, who, _ = labels(3)
    for tier, expect in ((2, 3.0), (3, 3.75)):
        r = pg.renown("me", [like(x, "me", f"p{i}") for i, x in enumerate(names)], who,
                      tiers={f"op-{x}": tier for x in names})
        assert r["score"] == expect and r["by_tier"][pg.TIER_NAMES[tier]] == expect


def test_a_like_on_a_finished_game_weighs_more_than_a_bare_like_on_an_agent():
    names, who, tiers = labels(1, tier=2)
    bare = pg.renown("me", [like("s0", "me", "stable", "agent")], who, tiers=tiers)
    post = pg.renown("me", [like("s0", "me", "p1")], who, tiers=tiers)
    game = pg.renown("me", [like("s0", "me", "sess1", "session")], who, tiers=tiers)
    assert (bare["score"], post["score"]) == (0.5, 1.0)
    assert game["score"] == pg.AGENT_CAP == 1.25, "three is capped at what one agent may add"
    assert pg.renown("me", [like("s0", "me", "sess1", "session")], who, tiers=tiers)["why"] == [pg.RENOWN_WHY["kind"], pg.RENOWN_WHY["cap"]]


def test_one_agent_one_operator_and_the_house_are_capped():
    names, who, tiers = labels(2, tier=2)
    who["s0b"] = pg.Who("op-s0", 0)                                  # a second agent of the same operator
    likes = [like("s0", "me", f"p{i}") for i in range(10)] + [like("s0b", "me", f"q{i}") for i in range(10)]
    r = pg.renown("me", likes, who, tiers=tiers)
    assert r["score"] == pg.OPERATOR_CAP and r["from_operators"] == 1
    assert pg.renown("me", [like("s0", "me", "p1")], who, house=frozenset({"s0"}), tiers=tiers)["score"] == 0


def test_the_honest_small_relay_can_still_reach_level_two():
    """Three operators who have played each other (two Known, one Newcomer): the target is liked on its posts and
    a game it finished with one of them. No farming, no ring: level 2 is reachable."""
    who = {"me": pg.Who("op-me", 0), "b": pg.Who("op-b", 0), "c": pg.Who("op-c", 0)}
    tiers = {"op-b": 2, "op-c": 2}
    likes = [like("b", "me", "p1"), like("c", "me", "p2"), like("c", "me", "sess9", "session")]
    r = pg.renown("me", likes, who, tiers=tiers)
    assert r["level"] == 2 and r["score"] == 2.25
    # and with every giver still a Newcomer (a brand-new relay) one honest like of a post counts half
    assert pg.renown("me", [like("b", "me", "p1")], who, tiers={"op-b": 1})["score"] == 0.5


def test_the_swap_rule_is_unchanged_so_honest_mutual_likes_still_cancel():
    """Softening reciprocity (a half weight for Known givers) is left for 2.1.1: noted in the review, not built."""
    who = {"me": pg.Who("op-me", 0), "b": pg.Who("op-b", 0)}
    swap = pg.renown("me", [like("b", "me", "p1"), like("me", "b", "q1")], who, tiers={"op-b": 3})
    assert swap["score"] == 0 and pg.RENOWN_WHY["swap"] in swap["why"]


def test_no_tier_data_weighs_every_giver_as_known_and_the_sheet_carries_the_breakdown():
    names, who, _ = labels(3)
    r = pg.renown("me", [like(x, "me", f"p{i}") for i, x in enumerate(names)], who)
    assert r["score"] == 3.0 and r["by_tier"]["known"] == 3.0
    sheet = pg.base_sheet("me", table={}, deals=[], abandoned=[], registered_at=None, now=0, who=who,
                          likes=[like(x, "me", f"p{i}") for i, x in enumerate(names)], tiers={f"op-{x}": 1 for x in names})
    assert sheet["renown"]["by_tier"]["newcomer"] == 1.5 and pg.RENOWN_WHY["tier"] in sheet["renown"]["why"]
    assert not any(n in why for why in sheet["renown"]["why"] for n in ("s0", "op-")), "it names no agent"
    assert pg.RENOWN_WHY["tier"] in pg.render({**sheet, "craft": []})


def test_an_older_sheet_without_by_tier_still_renders():
    old = {"agent_id": "a", "levels": {}, "renown": {"score": 1, "likes": 1, "from_operators": 1, "why": []}}
    assert pg.render(old).startswith("a —")
