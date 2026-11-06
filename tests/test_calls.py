"""Calls and Insight (gamify, `docs/proposals/gamify-calls.md`): a forecast sealed with the move,
scored against the rating's own forecast.

The numbers below are worked by hand (natural log): ln 0.7 = -0.356675, ln 0.3 = -1.203973,
ln 0.5 = -0.693147, ln 0.01 = -4.605170; Glicko's g(350/173.7178) = 0.669069."""
import json
import math
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import calls, commit_reveal
from emporia import progression as pg
from emporia import relay_server as rs
from emporia.module_sdk import MODULE_REGISTRY, SessionAction, list_available_modules
from emporia.modules.blotto import BlottoModule, BlottoV2Module
from emporia.modules.lowest_unique import LowestUniqueV2Module
from emporia.modules.two_thirds import TwoThirdsV2Module
from emporia.routes import standing as st

BLOTTO = "emporia:blotto:v2"


def approx(x):
    return pytest.approx(x, abs=1e-6)


# ── the scoring math, by hand ─────────────────────────────────────────────────

def test_the_log_score_by_hand():
    assert pg.log_score(0.7, 1) == approx(-0.356675)
    assert pg.log_score(0.7, 0) == approx(-1.203973)
    assert pg.log_score(0.5, 0.5) == approx(-0.693147)
    # A 70 % call against the rating's 50 %: +0.336472 on a win, -0.510826 on a loss.
    assert pg.log_score(0.7, 1) - pg.log_score(0.5, 1) == approx(0.336472)
    assert pg.log_score(0.7, 0) - pg.log_score(0.5, 0) == approx(-0.510826)


def test_the_rule_is_proper_so_the_best_call_is_the_real_belief():
    """If the agent believes it wins 70 % of the time, its expected score is best at 0.70."""
    for belief in (0.2, 0.5, 0.7, 0.9):
        expected = {p / 100: belief * pg.log_score(p / 100, 1) + (1 - belief) * pg.log_score(p / 100, 0)
                    for p in range(1, 100)}
        assert max(expected, key=expected.get) == pytest.approx(belief)


def test_the_ratings_forecast_is_glickos_expected_score():
    a = pg.Rating("a", BLOTTO, rating=1700, rd=50)
    b = pg.Rating("b", BLOTTO, rating=1500, rd=350)
    # mu diff 200/173.7178 = 1.151293; g(phi_b = 2.014762) = 0.669069; E = 1/(1+e^-0.770293)
    assert pg.expected(a, b) == pytest.approx(0.683585, abs=1e-5)
    assert pg.expected_share("a", {"a": a, "b": b}) == pytest.approx(0.683585, abs=1e-5)
    equal = {p: pg.Rating(p, BLOTTO) for p in "xyz"}
    assert [pg.expected_share(p, equal) for p in "xyz"] == [approx(1 / 3)] * 3, "a table of equals: 1/N each"
    far = {"a": pg.Rating("a", BLOTTO, rating=3000, rd=30), "b": pg.Rating("b", BLOTTO, rating=800, rd=30)}
    assert pg.expected_share("a", far) == 0.99 and pg.expected_share("b", far) == 0.01, "held to the calls' range"


def test_a_share_of_the_win_and_the_worst_call():
    g = {"participants": ["a", "b", "c"], "winner": "a", "termination": "closest"}
    assert pg.shares(g) == {"a": 1.0, "b": 0.0, "c": 0.0}
    assert pg.shares({**g, "winner": None, "termination": "tie", "tied": ["a", "b"]}) == {"a": 0.5, "b": 0.5, "c": 0.0}
    assert pg.shares({"participants": ["a", "b"], "winner": None, "termination": "rounds"}) == {"a": 0.5, "b": 0.5}
    assert pg.shares({"participants": ["a", "b", "c"], "abandoned_by": "b"}) == {"a": 0.5, "b": 0.0, "c": 0.5}
    assert pg.shares({"participants": ["a", "b"], "termination": "no_reveals"}) is None
    assert pg.shares({"participants": ["a", "b"], "winner": None, "termination": "timeout"}) is None
    assert pg.worst_call(0.0) == 0.99 and pg.worst_call(1.0) == 0.01
    # A shared win of a half: 0.40 against the table's 1/3 gains 0.038481.
    assert (pg.log_score(0.4, 0.5) - pg.log_score(1 / 3, 0.5)) == pytest.approx(0.038481, abs=1e-6)


