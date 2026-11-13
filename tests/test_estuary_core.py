"""Estuary core: pure rules, whole numbers, replayable from a seed."""

from fractions import Fraction

import pytest

from emporia.modules import estuary_core as ec

R = ec.DEFAULT
SEED = b"estuary-test-seed"


def test_topology_has_sixty_channels_with_three_mouths():
    ids = ec.channel_ids(R.sizes())
    assert len(ids) == 60 == ec.n_channels(R)
    assert ids[0] == "a1>b1" and ids[-1] == "c5>d3"
    assert len(set(ids)) == 60


def test_draw_is_deterministic_and_in_range():
    w = ec.open_widths(SEED)
    assert w == ec.open_widths(SEED)
    assert w != ec.open_widths(b"other")
    assert all(-20 <= x <= 20 for x in w)
    rain = ec.draw_rain(SEED, 1)
    assert len(rain) == 16 and all(len(s) == 4 and all(0 <= v <= 9 for v in s) for s in rain)
    assert rain != ec.draw_rain(SEED, 2)
    silt = ec.draw_silt(SEED, 1)
    assert len(silt) == 60 and silt == ec.draw_silt(SEED, 1)


def test_draws_are_roughly_uniform():
    counts = [0] * 10
    for e in range(1, 60):
        for s in ec.draw_rain(SEED, e):
            for v in s:
                counts[v] += 1
    total = sum(counts)
    assert all(abs(c / total - 0.1) < 0.02 for c in counts)
    blocked = sum(sum(ec.draw_silt(SEED, e)) for e in range(1, 200))
    assert abs(blocked / (200 * 60 - 60) - 1 / 8) < 0.02


def test_forward_hand_computed():
    # all widths 1: layer b = sum of sources, c = 5*sum, d = 25*sum each
    w = (1,) * 60
    out = ec.forward(w, (1, 2, 3, 4), None)
    assert out == [250, 250, 250]
    # a negative width cuts a pool to zero (max(0, .))
    w2 = tuple(-1 if i < 5 else 1 for i in range(60))  # a1>b* negative
    out2 = ec.forward(w2, (1, 0, 0, 0), None)
    assert out2 == [0, 0, 0]
    # blocked channels carry nothing
    blocked = (True,) * 60
    assert ec.forward(w, (9, 9, 9, 9), blocked) == [0, 0, 0]


def test_shares_sum_to_one_and_all_zero_is_equal():
    sh = ec.shares([1, 2, 5])
    assert sum(sh) == 1 and sh[2] == Fraction(5, 8)
    assert ec.shares([0, 0, 0]) == (Fraction(1, 3),) * 3
    assert all(isinstance(x, Fraction) for x in sh)


def test_validate_move():
    ok = ec.validate_move({"back": 2, "push": {"a1>b3": 4, "b3>c2": -4, "c2>d2": 3, "c4>d1": 1}})
    assert ok["back"] == 2
    for bad in (
        {"back": 0, "push": {}},
        {"back": 4, "push": {}},
        {"back": True, "push": {}},
        {"back": 1, "push": {"zz": 1}},
        {"back": 1, "push": {"a1>b1": 1.5}},
        {"back": 1, "push": {"a1>b1": 13}},
        {
            "back": 1,
            "push": {f"a{i}>b1": 1 for i in range(1, 5)} | {"b1>c1": 1, "b2>c1": 1, "b3>c1": 1},
        },
        {"back": 1, "x": 1},
    ):
        with pytest.raises(ValueError):
            ec.validate_move(bad)


def test_apply_pushes_adds_clamps_and_carries():
    w = (19,) + (0,) * 59
    mv = [{"back": 1, "push": {"a1>b1": 5}}, {"back": 1, "push": {"a1>b1": 4, "a1>b2": -3}}]
    out = ec.apply_pushes(w, mv)
    assert out[0] == 20 and out[1] == -3
    # order independence
    assert out == ec.apply_pushes(w, mv[::-1])


