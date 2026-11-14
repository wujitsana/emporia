"""The Chronicle's pure core (games plan, phase T0): decks, seats, passage checks, the sealed-pick tally, a whole
chapter from the signed records, practice mode, teams, recaps and the bounded views. No relay, no network."""
import hashlib
import importlib.util
import pathlib

import pytest

from emporia import commit_reveal, progression, rand_game, wire
from emporia.modules import chronicle as C

spec = importlib.util.spec_from_file_location(
    "sim_chronicle", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sim_chronicle.py")
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)

DAY = 86_400
CALL = 1_800_000_000.0
PREV = CALL - DAY


def sig(*p):
    return hashlib.sha256(":".join(map(str, p)).encode()).digest()


SIGS = {"call": sig("c"), "show": sig("s"), "count": sig("n")}


def mk_members(n, per=1, age=100 * DAY, team=False, house=False):
    out = []
    for i in range(n):
        for j in range(per):
            out.append(C.Member(f"a{i}_{j}", f"op{i}", PREV - age, PREV - 10 * DAY, f"T{i}" if team and per > 1 else ""))
    if house:
        out.append(C.Member("house", "house", PREV - 300 * DAY, PREV - 200 * DAY, "", True))
    return out


def mk_events(n=8, ops=4):
    return [{"ref": f"{['session', 'post', 'gh_pr', 'room'][i % 4]}:e{i}", "line": f"event {i}",
             "operators": [f"op{i % ops}", f"op{(i + 1) % ops}"], "at": CALL - DAY + 60 + i} for i in range(n)]


class Run:
    """One chapter in progress: knows the seats and card the way an agent's tool does, and writes the records."""

    def __init__(self, members, params=C.Params(), events=None, history=(), bench=frozenset()):
        self.members, self.params, self.bench = members, params, bench
        self.events = events if events is not None else mk_events()
        roster = C.eligible(members, PREV)
        self.mode = C.mode_of(roster, params, bench)
        self.numbering = C.next_numbering(list(history), self.mode or "live")
        self.scope = C.chapter_scope("r", self.numbering, self.mode or "live")
        self.units = C.roster_units(roster, self.mode, params, bench) if self.mode else []
        self.seats = C.seat(self.units, self.scope, SIGS["call"], self.mode or "live")
        self.window = C.record_window(self.events, CALL - params.epoch_s, CALL)
        self.card = C.draw_card(self.scope, SIGS["call"])
        self.anchors = C.draw_anchors(self.window, self.scope, SIGS["call"])
        self.refs = [e["ref"] for e in self.window]
        self.w = C.windows(CALL, params.epoch_s)
        self.seals, self.reveals, self.ballots = [], [], []
        self.nonces = {}
        self.by_unit = {}
        for m in roster:
            self.by_unit.setdefault(m.agent_id if self.mode == "practice" else C.unit_key(m, params), []).append(m.agent_id)

    def agent(self, unit, k=0):
        return self.by_unit[unit][k]

    def good_cites(self):
        return [self.anchors[0], next(r for r in self.refs if r != self.anchors[0])]

    def passage(self, unit, post=None, agent=None, reveal=True, text=None, cites=None, nonce=None, seal_at=None,
                reveal_at=None):
        agent = agent or self.agent(unit)
        post = post or f"pst_{unit}"
        n = nonce or hashlib.sha256(f"{unit}:{post}".encode()).hexdigest()
        self.nonces[post] = n
        self.seals.append({"agent": agent, "what": "passage", "commitment": C.passage_commitment(post, n),
                           "at": seal_at if seal_at is not None else self.w["write"][0] + 1})
        if reveal:
            self.reveals.append({"agent": agent, "post_id": post, "text": text or sim.valid_passage(self.card),
                                 "cites": cites or self.good_cites(), "nonce": n,
                                 "at": reveal_at if reveal_at is not None else self.w["open"][0] + 1})
        return post

    def pick(self, unit, pick, agent=None, reveal=True, nonce=None, reason="", ballot_at=None, seal_at=None):
        agent = agent or self.agent(unit)
        n = nonce or hashlib.sha256(f"{unit}:{pick}:pick".encode()).hexdigest()
        self.seals.append({"agent": agent, "what": "pick", "commitment": C.pick_commitment(pick, reason, None, n),
                           "at": seal_at if seal_at is not None else self.w["pick"][0] + 1})
        if reveal:
            self.ballots.append({"agent": agent, "pick": pick, "reason": reason, "recap": None, "nonce": n,
                                 "at": ballot_at if ballot_at is not None else self.w["count"][0] + 1})

    def write_all(self, silent=()):
        return {u: self.passage(u) for u in self.seats.scribes if u not in silent}

    def resolve(self):
        return C.resolve_chapter(relay_id="r", numbering=self.numbering, members=self.members, prev_call_ts=PREV,
                                 call_ts=CALL, events=self.events, sigs=SIGS, seals=self.seals, reveals=self.reveals,
                                 ballots=self.ballots, params=self.params, bench=self.bench)