def _g(sid, a, b, winner, day, **extra):
    return {"session_id": sid, "module_type": BLOTTO, "played_as": BLOTTO, "participants": [a, b],
            "winner": winner, "termination": "rounds", "ended_at": f"2026-11-{day:02d}T00:00:00+00:00", **extra}


def test_a_first_call_against_fresh_ratings_by_hand():
    out = pg.calls_of([_g("s1", "a", "b", "a", 1, calls={"a": 70})])
    [c] = out["a"]
    assert (c.call, c.forecast, c.outcome) == (0.7, 0.5, 1.0)
    assert c.edge == approx(0.336472) and c.weight == 1.0 and not c.settled
    assert "b" not in out, "no call, no Insight"
    line = pg.insight("a", out["a"], min_calls=10)
    assert line["insight"] == 0.336 and line["games"] == 1 and line["formula"] == "progression:v1/insight"
    assert line["why"] == "calls beat the rating's own forecast by 0.34 per game over 1 game"
    assert line["calibration"]["line"] == "near 70%: 1 of 1 won"


def test_repeat_games_against_one_operator_flatten_by_one_over_root_n():
    games = [_g("s1", "a", "b", "a", 1, calls={"a": 70}), _g("s2", "a", "b", "b", 2, calls={"a": 70})]
    first, second = pg.calls_of(games)["a"]
    assert first.weight == 1.0 and second.weight == approx(1 / math.sqrt(2))
    # (0.336472·1 + w·e2) / (1 + w), with e2 the second game's own edge against its own forecast.
    mean = (first.edge + second.weight * second.edge) / (1 + second.weight)
    assert pg.insight("a", [first, second], min_calls=10)["insight"] == round(mean, 3)
    assert second.forecast > 0.5, "after a win the rating expects more of a"


def test_calling_the_ratings_own_number_scores_zero():
    """Fresh against fresh the rating says 50 %; so a 50 % call there scores nothing, won or lost."""
    games = [_g(f"s{i}", f"a{i}", f"b{i}", f"a{i}" if i % 2 else f"b{i}", i + 1, calls={f"a{i}": 50})
             for i in range(6)]
    by = pg.calls_of(games)
    assert [c.edge for i in range(6) for c in by[f"a{i}"]] == [0.0] * 6
    # And wherever the rating stands: a call equal to its forecast has no edge.
    views = {"s1": {"forecast": {"a": 0.62, "b": 0.38}, "weight": {"a": 1.0, "b": 1.0},
                    "settled": {"a": True, "b": True}}}
    for winner in ("a", "b"):
        [c] = pg.calls_of([_g("s1", "a", "b", winner, 1, calls={"a": 62})], views=views)["a"]
        assert c.edge == approx(0.0)


def test_a_losing_line_says_so_and_calibration_groups_the_calls():
    sure = [_g(f"s{i}", "a", f"o{i}", f"o{i}", i + 1, calls={"a": 90}) for i in range(3)]
    line = pg.insight("a", pg.calls_of(sure)["a"], min_calls=10)
    assert line["insight"] < 0 and line["why"].startswith("calls fell short of the rating's own forecast by ")
    assert line["why"].endswith("per game over 3 games")
    games = [_g("s1", "a", "b", "b", 1, calls={"a": 70}), _g("s2", "a", "c", "a", 2, calls={"a": 72}),
             _g("s3", "a", "d", "d", 3, calls={"a": 30})]
    line = pg.insight("a", pg.calls_of(games)["a"], min_calls=10)
    assert line["calibration"]["bins"] == [{"near": 0.3, "calls": 1, "won": 0.0, "rate": 0.0},
                                           {"near": 0.7, "calls": 2, "won": 1.0, "rate": 0.5}]
    assert line["calibration"]["line"] == "near 30%: 0 of 1 won · near 70%: 1 of 2 won"


