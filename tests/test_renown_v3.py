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


def test_a_swap_counts_half_so_honest_reciprocity_on_a_small_relay_counts():
    """Two operators who like each other's things: 0 before the softer rule, half now."""
    who = {"me": pg.Who("op-me", 0), "b": pg.Who("op-b", 0)}
    mutual = [like("b", "me", "p1"), like("me", "b", "q1")]
    swap = pg.renown("me", mutual, who, tiers={"op-b": 3})
    assert swap["score"] == 0.62 and pg.RENOWN_WHY["swap"] in swap["why"]       # 1 x 1.25 x 0.5 = 0.625, rounded
    assert pg.renown("me", mutual, who, tiers={"op-b": 2})["score"] == 0.5     # was 0
    assert pg.renown("me", [like("b", "me", "p1")], who, tiers={"op-b": 2})["score"] == 1.0, "no return like, no swap"
    assert "half" in pg.RENOWN_WHY["swap"] and "cancel" not in pg.RENOWN_WHY["swap"]


def test_the_honest_two_operator_relay_is_no_longer_stuck_at_zero():
    who = {"me": pg.Who("op-me", 0), "b": pg.Who("op-b", 0)}
    likes = [like("b", "me", "sess1", "session"), like("me", "b", "sess1", "session")]
    r = pg.renown("me", likes, who, tiers={"op-b": 2})
    assert r["score"] == pg.SWAP_OPERATOR_CAP == 0.75       # a finished game both were in (it was 0)


def test_a_colluding_pair_gets_no_more_than_one_honest_pair_at_its_best():
    who = {"me": pg.Who("op-me", 0), "b": pg.Who("op-b", 0)}
    tiers = {"op-b": 3}
    honest = pg.renown("me", [like("b", "me", "sess1", "session"), like("me", "b", "sess1", "session")], who, tiers=tiers)
    spam = [like("b", "me", f"p{i}") for i in range(60)] + [like("b", "me", f"g{i}", "session") for i in range(60)]
    spam += [like("me", "b", f"q{i}") for i in range(60)]
    colluding = pg.renown("me", spam, who, tiers=tiers)
    assert colluding["score"] <= honest["score"] == pg.SWAP_OPERATOR_CAP
    assert colluding["level"] == 1 and pg.RENOWN_WHY["cap"] in colluding["why"]
    sybil = pg.renown("me", spam, who, tiers={"op-b": 1})                       # the same pair as Newcomer labels
    assert sybil["score"] <= honest["score"]


def test_a_swap_and_a_ring_each_keep_their_own_discount_and_a_liked_back_crowd_stays_at_level_two():
    who = {x: pg.Who(f"op-{x}", 0) for x in ("a", "b", "c")}
    ring = [like("a", "b", "pb"), like("b", "c", "pc"), like("c", "a", "pa"), like("b", "a", "qa")]
    assert pg.renown("a", ring, who)["score"] == 0.75        # c -> a closes a ring (0.25); b <-> a is a swap (0.5)
    names, w2, tiers = labels(18, tier=1)
    back = [like("me", x, "q", "agent") for x in names]                         # every label is liked back: all swaps
    attack = pg.renown("me", [like(x, "me", "stable-me", "agent") for x in names] + back, w2, tiers=tiers)
    assert attack["level"] <= 2 and attack["score"] <= 4.0


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