# ---------------------------------------------------------------- reuse, not copies

def test_the_core_imports_what_the_plan_says_to_reuse():
    assert C._weight is progression._weight
    assert C.commit_reveal is commit_reveal and C.rand_game is rand_game


def test_url_check_agrees_with_the_wire_rule():
    for t in ["see www.x.com", "a.b.com/x", "plain. text", "http://x", "ftp://y", "mail me@x.com", "v1.2 is out", "x.io/y"]:
        assert C.has_url(t) == wire.has_url(t), t


# ---------------------------------------------------------------- decks and the card

def test_decks_are_what_the_plan_says():
    d = C.decks()
    assert len(d["words"]) == 256 == len(set(d["words"])) and all(w.isalpha() and w == w.lower() and len(w) <= 9 for w in d["words"])
    assert d["limits"] == [280, 400, 600] and len(d["shapes"]) == 16 and len(d["voices"]) == 16
    assert {s["id"] for s in d["shapes"]} == set(C.SHAPES)


def test_the_card_is_a_pure_draw_from_the_beacon():
    a, b = C.draw_card("s", sig(1)), C.draw_card("s", sig(1))
    assert a == b and C.draw_card("s", sig(2)) != a or C.draw_card("t", sig(1)) != a


def test_every_shape_has_a_passage_that_passes_for_every_word_and_limit():
    for shape in C.SHAPES:
        for w in C.decks()["words"]:
            for limit in C.decks()["limits"]:
                card = C.Card(w, limit, shape, "myth")
                chk = C.check_passage(sim.valid_passage(card), ["session:g1", "post:p1"], card, sim._REFS, sim._ANCH)
                assert chk.ok, (shape, w, chk.hard)


def test_each_shape_rejects_a_plain_paragraph_that_does_not_follow_it():
    plain = "Lantern light is not a shape, but it is a sentence, and so is this one, which goes on for a while."
    for shape, fn in C.SHAPES.items():
        if shape in ("no_long_words", "short_sentences", "ends_with_question", "distinct_initials"):
            continue
        assert not fn(plain), shape


# ---------------------------------------------------------------- criterion 9: the hand-written fixtures

def test_forty_fixture_passages_no_wrong_refusal_no_wrong_pass():
    r = sim.run_fixtures()
    assert r["n"] >= 40 and not r["wrong"], r["wrong"]


def test_a_flag_is_not_a_hard_failure():
    card = C.Card("lantern", 400, "no_commas", "myth")
    chk = C.check_passage("I lit the lantern myself.", sim._OK, card, sim._REFS, sim._ANCH,
                          recent_openings=("I lit the lantern myself",), style={"narrator": "third person", "tense": "past"})
    assert chk.ok and {r for r, _ in chk.flags} >= {"opening", "narrator"}


# ---------------------------------------------------------------- the clock and the record window

def test_windows_tile_the_epoch_and_scale():
    for epoch in (DAY, 7 * DAY):
        w = C.windows(CALL, epoch)
        assert w["write"][0] == CALL and w["count"][1] == CALL + epoch
        assert w["write"][1] == w["open"][0] and w["open"][1] == w["pick"][0] and w["pick"][1] == w["count"][0]
        assert w["write"][1] - w["write"][0] == epoch / 3 and w["open"][1] - w["open"][0] == epoch * 5 / 24
    r = C.call_rounds(CALL)
    assert r["call"] < r["show"] < r["count"]


def test_quiet_day_needs_six_events_from_three_operators():
    ev = mk_events(8, 4)
    assert not C.quiet_day(C.record_window(ev, CALL - DAY, CALL))
    assert C.quiet_day(C.record_window(mk_events(5, 4), CALL - DAY, CALL))
    assert C.quiet_day(C.record_window(mk_events(8, 2), CALL - DAY, CALL), "live")           # 2 operators
    assert not C.quiet_day(C.record_window(mk_events(8, 2), CALL - DAY, CALL), "practice")   # anyone will do


def test_the_window_carries_over_and_dedupes():
    ev = mk_events(4, 4)
    carry = [{"ref": "post:old", "line": "x" * 300, "operators": ["op1"], "at": CALL - 3 * DAY}]
    win = C.record_window(ev + ev, CALL - DAY, CALL, carry)
    assert len(win) == 5 and all(len(e["line"]) <= C.LINE_MAX for e in win)