def test_insight_never_moves_craft():
    plain = [_g("s1", "a", "b", "a", 1), _g("s2", "a", "c", "c", 2)]
    called = [{**plain[0], "calls": {"a": 99, "b": 1}}, {**plain[1], "calls": {"a": 1, "c": 99}}]
    r0, r1 = pg.craft_ratings(plain)[BLOTTO]["a"], pg.craft_ratings(called)[BLOTTO]["a"]
    assert (r0.rating, r0.rd, r0.vol) == (r1.rating, r1.rd, r1.vol)


# ── anti-farming ──────────────────────────────────────────────────────────────

def test_house_bounty_and_retired_games_count_nothing():
    base = _g("s1", "a", "b", "a", 1, calls={"a": 90})
    assert pg.calls_of([base], house=frozenset({"b"})) == {}
    assert pg.calls_of([{**base, "bounty": True}]) == {}
    assert pg.calls_of([{**base, "played_as": "emporia:blotto:v1"}]) == {}, "only the :v2 set plays take calls"
    assert pg.calls_of([{**base, "played_as": "emporia:chess:v1", "module_type": "emporia:chess:v1"}]) == {}


def test_same_operator_tables_count_nothing_for_anyone():
    who = {"a": pg.Who("nous:1", True), "b": pg.Who("nous:1", True), "c": pg.Who("nous:2", True)}
    two = _g("s1", "a", "b", "a", 1, calls={"a": 90})
    assert pg.calls_of([two], who) == {}
    three = {**two, "participants": ["a", "b", "c"], "calls": {"a": 60, "c": 40}, "termination": "closest"}
    assert pg.calls_of([three], who) == {}, "two seats of one operator: nobody's call counts"


def test_an_unverified_or_new_counterparty_weighs_little():
    who = {"a": pg.Who("nous:1", True), "b": pg.Who("b", False)}
    [c] = pg.calls_of([_g("s1", "a", "b", "a", 1, calls={"a": 70})], who)["a"]
    assert c.weight == approx(pg.UNVERIFIED_WEIGHT)   # registered_at unknown: no ramp, key-only: a quarter


def test_walking_away_from_a_sealed_call_scores_the_worst_call():
    """b sealed a call, saw a's reveal, and let its clock run out: scored as 99 % on a loss,
    ln 0.01 - ln 0.5 = -3.912023. Revealing could only have done better."""
    timeout = _g("s1", "a", "b", "a", 1, termination="timeout", calls={"a": 60}, called=["a", "b"])
    out = pg.calls_of([timeout])
    [c] = out["b"]
    assert c.call is None and c.edge == approx(-3.912023)
    assert out["a"][0].call == 0.6, "the revealed call stands as made"
    line = pg.insight("b", out["b"], min_calls=10)
    assert line["forfeited"] == 1 and "left unrevealed" in line["why"]
    assert line["calibration"]["line"] == "no calls revealed yet", "a forfeit has no call to calibrate"
    # In a pick game, the ones out; on an abandoned game, the one who walked away.
    out_game = {**timeout, "participants": ["a", "b", "c"], "out": ["c"], "called": ["c"], "calls": {}}
    # c, out of a table of three equals (forecast 1/3), lost: ln 0.01 - ln(2/3) = -4.199705.
    assert pg.calls_of([out_game])["c"][0].edge == approx(-4.199705)
    gone = {**_g("s3", "a", "b", None, 1, abandoned_by="b"), "called": ["b"], "calls": {}}
    assert pg.calls_of([gone])["b"][0].call is None


def test_a_call_left_unrevealed_through_no_fault_of_ones_own_is_not_scored():
    """a lost on time at its own reveal: b never got to reveal, and won. Nothing for b."""
    g = _g("s1", "a", "b", "b", 1, termination="timeout", called=["a", "b"], calls={})
    out = pg.calls_of([g])
    assert "b" not in out and out["a"][0].call is None


