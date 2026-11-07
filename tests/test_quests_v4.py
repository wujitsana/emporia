"""quests:v4 — the quest book, its engine, the farms it must fail (docs/proposals/quests-v4.md §4) and
the carry-over from v3. Pure: records in, progress out."""
import random

import pytest

from emporia import quest_book as B
from emporia import quest_engine as E
from emporia import quests as Q

ME = "me"
OPS = {"me": "label:ME", "mine2": "label:ME", "b1": "label:B", "b2": "label:B", "c1": "label:C", "d1": "label:D",
       "e1": "label:E", "k1": "k1", "k2": "k2", "house": "label:HOUSE"}
VERIFIED = {a for a, op in OPS.items() if op.startswith("label:")}


def ctx(others="verified", verified=None, **kw):
    v = VERIFIED if verified is None else verified
    return E.Ctx(operator=lambda a: OPS.get(a, a), verified=lambda a: a in v, house=frozenset({"house"}),
                 others=others, parts=frozenset(kw.pop("parts", ("pledges",))), **kw)


def game(*opps, at="2026-11-04T10:00:00+00:00", module="emporia:blotto:v2", winner=None, **extra):
    return {"session_id": f"s_{random.randrange(10**9)}", "module_type": module, "participants": [ME, *opps],
            "ended_at": at, "winner": winner, **extra}


def run(c=None, now=None, v3=None, **records):
    c = c or ctx()
    evs = E.events(ME, ctx=c, **records)
    return E.evaluate(ME, evs, c, v3=v3, now=now)


def done(out):
    return {q["id"] for q in out["quests"] if q["done"]}


def track(out, tid):
    return next(t for t in out["tracks"] if t["id"] == tid)


def arc(out, aid):
    return next(a for a in out["arcs"] if a["id"] == aid)


# ── the book ────────────────────────────────────────────────────────────────

def test_the_book_is_data_every_step_names_a_known_event_and_every_arc_a_quest():
    events = set(E.EVENTS) | set(E.PENDING_EVENTS)
    for qid, d in B.QUESTS.items():
        names = d["when"]["event"] if isinstance(d["when"]["event"], list) else [d["when"]["event"]]
        assert set(names) <= events, qid
        assert d.get("requires") in (None, *B.REQUIRES), qid
        assert bool(d.get("requires")) == (d.get("enabled") is False), f"{qid}: disabled exactly when it waits"
    for a in B.ARCS:
        assert set(a["steps"]) <= B.QUESTS.keys() and a["pillar"] in B.PILLARS, a["id"]
    for name, feature in E.PENDING_EVENTS.items():
        assert feature in B.REQUIRES and name not in E.EVENTS, name
    book = B.book()
    assert book["formula"] == Q.FORMULA == "quests:v5"
    assert {q["id"] for q in book["pending"]["quests"]} == {q for q, d in B.QUESTS.items() if d.get("requires")}
    assert "typed-post" in {q["id"] for q in book["quests"]}


def test_every_v3_quest_keeps_its_id_and_xp():
    for q in Q.QUESTS:
        assert B.QUESTS[q.id]["xp"] == q.xp and B.QUESTS[q.id]["title"] == q.title, q.id


def test_disabled_quests_are_never_shown_or_counted():
    out = run(games=[game("b1")])
    ids = {q["id"] for q in out["quests"]}
    assert not ids & {q for q, d in B.QUESTS.items() if d.get("requires")}
    assert "lab" not in {a["id"] for a in out["arcs"]}


def test_arrival_walks_a_post_a_discussion_then_a_game_or_a_deal_with_another_operator():
    out = run(posts=[{"author_id": ME, "topic_slug": "general", "visibility": "public", "created_at": "2026-11-04"}])
    a = arc(out, "arrival")
    assert a["next"]["id"] == "discussion" and a["done"] == 1 and a["pillar"] == "start"
    out = run(posts=[{"author_id": ME, "topic_slug": "general", "visibility": "public", "created_at": "2026-11-04"}],
              comments=[{"with": "b1", "at": "2026-11-04T11:00:00+00:00"}],
              deals=[{"buyer": ME, "seller": "c1", "status": "confirmed", "ended_at": "2026-11-05T00:00:00+00:00"}])
    a = arc(out, "arrival")
    assert a["complete"] and "arrived" in out["badges"] and {"discussion", "first-act", "first-trade"} <= done(out)


# ── farms (proposal §4): each must fail ─────────────────────────────────────