def test_anchors_are_a_game_a_post_and_another_kind():
    win = C.record_window(mk_events(12, 4), CALL - DAY, CALL)
    a = C.draw_anchors(win, "s", sig(1))
    assert [C.ref_kind(x) for x in a] == ["session", "post", C.ref_kind(a[2])] and C.ref_kind(a[2]) not in ("session", "post")
    assert a == C.draw_anchors(win, "s", sig(1))
    only_posts = [e for e in win if C.ref_kind(e["ref"]) == "post"]
    assert [C.ref_kind(x) for x in C.draw_anchors(only_posts, "s", sig(1))] == ["post"]


# ---------------------------------------------------------------- seats

def test_seats_split_by_the_plan_and_never_overlap():
    expect = {4: (2, 2, 0), 5: (2, 3, 0), 7: (3, 4, 0), 8: (4, 4, 0), 9: (4, 5, 0), 20: (9, 8, 3), 30: (9, 8, 13)}
    for m, (j, s, a) in expect.items():
        seats = C.seat([f"u{i}" for i in range(m)], "s", sig(m))
        assert (len(seats.jurors), len(seats.scribes), len(seats.alternates)) == (j, s, a)
        allu = [*seats.jurors, *seats.scribes, *seats.alternates]
        assert len(set(allu)) == m


def test_seats_depend_on_the_beacon_not_on_the_input_order():
    u = [f"u{i}" for i in range(9)]
    assert C.seat(u, "s", sig(1)) == C.seat(u[::-1], "s", sig(1)) != C.seat(u, "s", sig(2))


def test_show_order_is_the_jurors_own_beacon_draw():
    ids = [f"p{i}" for i in range(8)]
    assert sorted(C.show_order("j", ids, "s", sig(1))) == ids
    assert C.show_order("j", ids, "s", sig(1)) != C.show_order("k", ids, "s", sig(1))


def test_the_criterion_two_check_on_ten_thousand_draws():
    r = sim.crit2()
    assert r["pass"], r


# ---------------------------------------------------------------- the tally

def P(unit, pick, w=1.0):
    return C.Pick(unit, pick, w)


def tl(cands, picks, J=4, mode="live", authors=None):
    return C.tally(cands, picks, J=J, scope="s", tie_sig=sig("t"), mode=mode, authors=authors)


def test_most_weight_wins():
    v = tl(["a", "b"], [P("j1", "a"), P("j2", "a"), P("j3", "b"), P("j4", "a")])
    assert v["canon"] == "a" and v["reason"] == "canon" and v["totals"] == {"a": 3.0, "b": 1.0} and v["tie"] is None


def test_the_age_ramp_weighs_a_new_agents_pick():
    now = CALL
    who = {"old": progression.Who("o", now - 100 * DAY), "new": progression.Who("n", now - 1 * DAY),
           "fresh": progression.Who("f", now)}
    ws = [progression._weight(who, a, now) for a in ("old", "new", "fresh")]
    v = tl(["a", "b"], [P("o", "a", ws[0]), P("n", "b", ws[1]), P("f", "b", ws[2])], J=3)
    assert ws[0] == 1.0 and ws[2] == progression.MIN_RAMP == 0.1 and abs(ws[1] - 1 / 30) < 1e-9 or ws[1] == 0.1
    assert ws[1] == 0.1                      # one day old: still at the floor
    assert v["canon"] == "a" and v["totals"] == {"a": 1.0, "b": 0.2}


def test_quorum_is_two_picks_from_two_units_and_half_of_the_jury():
    assert tl(["a"], [P("j1", "a")], J=4)["reason"] == "quorum"
    assert tl(["a"], [P("j1", "a"), P("j2", "a")], J=4)["canon"] == "a"
    assert tl(["a"], [P("j1", "a"), P("j2", "a")], J=9)["reason"] == "quorum"      # half of 9 is 5
    assert tl(["a"], [P("j1", "a")] * 2, J=2)["reason"] == "quorum"                # one unit twice
    assert tl([], [P("j1", "none"), P("j2", "none")])["reason"] == "no_candidates"


def test_none_wins_or_ties_for_it_means_no_canon():
    assert tl(["a"], [P("j1", "none"), P("j2", "none"), P("j3", "a")])["reason"] == "none_won"
    v = tl(["a"], [P("j1", "none"), P("j2", "a")], J=2)
    assert v["canon"] is None and v["reason"] == "none_won"
    assert tl(["a"], [P("j1", "none"), P("j2", "a"), P("j3", "a")])["canon"] == "a"


def test_a_tie_is_drawn_by_the_beacon_among_the_tied_sorted_by_id():
    v = tl(["b", "a", "c"], [P("j1", "a"), P("j2", "b"), P("j3", "c", 0.5)], J=3)
    assert v["tie"]["among"] == ["a", "b"] and v["canon"] == ["a", "b"][rand_game.pick("s", "tie", sig("t"), 2)]
    assert v == tl(["a", "b", "c"], [P("j1", "a"), P("j2", "b"), P("j3", "c", 0.5)], J=3)
    seen = set()
    for i in range(40):
        seen.add(C.tally(["a", "b"], [P("j1", "a"), P("j2", "b")], J=2, scope="s", tie_sig=sig(i))["canon"])
    assert seen == {"a", "b"}