def test_the_board_counts_only_calls_made_with_a_settled_rating():
    # a plays 40 different opponents; its rating settles after a few games.
    games = [_g(f"s{i}", "a", f"o{i}", "a" if i % 3 else f"o{i}", 1 + i // 10, calls={"a": 60})
             for i in range(40)]
    games = [{**g, "ended_at": f"2026-11-{1 + i // 10:02d}T{i % 10:02d}:00:00+00:00"} for i, g in enumerate(games)]
    by = pg.calls_of(games)
    settled = [c for c in by["a"] if c.settled]
    assert 0 < len(settled) < 40 and not by["a"][0].settled, "the first calls were made while provisional"
    [row] = pg.insight_board(by, min_calls=10)
    assert row["agent_id"] == "a" and row["games"] == len(settled)
    assert pg.insight_board(by, min_calls=len(settled) + 1) == [], "the board starts at min_calls"
    line = pg.insight("a", by["a"], min_calls=10)
    assert line["settled_games"] == len(settled) and line["games"] == 40


def test_min_calls_reads_its_setting(monkeypatch):
    monkeypatch.setenv("EMPORIA_INSIGHT_MIN_CALLS", "3")
    assert pg.insight_min_calls() == 3
    for bad in ("0", "x", "-2", ""):
        monkeypatch.setenv("EMPORIA_INSIGHT_MIN_CALLS", bad)
        assert pg.insight_min_calls() == pg.INSIGHT_MIN_CALLS


# ── the call is bound by the commitment ───────────────────────────────────────

def _act(m, s, who, typ, payload):
    a = SessionAction(agent_id=who, action_type=typ, payload=payload)
    ok, why = m.validate_action(s, a)
    return (m.apply_action(s, a).new_state if ok else s), ok, why


def test_the_call_is_sealed_with_the_move_and_cannot_change():
    m, n = BlottoV2Module(), commit_reveal.new_nonce()
    s = m.initial_state(["a", "b"], {"battlefields": 3, "troops": 10})
    s, ok, _ = _act(m, s, "a", "commit", {"commitment": calls.commitment([5, 5, 0], 70, n), "called": True})
    s, ok2, _ = _act(m, s, "b", "commit", {"commitment": calls.commitment([0, 5, 5], None, n), "called": False})
    assert ok and ok2 and s.data["called"] == ["a"]
    for payload, why in (({"move": [5, 5, 0], "call_pct": 60, "nonce": n}, "does not match"),
                         ({"move": [5, 5, 0], "nonce": n}, "declared a call"),
                         ({"move": [5, 5, 0], "call_pct": 70.0, "nonce": n}, "declared a call")):
        _, ok, err = _act(m, s, "a", "reveal", payload)
        assert not ok and why in err, (payload, err)
    s, ok, _ = _act(m, s, "a", "reveal", {"move": [5, 5, 0], "call_pct": 70, "nonce": n})
    assert ok and s.data["calls"] == {"a": 70}
    _, ok, err = _act(m, s, "b", "reveal", {"move": [0, 5, 5], "call_pct": 40, "nonce": n})
    assert not ok and "declared no call" in err, "a call nobody declared cannot appear at the reveal"
    s, ok, _ = _act(m, s, "b", "reveal", {"move": [0, 5, 5], "call_pct": None, "nonce": n})
    assert ok and s.data["calls"] == {"a": 70} and m.is_terminal(s)[0]


def test_a_v2_commitment_is_not_a_v1_one():
    n = commit_reveal.new_nonce()
    assert calls.commitment([5, 5, 0], None, n) != commit_reveal.commitment([5, 5, 0], n)
    assert calls.commitment([5, 5, 0], None, n) == commit_reveal.commitment({"call_pct": None, "move": [5, 5, 0]}, n)
    assert not calls.matches(commit_reveal.commitment([5, 5, 0], n), [5, 5, 0], None, n)


def test_a_call_rides_on_the_first_move_only():
    m, n = BlottoV2Module(), commit_reveal.new_nonce()
    s = m.initial_state(["a", "b"], {"battlefields": 3, "troops": 10, "rounds": 3})
    s, *_ = _act(m, s, "a", "commit", {"commitment": calls.commitment([5, 5, 0], 80, n), "called": True})
    s, *_ = _act(m, s, "b", "commit", {"commitment": calls.commitment([0, 5, 5], None, n)})
    s, *_ = _act(m, s, "a", "reveal", {"move": [5, 5, 0], "call_pct": 80, "nonce": n})
    s, *_ = _act(m, s, "b", "reveal", {"move": [0, 5, 5], "nonce": n})
    assert s.data["round"] == 2 and s.data["calls"] == {"a": 80}
    _, ok, err = _act(m, s, "a", "commit", {"commitment": calls.commitment([5, 5, 0], 90, n), "called": True})
    assert not ok and "first move" in err
    _, ok, err = _act(m, s, "a", "commit", {"commitment": "0" * 64, "called": "yes"})
    assert not ok and "true or false" in err
    s, ok, _ = _act(m, s, "a", "commit", {"commitment": calls.commitment([5, 5, 0], None, n)})
    s, *_ = _act(m, s, "b", "commit", {"commitment": calls.commitment([0, 5, 5], None, n)})
    _, ok, err = _act(m, s, "a", "reveal", {"move": [5, 5, 0], "call_pct": 80, "nonce": n})
    assert not ok and "declared no call" in err, "round 1's call is not revealed again in round 2"
    s, ok, _ = _act(m, s, "a", "reveal", {"move": [5, 5, 0], "nonce": n})
    assert ok and s.data["calls"] == {"a": 80}, "the call survives the round's reset"


@pytest.mark.parametrize("cls,picks", [(TwoThirdsV2Module, {"a": 20, "b": 30, "c": 40}),
                                       (LowestUniqueV2Module, {"a": 1, "b": 2, "c": 2, "d": 3})])
def test_the_pick_games_seal_a_call_too(cls, picks):
    m, n = cls(), commit_reveal.new_nonce()
    s = m.initial_state(list(picks), {})
    for p, pick in picks.items():
        call = 55 if p == "a" else None
        s, ok, why = _act(m, s, p, "commit", {"commitment": calls.commitment(pick, call, n), "called": call is not None})
        assert ok, why
    _, ok, err = _act(m, s, "a", "reveal", {"move": picks["a"], "call_pct": 56, "nonce": n})
    assert not ok and "does not match" in err
    for p, pick in picks.items():
        s, ok, why = _act(m, s, p, "reveal", {"move": pick, "nonce": n, **({"call_pct": 55} if p == "a" else {})})
        assert ok, why
    assert s.data["calls"] == {"a": 55} and s.data["called"] == ["a"] and m.is_terminal(s)[0]


def test_the_agents_call_is_a_probability_in_whole_percent():
    assert calls.pct_of(None) is None and calls.pct_of(0.7) == 70 and calls.pct_of(0.01) == 1
    assert calls.pct_of(0.99) == 99 and calls.pct_of(0.255) in (25, 26)
    for bad in (0, 1, 0.005, 0.995, 1.5, -0.2, True, "0.7", [0.7]):
        with pytest.raises(ValueError):
            calls.pct_of(bad)


def test_commit_move_seals_the_call_and_reveal_move_sends_it(monkeypatch, tmp_path):
    from emporia import mcp_server as m
    state = {"round": 1, "phase": "commit", "commitments": {}, "battlefields": 5, "troops": 100}
    sent = []
    monkeypatch.setattr("emporia.identity.KEY_DIR", tmp_path)
    monkeypatch.setattr(m, "_session_json", lambda sid, aid: {"module_type": BLOTTO, "state": {"data": state}})
    monkeypatch.setattr(m, "_session_data", lambda sid, aid: state)
    monkeypatch.setattr(m, "_submit_simultaneous",
                        lambda sid, action, payload, rationale, aid, say="": sent.append((action, payload))
                        or {"success": True})
    for bad, why in ((1.2, "between 0.01 and 0.99"), ("high", "must be a number")):
        out = m.commit_move("sess_x", [20] * 5, "spread evenly this time", agent_id="me", call=bad)
        assert not out["success"] and why in out["error"] and "Nothing was committed" in out["error"]
    assert not sent
    assert m.commit_move("sess_x", [20] * 5, "spread evenly this time", agent_id="me", call=0.64)["success"]
    [(action, payload)] = sent
    assert action == "commit" and payload["called"] is True and set(payload) == {"commitment", "called"}
    state["commitments"] = {"me": payload["commitment"]}
    assert m.reveal_move("sess_x", "revealing as committed", agent_id="me")["success"]
    action, reveal = sent[-1]
    assert action == "reveal" and reveal["call_pct"] == 64
    assert calls.matches(payload["commitment"], reveal["move"], 64, reveal["nonce"])
    retry = m.commit_move("sess_x", [20] * 5, "spread evenly this time", agent_id="me", call=0.9)
    assert not retry["success"] and "already committed" in retry["error"], "a call cannot change on a retry"


def test_commit_move_refuses_a_call_where_none_is_taken(monkeypatch, tmp_path):
    from emporia import mcp_server as m
    monkeypatch.setattr("emporia.identity.KEY_DIR", tmp_path)
    for mt, data in (("emporia:blotto:v1", {"round": 1}), (BLOTTO, {"round": 2})):
        monkeypatch.setattr(m, "_session_json", lambda sid, aid, mt=mt, data=data: {
            "module_type": mt, "state": {"data": {**data, "commitments": {}, "battlefields": 5, "troops": 100}}})
        out = m.commit_move("sess_x", [20] * 5, "spread evenly this time", agent_id="me", call=0.6)
        assert not out["success"] and ("takes no call" in out["error"] or "first move" in out["error"])


# ── Bargain takes no call (gamify-all: calls and Bargain together) ──────────

BARGAIN = "emporia:bargain:v1"


def test_bargain_takes_no_call(monkeypatch, tmp_path):
    """A bargain is scored out of 10 (each side's own value), not won or lost, so a
    probability of winning means nothing there: it is no :v2 set play, and commit_move
    refuses a call on one before anything is committed."""
    from emporia import mcp_server as m
    assert not calls.takes_calls(BARGAIN) and BARGAIN not in calls.ALL_SET_PLAYS
    assert MODULE_REGISTRY[BARGAIN].RETIRED_FOR is None
    monkeypatch.setattr("emporia.identity.KEY_DIR", tmp_path)
    monkeypatch.setattr(m, "_session_json", lambda sid, aid: {"module_type": BARGAIN, "state": {"data": {
        "round": 1, "offers": [], "pool": {"silk": 3, "spice": 2, "amber": 1}}}})
    sent = []
    monkeypatch.setattr(m, "submit_action", lambda *a, **k: sent.append(a) or {"success": True})
    out = m.commit_move("sess_b", [1], "sealing a call on a bargain", agent_id="me", call=0.6)
    assert not out["success"] and "takes no call" in out["error"] and not sent
    assert not list(tmp_path.rglob("*commit*")), "nothing was saved to reveal"


def _bargain(sid, a, b, day, sa, sb, **extra):
    return {"session_id": sid, "module_type": BARGAIN, "played_as": BARGAIN, "participants": [a, b],
            "winner": a if sa > sb else (b if sb > sa else None), "termination": "deal",
            "rating_scores": {a: sa / 10, b: sb / 10}, "ended_at": f"2026-11-{day:02d}T00:00:00+00:00", **extra}


def test_insight_never_reads_a_bargain_and_craft_rates_its_own_score():
    """Craft rates a bargain by each side's own score (both gain from a good deal, both lose
    from none); Insight counts no bargain, even one whose record somehow carried calls, and
    a bargain in the same history does not move a blotto call's score."""
    good, none = _bargain("b1", "a", "b", 1, 7, 7), _bargain("b2", "c", "d", 1, 0, 0)
    t = pg.craft_ratings([good, none])[BARGAIN]
    assert t["a"].rating > 1500 and t["b"].rating > 1500, "a deal good for both raises both"
    assert t["c"].rating < 1500 and t["d"].rating < 1500, "no deal lowers both"
    assert pg.calls_of([{**good, "calls": {"a": 90}}]) == {}, "no call is read from a bargain"
    blotto = _g("s1", "a", "b", "a", 2, calls={"a": 70})
    alone, mixed = pg.calls_of([blotto]), pg.calls_of([good, blotto])
    assert [c.session_id for c in mixed["a"]] == ["s1"]
    assert mixed["a"][0].forecast == alone["a"][0].forecast, "Insight's forecast is per game type"
    board = pg.season_boards([good, blotto], [], None, 0.0, {"season": "s", "started_at": None, "ends_at": None})
    assert BARGAIN in board["boards"] and board["boards"]["insight"] == []


def test_strike_a_bargain_is_no_call_quest():
    from emporia import quests
    g = {"session_id": "b1", "module_type": BARGAIN, "participants": ["a", "b"], "termination": "deal",
         "ended_at": "2026-11-05T00:00:00+00:00", "calls": {"a": 60}}
    done = {q["id"] for q in quests.progress("a", posts=[], games=[g], deals=[], reports=[])["quests"] if q["done"]}
    assert {"first-set-play", "bargain"} <= done and "call" not in done


def test_the_call_argument_is_documented_for_the_agent():
    from emporia import mcp_server
    import asyncio
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    spec = tools["commit_move"].parameters["properties"]["call"]
    assert "0.01-0.99" in spec["description"] and "never Craft" in spec["description"]
    assert set(tools["commit_move"].parameters["required"]) == {"session_id", "move", "rationale"}


# ── v1 → v2 ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51231))


def test_v1_set_plays_are_retired_loudly(client):
    from tests.test_emporia import _register_keyed
    a = f"cv_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    for old, new in calls.RETIRED.items():
        r = client.post("/sessions", json={"module_type": old, "creator_agent_id": a},
                        headers={"x-emporia-agent-id": a})
        assert r.status_code == 400 and "retired" in r.json()["detail"] and new in r.json()["detail"]
        assert MODULE_REGISTRY[old].RETIRED_FOR == new and MODULE_REGISTRY[new].RETIRED_FOR is None
    listed = {m["module_type"]: m for m in list_available_modules()}
    assert listed["emporia:blotto:v1"]["retired_for"] == BLOTTO and "retired_for" not in listed[BLOTTO]
    assert "Call (optional)" in listed[BLOTTO]["rules"] and "Call" not in listed["emporia:blotto:v1"]["rules"]
    from emporia.bounties import judging_modules
    judges = judging_modules(MODULE_REGISTRY)
    assert BLOTTO in judges and "emporia:blotto:v1" not in judges, "no new contest on a retired game"


def test_a_v1_game_already_running_finishes_under_its_old_rules(client, monkeypatch):
    """Made before the upgrade (retirement lifted for the moment it takes to create it), it is
    played to the end with plain v1 commitments, and its result counts toward blotto Craft."""
    from tests.test_emporia import _register_keyed, _signed_action_body
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    a, b = f"cva_{uuid.uuid4().hex[:6]}", f"cvb_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    monkeypatch.setattr(BlottoModule, "RETIRED_FOR", None)
    r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a},
                    headers={"x-emporia-agent-id": a})
    assert r.status_code == 200, r.text
    monkeypatch.undo()
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert MODULE_REGISTRY["emporia:blotto:v1"].RETIRED_FOR == BLOTTO
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b}).status_code == 200

    def act(who, typ, payload):
        body = _signed_action_body(client, sid, who, typ, payload, "playing the old game to its end")
        return client.post(f"/sessions/{sid}/action", json=body)
    moves, nonces = {a: [30, 30, 30, 5, 5], b: [20] * 5}, {a: commit_reveal.new_nonce(), b: commit_reveal.new_nonce()}
    for who in (a, b):
        assert act(who, "commit", {"commitment": commit_reveal.commitment(moves[who], nonces[who])}).status_code == 200
    r = act(a, "reveal", {"move": moves[a], "nonce": nonces[a], "call_pct": 70})
    assert r.status_code == 200, "a v1 reveal ignores what v1 never had"
    r = act(b, "reveal", {"move": moves[b], "nonce": nonces[b]})
    assert r.status_code == 200 and r.json()["outcome"]["winner"] == a
    assert "calls" not in rs.load_session(sid)["state"]["data"], "a v1 game has no calls"
    g = next(g for g in st._craft_games() if g["session_id"] == sid)
    assert g["module_type"] == BLOTTO and g["played_as"] == "emporia:blotto:v1"
    sheet = client.get(f"/agents/{a}/sheet").json()
    assert BLOTTO in {c["module_type"] for c in sheet["craft"]} and sheet["insight"]["games"] == 0
    ladder = client.get("/ratings", params={"module_type": "emporia:blotto:v1"}).json()
    assert ladder["module_type"] == BLOTTO and a in [x["agent_id"] for x in ladder["ratings"]]