def test_q1_your_own_second_agent_counts_for_no_multi_party_step():
    out = run(games=[game("mine2", challenge={"kind": "open"}), game("mine2", challenge={"kind": "rematch"})],
              comments=[{"with": "mine2", "at": "2026-11-04"}])
    assert not done(out) & {"first-contact", "open-table", "rematch", "discussion", "first-act"}
    assert "first-set-play" in done(out), "it is still a game played (Welcome counts any)"


def test_q2_key_only_agents_are_not_other_operators_under_the_default_rule():
    out = run(games=[game("k1"), game("k2"), game("k1")])
    assert "first-contact" not in done(out) and track(out, "rivals")["value"] == 0
    assert "first-contact" in done(run(c=ctx(others="any"), games=[game("k1")])), "EMPORIA_QUEST_OTHERS=any counts them"


def test_q3_a_key_only_agent_cannot_farm_one_verified_agent():
    c = ctx(verified=VERIFIED - {"me"})
    out = run(c=c, games=[game("b1")])
    fc = next(q for q in out["quests"] if q["id"] == "first-contact")
    assert not fc["done"] and "verified" in fc["blocked"], "says what to fix"
    assert arc(out, "challenger")["next"] is None or arc(out, "challenger")["next"]["id"] == "house-game"


def test_q4_the_house_is_practice_only():
    out = run(games=[game("house"), game("house"), game("house")])
    assert "house-game" in done(out) and "first-contact" not in done(out) and track(out, "rivals")["value"] == 0


def test_q5_one_buyer_buying_a_hundred_times_is_one_regular():
    deals = [{"buyer": "b1", "seller": ME, "status": "confirmed", "ended_at": f"2026-11-04T{i // 60:02d}:{i % 60:02d}"}
             for i in range(100)]
    t = track(run(deals=deals), "regulars")
    assert t["value"] == 1 and t["tier"] == 1


def test_q6_many_agents_of_one_other_operator_are_one_rival():
    assert track(run(games=[game("b1"), game("b2"), game("b1", "b2")]), "rivals")["value"] == 1


def test_q7_likes_from_your_own_agents_do_not_notice_you():
    out = run(likes=[{"agent_id": "mine2", "to_agent": ME, "kind": "post", "at": "2026-11-04"},
                     {"agent_id": "house", "to_agent": ME, "kind": "post", "at": "2026-11-04"}])
    assert "noticed" not in done(out)
    assert "noticed" in done(run(likes=[{"agent_id": "c1", "to_agent": ME, "kind": "post", "at": "2026-11-04"}]))


def test_q8_taking_your_own_open_challenge_counts_nothing():
    out = run(games=[game("mine2", challenge={"kind": "open"})])
    assert "open-table" not in done(out)
    assert "open-table" in done(run(games=[game("b1", challenge={"kind": "open"})]))
    taker = {"session_id": "s_t", "module_type": "emporia:blotto:v2", "participants": ["b1", ME],
             "ended_at": "2026-11-04", "winner": None, "challenge": {"kind": "open"}}
    assert "taken-up" in done(run(games=[taker])), "joining another operator's open challenge"


def test_q9_q10_ladder_places_count_once_per_operator_at_its_best():
    def lad(opp, places, at, won=True):
        return game(opp, at=at, winner=ME if won else opp, challenge={"kind": "ladder", "ladder": {"places": places}})
    out = run(games=[lad("b1", 3, "2026-11-01"), lad("b1", 3, "2026-11-02"), lad("b2", 4, "2026-11-03"),
                     lad("mine2", 9, "2026-11-04"), lad("house", 9, "2026-11-05"), lad("c1", 2, "2026-11-06", won=False)])
    t = track(out, "ladder")
    assert t["value"] == 4 and t["tier"] == 1, "b1/b2 are one operator (best 4); own, house and a loss count nothing"


def test_q12_the_weekly_quest_pays_once_a_week():
    week = "2026-11-02T10:00:00+00:00"            # ISO 2026-W45
    exp = [{"created_at": "2026-11-01T00:00:00+00:00", "content": "This week:\nquest: week-rival\n"}]
    c = ctx()
    now = E._iso("2026-11-05T00:00:00+00:00").timestamp()
    one = E.evaluate(ME, E.events(ME, ctx=c, games=[game("b1", at=week)]), c, expeditions=exp, now=now)
    assert one["weekly"]["id"] == "week-rival" and one["weekly"]["from"] == "expedition"
    assert one["weekly"]["done"] and one["weekly"]["weeks_done"] == 1
    again = E.evaluate(ME, E.events(ME, ctx=c, games=[game("b1", at=week), game("b1", at="2026-11-03")]), c,
                       expeditions=exp, now=now)
    assert again["weekly"]["weeks_done"] == 1 and again["xp"] == one["xp"], "a second game the same week adds none"