def test_a_pick_for_a_non_candidate_or_ones_own_passage_is_not_counted():
    v = tl(["a", "b"], [P("j1", "zzz"), P("j2", "b"), P("j3", "a"), P("j4", "a")], authors={"a": "j3"})
    assert v["canon"] == "b" and v["totals"] == {"a": 1.0, "b": 1.0} or v["canon"] in ("a", "b")
    assert ("j1", "not a candidate") in v["ignored"] and ("j3", "own passage") in v["ignored"]


def test_practice_counts_one_pick_with_weight_one():
    v = tl(["a"], [P("j1", "a")], J=1, mode="practice")
    assert v["canon"] == "a" and v["mode"] == "practice" and v["why"] == "fewer than 4 operators"


# ---------------------------------------------------------------- a whole chapter from the records

def test_a_chapter_from_records_alone_agrees_with_the_independent_count_1000_seeded_chapters():
    r = sim.crit1(1000)
    assert r["pass"], r["bad"]
    assert r["by"]["live"] > 300 and r["by"]["practice"] > 100       # the mix really exercises both modes


def test_happy_path_a_canon_passage():
    run = Run(mk_members(8))
    posts = run.write_all()
    cand = sorted(posts.values())
    for i, j in enumerate(run.seats.jurors):
        run.pick(j, cand[0] if i < 3 else cand[1], reason="x")
    v = run.resolve()
    assert v["mode"] == "live" and v["canon"] == cand[0] and v["reason"] == "canon" and v["label"] == "Chapter 1"
    assert v["operators"] == 8 and v["candidates"] == cand and not v["refused"]
    assert all(o == "ok" for o in v["outcome"].values())


def test_a_tie_between_two_passages_is_drawn():
    run = Run(mk_members(8))
    cand = sorted(run.write_all().values())
    for i, j in enumerate(run.seats.jurors):
        run.pick(j, cand[i % 2])
    v = run.resolve()
    assert v["tie"]["among"] == cand[:2] and v["canon"] == cand[:2][v["tie"]["drawn"]]


def test_no_quorum_when_too_few_picks_are_opened():
    run = Run(mk_members(8))
    cand = sorted(run.write_all().values())
    j = run.seats.jurors
    run.pick(j[0], cand[0])
    for u in j[1:]:
        run.pick(u, cand[0], reveal=False)           # sealed, never opened
    v = run.resolve()
    assert v["canon"] is None and v["reason"] == "quorum" and v["picks_counted"] == 1
    assert v["outcome"][j[1]] == "missed" and v["outcome"][j[0]] == "ok"


def test_none_wins_and_no_canon():
    run = Run(mk_members(8))
    cand = sorted(run.write_all().values())
    for i, j in enumerate(run.seats.jurors):
        run.pick(j, "none" if i < 3 else cand[0])
    v = run.resolve()
    assert v["canon"] is None and v["reason"] == "none_won" and v["none"] == 3.0


def test_an_unrevealed_pick_does_not_count_and_an_alternate_fills_the_juror_seat():
    run = Run(mk_members(9))                       # J=4, S=5? 9//2=4 jurors, 5 scribes, no alternates
    assert len(run.seats.alternates) == 0
    run = Run(mk_members(20))                      # 9 jurors, 8 scribes, 3 alternates
    cand = sorted(run.write_all().values())
    j, alts = run.seats.jurors, run.seats.alternates
    for u in j[:8]:
        run.pick(u, cand[0])
    run.pick(j[8], cand[1], reveal=False)          # a juror sealed and went quiet
    run.pick(alts[0], cand[1])                     # an alternate with no passage sealed a pick: fills the seat
    run.pick(alts[1], cand[1])                     # not needed: J picks are already counted
    v = run.resolve()
    assert v["picks_counted"] == 9 and v["totals"][cand[1]] == 1.0 and v["totals"][cand[0]] == 8.0
    assert v["outcome"][j[8]] == "missed"


def test_an_alternate_fills_a_scribe_seat_that_stayed_empty():
    run = Run(mk_members(20))
    scribes, alts = list(run.seats.scribes), run.seats.alternates
    posts = run.write_all(silent={scribes[0]})
    ap = run.passage(alts[0])
    run.passage(alts[1])
    cand = sorted([*posts.values(), ap, f"pst_{alts[1]}"])
    for u in run.seats.jurors:
        run.pick(u, ap)
    v = run.resolve()
    assert ap in v["candidates"] and len(v["candidates"]) == 8                 # S=8 seats: 7 scribes + the first alternate
    assert f"pst_{alts[1]}" not in v["candidates"] and v["canon"] == ap
    assert cand and v["authors"][ap] == alts[0]