def test_a_playbook_naming_the_old_game_takes_the_new_one():
    from emporia import playbook as P
    assert P.parse_games("blotto,two-thirds") == (BLOTTO, "emporia:two-thirds:v2")
    old = P.Playbook("player", ("emporia:blotto:v1",), 100, "anyone", False, False, "model")
    assert P.may_play(old, BLOTTO) == "" and P.may_play(P.PRESETS["player"], "emporia:blotto:v1") == ""
    assert P.may_play(P.PRESETS["player"], "emporia:chess:v1") != ""


# ── through the relay: the sheet, the season, the quest ──────────────────────

def _session(module, participants, data, updated, step=4):
    from tests.test_progression import _register
    for p in participants:
        _register(p)
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    state = {"data": data, "current_agent": participants[0], "step_number": step,
             "metadata": {"participants": participants}}
    with rs.DB_LOCK, rs.get_db() as conn:
        for p in participants:
            conn.execute("INSERT INTO participants (session_id, agent_id, joined_at) VALUES (?, ?, ?)",
                         (sid, p, "2026-11-01T00:00:00+00:00"))
        conn.execute("INSERT INTO sessions (session_id, module_type, config_json, payment_rules_json, state_json, "
                     "current_agent, step_number, status, participants_json, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, module, "{}", "{}", json.dumps(state), participants[0], step, "completed",
                      json.dumps(participants), "2026-11-01T00:00:00+00:00", updated))
    return sid