def test_score_splits_by_backers():
    avg = (Fraction(1, 2), Fraction(1, 4), Fraction(1, 4))
    s = ec.score({"a": 1, "b": 1, "c": 2}, avg)
    assert s == {"a": Fraction(1, 4), "b": Fraction(1, 4), "c": Fraction(1, 4)}


def test_pull_matches_direct_plus_one():
    w = ec.open_widths(SEED)
    samples = ec.draw_rain(SEED, 1)
    base = ec.avg_share(w, samples, None)
    p = ec.pull(w, samples, None)
    ids = ec.channel_ids(R.sizes())
    n = 17
    w2 = list(w)
    w2[n] += 1
    assert tuple(a - b for a, b in zip(ec.avg_share(tuple(w2), samples, None), base)) == p[ids[n]]
    # shares sum to 1, so a channel's pulls over the mouths sum to 0
    assert all(sum(v) == 0 for v in p.values())


def test_credit_leave_one_out_and_zero_for_no_push():
    w = ec.open_widths(SEED)
    moves = {
        "a": {"back": 1, "push": {"c1>d1": 6, "c2>d1": 6}},
        "b": {"back": 2, "push": {}},
        "c": {"back": 3, "push": {"c1>d3": 12}},
    }
    r = ec.play_epoch(SEED, 1, w, moves)
    assert all(v == 0 for v in r.credit["b"])
    assert all(sum(v) == 0 for v in r.credit.values())
    rest = ec.apply_pushes(w, [moves["b"], moves["c"]])
    expect = tuple(x - y for x, y in zip(r.avg, ec.avg_share(rest, r.samples, r.blocked)))
    assert r.credit["a"] == expect


def test_replay_is_exact_and_changes_with_a_tampered_move():
    ep1 = {"a": {"back": 1, "push": {"c1>d1": 12}}, "b": {"back": 2, "push": {"c1>d2": 12}}}
    ep2 = {"a": {"back": 3, "push": {}}, "b": {"back": 3, "push": {"b1>c1": 5}}}
    res, tot = ec.play_game(SEED, [ep1, ep2])
    res2, tot2 = ec.play_game(SEED, [ep1, ep2])
    assert tot == tot2 and all(isinstance(v, Fraction) for v in tot.values())
    assert res[1].widths_before == res[0].widths_after  # the network carries over
    ep1b = {**ep1, "a": {"back": 2, "push": {"c1>d1": 11}}}
    _, tot3 = ec.play_game(SEED, [ep1b, ep2])
    assert tot3 != tot


def test_totals_and_winners():
    assert ec.winners({"a": Fraction(1), "b": Fraction(1), "c": Fraction(1, 2)}) == ["a", "b"]
    assert ec.winners({}) == []


def test_rules_override_mouths():
    r = ec.Rules(mouths=5, silt_den=0)
    assert ec.n_channels(r) == 4 * 5 + 25 + 25
    assert not any(ec.draw_silt(SEED, 1, r))
    res = ec.play_epoch(SEED, 1, ec.open_widths(SEED, r), {"a": {"back": 5, "push": {}}}, r)
    assert len(res.avg) == 5 and sum(res.avg) == 1


def test_crowd_pow_squares_the_split():
    avg = (Fraction(1, 2), Fraction(1, 4), Fraction(1, 4))
    s = ec.score({"a": 1, "b": 1, "c": 2}, avg, crowd_pow=2)
    assert s == {"a": Fraction(1, 8), "b": Fraction(1, 8), "c": Fraction(1, 4)}
    r = ec.Rules(crowd_pow=2)
    res = ec.play_epoch(
        SEED,
        1,
        ec.open_widths(SEED, r),
        {"a": {"back": 1, "push": {}}, "b": {"back": 1, "push": {}}},
        r,
    )
    assert res.scores["a"] == res.avg[0] / 4