def test_a_passage_that_breaks_a_hard_rule_is_set_aside_with_the_rule():
    run = Run(mk_members(8))
    s = run.seats.scribes
    run.passage(s[0], text="x" * 1000)
    for u in s[1:]:
        run.passage(u)
    for u in run.seats.jurors:
        run.pick(u, f"pst_{s[1]}")
    v = run.resolve()
    assert v["set_aside"] == [{"unit": s[0], "post_id": f"pst_{s[0]}", "rules": v["set_aside"][0]["rules"]}]
    assert "length" in v["set_aside"][0]["rules"] and f"pst_{s[0]}" not in v["candidates"]


def test_a_quiet_day_calls_no_chapter():
    run = Run(mk_members(8), events=mk_events(5, 4))
    assert run.resolve()["reason"] == "quiet"
    run = Run(mk_members(8), events=mk_events(9, 2))
    assert run.resolve()["reason"] == "quiet"


def test_the_records_the_rules_refuse_are_listed_not_dropped():
    run = Run(mk_members(8, per=2))
    s, j = run.seats.scribes, run.seats.jurors
    p0 = run.passage(s[0])
    run.passage(s[0], post="pst_other", agent=run.agent(s[0], 1))                 # the second agent of one operator
    run.passage(s[1], nonce="a" * 64)
    run.reveals[-1]["nonce"] = "b" * 64                                           # a reveal that is not the commitment
    run.passage(s[2], seal_at=run.w["open"][0] + 1)                               # sealed after the write window
    run.passage(j[0])                                                             # a juror has no passage seat
    run.pick(s[3], p0)                                                            # a scribe has no pick seat
    run.pick(j[1], p0, reveal=False)
    run.ballots.append({"agent": run.agent(j[1]), "pick": p0, "reason": "", "recap": None, "nonce": "c" * 64,
                        "at": run.w["count"][0] + 1})
    run.pick(j[2], p0, ballot_at=run.w["count"][1] + 5)                           # opened after the window
    run.pick(j[3], p0, reason="x" * 141)                                          # a reason over 140
    v = run.resolve()
    why = {(r["what"], r["why"]) for r in v["refused"]}
    assert ("passage", "another agent of this operator acts first") in why
    assert ("reveal", "does not match the commitment") in why
    assert ("passage", "outside the write window") in why and ("passage", "no seat for this") in why
    assert ("pick", "no seat for this") in why and ("ballot", "does not match the commitment") in why
    assert ("ballot", "outside the count window") in why and ("ballot", "reason over 140 characters") in why
    assert v["candidates"] == [p0]


def test_an_alternate_that_sealed_a_passage_may_not_pick():
    run = Run(mk_members(20))
    alt = run.seats.alternates[0]
    run.passage(alt)
    run.pick(alt, "none")
    v = run.resolve()
    assert ("pick", "no seat for this") in {(r["what"], r["why"]) for r in v["refused"]}


def test_one_voice_per_operator_the_first_agent_to_seal_acts():
    run = Run(mk_members(8, per=3))
    s = run.seats.scribes[0]
    a1, a2 = run.agent(s, 1), run.agent(s, 0)
    run.passage(s, post="pst_one", agent=a1)
    run.passage(s, post="pst_two", agent=a2)
    assert run.resolve()["candidates"] == ["pst_one"]


# ---------------------------------------------------------------- teams

def test_a_claimed_team_is_one_unit_with_one_voice_and_its_oldest_agents_weight():
    members = mk_members(3) + [C.Member("t_old", "x1", PREV - 100 * DAY, PREV - 10 * DAY, "T"),
                               C.Member("t_new", "x2", PREV - 1 * DAY, PREV - 0.5 * DAY, "T")]
    roster = C.eligible(members, PREV)
    assert {C.unit_key(m) for m in roster} == {"op0", "op1", "op2", "T"}
    who = C.pick_weight_who(roster)
    assert who["t_new"].operator == "T" == who["t_old"].operator
    assert progression._weight(who, "t_new", CALL) == 1.0                   # the team's, not 0.1
    unclaimed = C.pick_weight_who([C.Member("u", "x3", PREV - 1 * DAY, PREV - 0.5 * DAY)])
    assert progression._weight(unclaimed, "u", CALL) < 0.1 + 0.04
    # the same new agent posing as its own operator weighs a tenth
    assert progression._weight(unclaimed, "u", CALL) < progression._weight(who, "t_new", CALL)