def test_q13_the_week_turns_without_an_expedition_and_a_bad_tag_is_ignored():
    wq, why = E.quest_for_week("2026-W45", [{"created_at": "2026-11-01T00:00:00+00:00", "content": "quest: rm-rf"}])
    assert why == "rotation" and wq in B.WEEKLY
    assert E.quest_for_week("2026-W46", [])[0] != E.quest_for_week("2026-W45", [])[0], "the pool turns"


def test_q15_is_in_test_bargain_and_q16_a_streak_to_one_friend_is_no_steady_word():
    def pl(to, ok, i):
        return {"maker_id": ME, "to_agent": to, "status": "kept" if ok else "broken", "resolved_by": "evidence",
                "resolved_at": f"2026-11-0{i}"}
    feats = {f["id"]: f for f in run(pledges=[pl("b1", True, i) for i in range(1, 7)])["feats"]}
    assert not feats["steady-word"]["done"], "one operator"
    mixed = [pl("b1", True, 1), pl("c1", True, 2), pl("d1", False, 3), pl("b1", True, 4), pl("c1", True, 5),
             pl("d1", True, 6), pl("e1", True, 7), pl("b1", True, 8)]
    assert {f["id"]: f for f in run(pledges=mixed)["feats"]}["steady-word"]["at"] == "2026-11-08", \
        "a broken pledge restarts the streak"


def test_q17_a_fair_deal_needs_another_operator_and_an_efficient_split():
    pool = {"silk": 3, "spice": 2, "amber": 1}
    values = {ME: {"silk": 2, "spice": 2, "amber": 0}, "b1": {"silk": 0, "spice": 2, "amber": 6}}
    fair = {ME: {"silk": 3, "spice": 2, "amber": 0}, "b1": {"silk": 0, "spice": 0, "amber": 1}}   # 10 and 6
    poor = {ME: {"silk": 3, "spice": 0, "amber": 0}, "b1": {"silk": 0, "spice": 2, "amber": 1}}   # 6 and 10
    assert E.fair_split(pool, values, fair) and not E.fair_split(pool, {**values, ME: {"silk": 1, "spice": 2,
                                                                                         "amber": 3}}, poor)

    def bargain(opp, alloc):
        v = {ME: values[ME], opp: values["b1"]}
        return game(opp, module="emporia:bargain:v2", termination="deal", pool=pool, values=v,
                    allocation={ME: alloc[ME], opp: alloc["b1"]})
    feats = {f["id"]: f["done"] for f in run(games=[bargain("mine2", fair)])["feats"]}
    assert not feats["fair-deal"]
    assert {f["id"]: f["done"] for f in run(games=[bargain("b1", fair)])["feats"]}["fair-deal"]


def test_q18_calibrated_needs_enough_settled_calls_above_the_forecast():
    def feat(n, edge):
        return {f["id"]: f["done"] for f in run(c=ctx(min_calls=10), insight={
            "settled_calls": n, "insight_settled": edge, "at": "2026-11-04"})["feats"]}["calibrated"]
    assert not feat(9, 0.2) and not feat(12, -0.01) and feat(10, 0.05)


def test_q19_q20_a_rematch_with_your_own_agent_is_none_and_xp_is_paid_once_per_quest():
    out = run(games=[game("mine2", challenge={"kind": "rematch"})])
    assert "rematch" not in done(out)
    posts = [{"author_id": ME, "topic_slug": "ideas", "visibility": "public", "created_at": "2026-11-04"}]
    out = run(posts=posts)
    assert out["xp"] == B.QUESTS["hello"]["xp"] + B.QUESTS["idea"]["xp"], \
        "hello sits in Welcome and Arrival, idea in Welcome and the Agora: paid once each"


def test_p7_a_step_waiting_on_the_operator_or_others_is_never_next():
    c = ctx()
    evs = E.events(ME, ctx=c, reports=[{"reporter_id": ME, "status": "open", "created_at": "2026-11-04"}])
    out = E.evaluate(ME, evs, c, pending_report=True)
    hunt = arc(out, "bug-hunt")
    assert hunt["next"] is None and "verdict" in hunt["waiting"]
    agora = arc(out, "agora")
    assert agora["next"]["id"] == "idea", "Noticed waits on others: the idea is next"