def _won(a, b, call, updated):
    return _session(BLOTTO, [a, b], {"wins": {a: 1, b: 0}, "history": [{}], "rounds": 1, "battlefields": 5,
                                     "troops": 100, "round": 2, "phase": "commit", "commitments": {}, "revealed": {},
                                     "turn_started_at": "2026-11-01T00:00:00+00:00",
                                     "called": [a], "calls": {a: call}}, updated)


def test_the_sheet_season_and_quests_show_insight(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_INSIGHT_MIN_CALLS", "1")
    tag = uuid.uuid4().hex[:6]
    a, b = f"ci_a_{tag}", f"ci_b_{tag}"
    _won(a, b, 70, "2026-11-02T00:00:00+00:00")
    sheet = client.get(f"/agents/{a}/sheet").json()
    ins = sheet["insight"]
    assert ins["games"] == 1 and ins["insight"] == 0.336 and ins["formula"] == pg.INSIGHT_FORMULA
    assert ins["why"] == "calls beat the rating's own forecast by 0.34 per game over 1 game"
    assert ins["recent"][0]["call"] == 0.7 and sheet["signature"], "signed with the rest of the sheet"
    assert client.get(f"/agents/{b}/sheet").json()["insight"]["games"] == 0
    shown = pg.render(sheet)
    assert "Insight  +0.34 per game" in shown and "near 70%: 1 of 1 won" in shown
    season = client.get("/season").json()
    assert "insight" in season["order"] and season["insight_min_calls"] == 1
    assert a not in [r["agent_id"] for r in season["boards"]["insight"]], "a provisional rating's calls wait"
    quests = client.get(f"/agents/{a}/quests").json()
    call = next(q for q in quests["quests"] if q["id"] == "call")
    assert call["done"] and call["optional"] and quests["formula"] == "quests:v3"
    other = next(q for q in client.get(f"/agents/{b}/quests").json()["quests"] if q["id"] == "call")
    assert not other["done"] and "commit_move" in other["how"]
    assert "Make a call" not in quests["next"], "an optional quest is never `next`"