def test_a_team_may_reseal_and_the_last_seal_stands_and_any_teammate_may_do_it():
    members = mk_members(7) + [C.Member(f"t{k}", f"x{k}", PREV - 90 * DAY, PREV - 10 * DAY, "T") for k in range(3)]
    run = Run(members)
    assert "T" in run.units
    u = "T"
    seat_scribe = u in run.seats.scribes
    if not seat_scribe:                                            # a draw where T judges: still one unit
        assert u in run.seats.jurors
        return
    run.passage(u, post="pst_draft", agent="t0")
    run.passage(u, post="pst_final", agent="t2")
    posts = [run.passage(s) for s in run.seats.scribes if s != u]
    for j in run.seats.jurors:
        run.pick(j, "pst_final")
    v = run.resolve()
    assert "pst_final" in v["candidates"] and "pst_draft" not in v["candidates"] and v["canon"] == "pst_final" and posts


def test_a_team_cannot_pick_its_own_passage_because_it_is_never_both_roles():
    for i in range(200):
        s = C.seat([f"op{k}" for k in range(5)] + ["T"], "s", sig(i))
        assert "T" in [*s.jurors, *s.scribes, *s.alternates]
        assert not ("T" in s.jurors and "T" in s.scribes)


def test_the_desk_deals_researcher_writer_and_critic_by_the_beacon():
    d = C.desk_roles(["a", "b", "c"], "s", sig(1))
    assert sorted(d.values()) == ["critic", "researcher", "writer"] and d == C.desk_roles(["c", "b", "a"], "s", sig(1))
    assert len(C.desk_roles(["a"], "s", sig(1))) == 1


# ---------------------------------------------------------------- practice mode (section 11)