# ── carry-over ──────────────────────────────────────────────────────────────

def _v3(records):
    return Q.progress(ME, posts=records.get("posts", []), games=records.get("games", []),
                      deals=records.get("deals", []), reports=records.get("reports", []), pledges_on=True)


def test_carry_over_v4_xp_is_never_below_v3_over_generated_records():
    rng = random.Random(7)
    people = list(OPS)
    for _ in range(200):
        recs = {"posts": [{"author_id": ME, "topic_slug": rng.choice(["ideas", "general", "debriefs"]),
                           "visibility": "public", "created_at": f"2026-11-0{rng.randint(1, 9)}"}
                          for _ in range(rng.randint(0, 2))],
                "games": [game(rng.choice(people[1:]), module=rng.choice(["emporia:blotto:v2", "emporia:bargain:v2"]),
                               at=f"2026-11-0{rng.randint(1, 9)}", calls={ME: 0.6} if rng.random() < .3 else {},
                               termination=rng.choice(["deal", None])) for _ in range(rng.randint(0, 3))],
                "deals": [{"buyer": ME, "seller": rng.choice(people[1:]), "status": "confirmed",
                           "ended_at": "2026-11-05"} for _ in range(rng.randint(0, 2))],
                "reports": [{"reporter_id": ME, "status": rng.choice(["open", "accepted", "invalid"]),
                             "created_at": "2026-11-04", "decided_at": "2026-11-05"}] if rng.random() < .4 else []}
        v3 = _v3(recs)
        out = run(v3=v3, **recs)
        assert out["xp"] >= v3["xp"], recs
        assert set(v3["badges"]) <= set(out["badges"])
        assert {q["id"] for q in v3["quests"] if q["done"]} <= done(out)


def test_a_v3_quest_v4_would_not_count_stays_done_and_says_so():
    recs = {"games": [game("mine2", calls={ME: 0.7})]}
    v3 = _v3(recs)
    assert any(q["id"] == "call" and q["done"] for q in v3["quests"])
    out = run(c=ctx(), v3={**v3, "quests": [*v3["quests"], {"id": "keep-word", "done": True, "at": "2026-11-01"}],
                           "badges": [*v3["badges"], "keep-word", "well-travelled"]}, **recs)
    kw = next(q for q in out["quests"] if q["id"] == "keep-word")
    assert kw["done"] and kw["carried"] == "quests:v3" and "keep-word" in out["carried"]["quests"]
    assert "well-travelled" in out["badges"] and "well-travelled" in out["carried"]["badges"]


@pytest.mark.parametrize("bad", ["", None, "not a time"])
def test_odd_times_never_crash_the_week(bad):
    assert E.week_of(bad) == ""
    run(games=[game("b1", at=bad)])


# ── Join a lab (labs W0): a finding in a lab someone else's operator made ────

def test_q_lab_join_counts_a_finding_in_another_operators_lab():
    c = ctx(parts=("pledges", "labs"))
    assert "lab-join" in done(run(c=c, lab_findings=[{"creator": "b1", "at": "2026-11-04T10:00:00+00:00"}]))


def test_q_lab_join_cannot_be_farmed_in_your_own_operators_lab_or_the_houses():
    c = ctx(parts=("pledges", "labs"))
    own = [{"creator": "mine2", "at": "2026-11-04T10:00:00+00:00"}, {"creator": ME, "at": "2026-11-04T10:00:00+00:00"}]
    assert "lab-join" not in done(run(c=c, lab_findings=own))
    assert "lab-join" not in done(run(c=c, lab_findings=[{"creator": "house", "at": "2026-11-04T10:00:00+00:00"}]))


def test_q_lab_join_is_hidden_on_a_relay_without_labs_and_the_rest_of_the_lab_arc_waits():
    out = run(lab_findings=[{"creator": "b1", "at": "2026-11-04T10:00:00+00:00"}])
    assert "lab-join" not in {q["id"] for q in out["quests"]}
    lab_quests = ("lab-join", "lab-confirmed", "lab-fixed", "land-fix", "review-fix")
    assert all(B.QUESTS[q].get("needs") == ("labs",) for q in lab_quests)
    assert not {q["id"] for q in out["quests"]} & set(lab_quests) and "lab" not in {a["id"] for a in out["arcs"]}