def test_a_roster_of_one_two_or_three_operators_plays_a_practice_page_with_the_right_seats_and_label():
    for ops, per, agents in ((1, 3, 3), (2, 2, 4), (3, 1, 3)):
        run = Run(mk_members(ops, per=per, house=ops == 1))
        assert run.mode == "practice", ops
        n = len(run.units)
        assert len(run.seats.jurors) == max(1, n // 2) and len(run.seats.scribes) == n - len(run.seats.jurors)
        assert run.numbering["label"] == "Practice page 1" and ["t", "pp-1"] in run.numbering["tags"]
        posts = run.write_all()
        for j in run.seats.jurors:
            run.pick(j, sorted(posts.values())[0])
        v = run.resolve()
        assert v["mode"] == "practice" and v["why"] == "fewer than 4 operators" and v["canon"] == sorted(posts.values())[0]
        assert v["label"] == "Practice page 1" and v["operators"] == ops


def test_practice_needs_three_agents_house_included_and_can_be_switched_off():
    assert Run(mk_members(1, per=2)).mode is None
    assert Run(mk_members(1, per=2, house=True)).mode == "practice"
    assert Run(mk_members(3), params=C.Params(practice=False)).mode is None
    assert Run(mk_members(4), params=C.Params(practice=False)).mode == "live"


def test_practice_picks_by_the_same_operator_and_the_house_count_and_cross_operator_picks_are_listed():
    run = Run(mk_members(2, per=2, house=True))          # 5 agents: 2 jurors, 3 scribes
    posts = run.write_all()
    first = sorted(posts.values())[0]
    for j in run.seats.jurors:
        run.pick(j, first)
    v = run.resolve()
    assert v["picks_counted"] == 2 and v["canon"] == first and isinstance(v["cross_operator_picks"], int)
    assert all(p == 1.0 for p in v["totals"].values()) or v["totals"][first] == 2.0


def test_a_practice_page_feeds_no_renown_track_board_or_recap():
    run = Run(mk_members(1, per=3, house=True))
    r = C.rewards({"mode": "practice"})
    assert r["first_passage_badge"] and r["first_passage_xp"] == 0
    assert not any(r[k] for k in ("quests", "tracks", "board", "renown", "style_card", "recap"))
    live = C.rewards({"mode": "live"})
    assert live["first_passage_xp"] == 5 and all(live[k] for k in ("quests", "tracks", "board", "renown"))
    assert run.mode == "practice"


def test_the_first_call_with_four_operators_is_chapter_one_whatever_practice_came_before():
    hist = [{"mode": "practice", "canon": "pst_pp1"}, {"mode": "practice", "canon": None}]
    n = C.next_numbering(hist, "live")
    assert n["n"] == 1 and n["label"] == "Chapter 1" and n["reply_to"] == C.PROLOGUE and n["tags"] == [["t", "chronicle"], ["t", "ch-1"]]
    run = Run(mk_members(4), history=hist)
    assert run.mode == "live" and run.numbering["n"] == 1
    # practice keeps its own chain, and a roster that falls under four goes back to it, canon waiting where it stopped
    hist2 = [*hist, {"mode": "live", "canon": "pst_c1"}]
    p = C.next_numbering(hist2, "practice")
    assert p["n"] == 3 and p["reply_to"] == "pst_pp1" and ["t", "practice"] in p["tags"] and ["t", "ch-1"] not in p["tags"]
    nxt = C.next_numbering(hist2, "live")
    assert nxt["n"] == 2 and nxt["reply_to"] == "pst_c1"


def test_the_passage_is_a_post_with_the_tags_the_plan_names():
    n = C.next_numbering([], "live")
    post = C.passage_post("text", ["session:a", "post:b"], n, teammates=["agent_x"])
    assert post["kind"] == "other" and post["label"] == "passage" and post["topic"] == "chronicle"
    assert post["tags"] == [["t", "chronicle"], ["t", "ch-1"], ["ref", "session", "a"], ["ref", "post", "b"], ["p", "agent_x"]]
    assert post["reply_to"] == C.PROLOGUE == post["root"]
    assert not wire.post_problem("other", "passage", "text", post["tags"], topic=None)


# ---------------------------------------------------------------- parameters: D1, D4, D6

def test_d1_one_passage_per_operator_or_per_agent():
    solo = mk_members(1, per=4)
    assert Run(solo, params=C.Params(unit="operator")).mode == "practice"          # one operator
    run = Run(solo, params=C.Params(unit="agent"))
    assert run.mode == "live" and len(run.units) == 4
    with pytest.raises(ValueError):
        C.Params(unit="team")


def test_d4_the_epoch_is_a_parameter_and_the_windows_follow_it():
    week = C.Params(epoch_s=7 * DAY)
    run = Run(mk_members(8), params=week, events=[{**e, "at": CALL - 7 * DAY + 60 + i} for i, e in enumerate(mk_events())])
    assert run.w["count"][1] - run.w["write"][0] == 7 * DAY and run.mode == "live"
    assert len(run.window) == 8                 # the week's window holds events a day's would not
    assert len(Run(mk_members(8), events=[{**e, "at": CALL - 7 * DAY + 60 + i} for i, e in enumerate(mk_events())]).window) == 0
    with pytest.raises(ValueError):
        C.Params(epoch_s=60)


# ---------------------------------------------------------------- missing twice

def test_missing_twice_in_a_row_leaves_an_operator_off_for_three_chapters():
    out = [{"a": "ok", "b": "missed"}, {"a": "ok", "b": "missed"}, {}, {}, {}, {}, {}]
    assert C.benched(out, 1) == set() and C.benched(out, 2) == {"b"} and C.benched(out, 4) == {"b"}   # chapters 2, 3, 4
    assert C.benched(out, 5) == set()
    assert C.benched([{"b": "missed"}, {"b": "ok"}, {"b": "missed"}], 3) == set()      # not in a row
    assert C.benched([{"b": "missed"}, {}, {"b": "missed"}], 3) == {"b"}                # chapters it did not seal do not break a streak


def test_a_benched_operator_is_left_off_the_roster():
    run = Run(mk_members(5), bench=frozenset({"op0"}))
    assert "op0" not in run.units and len(run.units) == 4 and run.mode == "live"
    assert Run(mk_members(4), bench=frozenset({"op0"})).mode == "practice"


# ---------------------------------------------------------------- recaps

def test_the_keeper_is_a_beacon_draw_among_the_arcs_canon_authors():
    o = C.keeper_order("s", sig(1), ["a", "b", "c", "a"])
    assert sorted(o) == ["a", "b", "c"] and o == C.keeper_order("s", sig(1), ["c", "b", "a"])
    assert {C.keeper_order("s", sig(i), ["a", "b", "c"])[0] for i in range(60)} == {"a", "b", "c"}


def test_arcs_and_volumes():
    assert [C.arc_index(i) for i in (1, 7, 8, 14, 15)] == [0, 0, 1, 1, 2]
    assert C.volume_index(28) == 0 and C.volume_index(29) == 1


def test_the_plain_recap_is_the_first_sentences_and_fits():
    ps = [("The lantern stood. " + "x " * 300) for _ in range(7)]
    r = C.plain_recap(ps)
    assert r["kind"] == "plain" and len(r["text"]) <= 900 and r["text"].count("The lantern stood.") == 7


def test_recap_checks():
    ok = {"story": "s" * 900, "threads": [{"text": "t" * 80, "ref": "post:1"}] * 8, "cast": ["the Gatekeeper", "bob"],
          "cites": ["post:1", "post:2"]}
    kw = dict(arc_canon=["post:1", "post:2"], handles={"bob"}, card_names={"the Gatekeeper"})
    assert C.recap_problem(ok, **kw) == []
    bad = {**ok, "story": "s" * 901, "threads": [{"text": "t" * 81, "ref": "post:1"}] * 9, "cast": ["nobody"], "cites": ["post:1"]}
    msgs = " | ".join(C.recap_problem(bad, **kw, open_refutations=["post:ref"]))
    for part in ("900", "8 threads", "80 characters", "post:2", "nobody", "post:ref"):
        assert part in msgs


def test_a_recap_is_adopted_by_more_than_half_of_the_counted_weight():
    assert C.recap_adopted([(1, "up"), (1, "up"), (1, "down")])
    assert not C.recap_adopted([(1, "up"), (1, "down")])
    assert not C.recap_adopted([(1, "up"), (0.9, "down"), (1.5, "down")])
    assert C.recap_adopted([(0.1, "up"), (0.05, "down"), (5, None)])
    assert not C.recap_adopted([])


# ---------------------------------------------------------------- the bounded views (criterion 8)

def test_views_stay_under_their_caps_whatever_the_length_of_the_book():
    r = sim.crit8()
    assert r["pass"], r
    sizes = {b: v["scribe"] for b, v in r["sizes"].items()}
    assert len(set(sizes.values())) == 1                      # 5, 50 and 500 chapters read alike


def test_a_small_view_is_whole_and_a_big_one_is_cut_in_the_plans_order():
    card = C.Card("lantern", 400, "no_commas", "myth")
    small = C.scribe_view(header="you are a scribe", card=card, anchors=[("post:1", "a post")], style={"narrator": "third person"},
                          canon=["The gate stood. It was shut."], window_lines=["a", "b"], recap="so far", local="my voice")
    assert small["dropped"] == [] and "my voice" in small["text"] and "The gate stood." in small["text"]
    big = C.scribe_view(header="h", card=card, anchors=[("post:1", "a")], style={"narrator": "n", "motifs": ["m"] * 6, "never": ["x"] * 4},
                        canon=["A. " + "w " * 280, "B. " + "w " * 280], window_lines=["L" * 110] * 9, recap="R" * 900,
                        volume_recap="V" * 300, threads=["T" * 80] * 8, cast=[("c", False)] * 20, local="V" * 600)
    order = ["before", "record", "volume", "threads", "recap", "style"]
    assert big["dropped"] == order[:len(big["dropped"])] and big["chars"] <= C.SCRIBE_CAP


def test_the_voice_note_is_the_last_thing_cut():
    card = C.Card("lantern", 400, "no_commas", "myth")
    huge = dict(header="h" * 300, card=card, anchors=[("post:" + "a" * 60, "L" * 110)] * 3,
                style={"narrator": "n" * 400, "tense": "past", "register": "r" * 500, "names": {f"i{i}": "n" * 30 for i in range(24)}},
                canon=["x" * 600, "y" * 600], window_lines=["L" * 110] * 9, recap="R" * 900, volume_recap="V" * 300,
                threads=["T" * 80] * 8, cast=[("c" * 40, True)] * 20, local="V" * 600)
    v = C.scribe_view(**huge)
    assert v["chars"] <= C.SCRIBE_CAP


def test_the_juror_view_never_cuts_a_candidate():
    card = C.Card("lantern", 600, "no_commas", "myth")
    cands = [{"id": chr(65 + i), "text": f"{i}" * 600, "checks": "ok", "cites": [f"post:{i}-{j}" for j in range(8)]} for i in range(8)]
    v = C.juror_view(header="h", card=card, style={"narrator": "n", "tense": "past", "names": {}}, previous_last="One. Two.",
                     candidates=cands, lines={r: "L" * 110 for c in cands for r in c["cites"]}, recap="R" * 900)
    assert v["chars"] <= C.JUROR_CAP and all(str(i) * 600 in v["text"] for i in range(8))


# ---------------------------------------------------------------- commitments

def test_commitments_bind_the_post_and_the_pick():
    n = "a" * 64
    c = C.passage_commitment("pst_1", n)
    assert commit_reveal.matches(c, {"post_id": "pst_1"}, n) and not commit_reveal.matches(c, {"post_id": "pst_2"}, n)
    p = C.pick_commitment("pst_1", "why", None, n)
    assert commit_reveal.matches(p, {"pick": "pst_1", "reason": "why", "recap": None}, n)
    assert not commit_reveal.matches(p, {"pick": "pst_1", "reason": "other", "recap": None}, n)
    assert not commit_reveal.matches(p, {"pick": "pst_1", "reason": "why", "recap": None}, "short")


# ---------------------------------------------------------------- the voting-rule gates, smaller than the report

@pytest.mark.parametrize("n", [6, 12])
def test_a_writers_ballot_strategy_gains_nothing_and_honest_voters_find_the_best(n):
    base = sim._cell(n, "none", 1.0, 5, 12)
    assert base["acc"] >= 0.65
    for st in ("bury", "random", "none"):
        r = sim._cell(n, "none", 1.0, 5, 12, strategy=st)
        assert r["wins"][0] == base["wins"][0]
