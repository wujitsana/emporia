"""Tesseract core: pure rules, whole numbers, replayable from a seed."""

import ast
import hashlib
import inspect
from dataclasses import replace

import pytest

from emporia.modules import tesseract_core as tc

R = tc.DEFAULT
SEED = b"tesseract-test-seed"


def push(**kw):
    return {"push": {k.replace("_", ">"): v for k, v in kw.items()}}


def evens(v):
    return (v,) * 24


# -- board ---------------------------------------------------------------------


def test_twenty_four_channels_each_a_one_letter_drop_in_fixed_order():
    ids = tc.channel_ids()
    assert len(ids) == 24 == tc.n_channels() and len(set(ids)) == 24
    assert ids[0] == "XYZ>XY"  # first input; the first hub it feeds (WX is not in XYZ)
    assert ids[11] == "WXY>XY" and ids[12] == "WX>W" and ids[-1] == "YZ>Z"
    for cid in ids:
        a, b = cid.split(">")
        assert set(b) < set(a) and len(a) - len(b) == 1
    # input-to-hub first (12), then hub-to-output (12)
    assert all(len(c.split(">")[0]) == 3 for c in ids[:12])
    assert all(len(c.split(">")[1]) == 1 for c in ids[12:])
    assert tc.channel_index()["XY>X"] == ids.index("XY>X")


def test_degrees():
    ids = tc.channel_ids()
    for hub in ("WX", "WY", "WZ", "XY", "XZ", "YZ"):
        assert sum(c.endswith(">" + hub) for c in ids) == 2  # fed by 2 inputs
        assert sum(c.startswith(hub + ">") for c in ids) == 2  # feeds 2 outputs
    for out in "WXYZ":
        assert sum(c.endswith(">" + out) for c in ids) == 3  # fed by 3 hubs
    for inp in ("XYZ", "WYZ", "WXZ", "WXY"):
        assert sum(c.startswith(inp + ">") for c in ids) == 3
    # 6 routes to each output
    assert tc.topology()[0][:4] == ("XYZ", "WYZ", "WXZ", "WXY")


# -- draws -----------------------------------------------------------------------


def test_draws_deterministic_and_in_range():
    p = tc.draw_pulses(SEED, 1)
    assert p == tc.draw_pulses(SEED, 1) and p != tc.draw_pulses(SEED, 2)
    assert len(p) == 16 and all(len(s) == 4 and all(0 <= v <= 9 for v in s) for s in p)
    d = tc.draw_dark(SEED, 1)
    assert len(d) == 24 and d == tc.draw_dark(SEED, 1)
    w, a = tc.open_network(SEED)
    assert (w, a) == tc.open_network(SEED) and w != tc.open_network(b"other")[0]
    assert all(1 <= x <= 20 for x in w)


def test_draws_roughly_uniform():
    counts = [0] * 10
    for e in range(1, 60):
        for s in tc.draw_pulses(SEED, e):
            for v in s:
                counts[v] += 1
    total = sum(counts)
    assert all(abs(c / total - 0.1) < 0.02 for c in counts)
    dark = sum(sum(tc.draw_dark(SEED, e)) for e in range(1, 400))
    assert abs(dark / (399 * 24) - 1 / 24) < 0.01


def test_opening_inside_the_band_for_100_seeds():
    attempts = []
    for i in range(100):
        seed = f"open-{i}".encode()
        w, a = tc.open_network(seed)
        assert a >= 0
        m = tc.mix(w, tc.draw_pulses(seed, 0), None)
        assert all(1500 <= x <= 3500 for x in m)
        attempts.append(a)
    assert sum(attempts) / 100 < 6


def test_opening_gives_up_after_the_attempt_limit():
    hard = replace(R, band_lo=2499, band_hi=2501, open_attempts=3)
    w, a = tc.open_network(SEED, hard)
    assert a == -1 and len(w) == 24
    st = tc.Stream(SEED, "open/2")  # the last allowed attempt stands
    assert w == tuple(st.below(20) + 1 for _ in range(24))


# -- targets ---------------------------------------------------------------------


def test_726_targets_all_forty_from_even_and_sorted():
    t = tc.targets()
    assert len(t) == 726 and list(t) == sorted(t) and len(set(t)) == 726
    for x in t:
        assert sum(x) == 100 and all(10 <= v <= 40 for v in x)
        assert sum(abs(v - 25) for v in x) == 40


def test_deal_target_is_uniform_and_follows_the_draw():
    t = tc.targets()
    n = 7260
    counts = {}
    for i in range(n):
        d = hashlib.sha256(f"seat{i}".encode()).digest()
        x = tc.deal_target(d)
        assert x in t
        counts[x] = counts.get(x, 0) + 1
    assert len(counts) > 700
    assert max(counts.values()) <= 25  # mean 10; a loose bound
    assert tc.deal_target((5).to_bytes(32, "big")) == t[5]
    assert tc.deal_target((726 + 5).to_bytes(32, "big")) == t[5]


# -- forward pass and mix --------------------------------------------------------


def test_equal_widths_equal_inputs_give_156_per_pulse():
    pulses = ((3, 3, 3, 3),) * 16
    m = tc.mix(evens(1), pulses, None)
    assert m == (2496, 2496, 2496, 2496)  # 16 * (625 // 4)


def test_hand_computed_forward_and_mix():
    # all widths 1, pulse (1,0,0,0): XYZ lacks W. Input 0 = XYZ feeds hubs XY, XZ, YZ.
    out = tc.forward(evens(1), (1, 0, 0, 0), None)
    # hubs XY=1 XZ=1 YZ=1, others 0. W is fed by WX, WY, WZ -> 0; X by WX, XY, XZ -> 2;
    # Y by WY, XY, YZ -> 2; Z by WZ, XZ, YZ -> 2
    assert out == [0, 2, 2, 2]
    pulses = ((1, 0, 0, 0),) * 16
    # share 625 * 2 // 6 = 208 per output, 0 for W; 16 pulses
    assert tc.mix(evens(1), pulses, None) == (0, 3328, 3328, 3328)
    # unequal inputs give unequal outputs even at equal widths
    assert tc.forward(evens(1), (4, 0, 0, 0), None) == [0, 8, 8, 8]
    # the second input, WYZ, feeds hubs WY, WZ, YZ
    assert tc.forward(evens(1), (0, 1, 0, 0), None) == [2, 0, 2, 2]
    assert tc.forward(evens(1), (4, 1, 0, 0), None) == [2, 8, 10, 10]


def test_all_zero_outputs_give_156_each():
    assert tc.mix(evens(0), ((5, 5, 5, 5),) * 16, None) == (2496,) * 4
    assert tc.mix(evens(3), ((0, 0, 0, 0),) * 16, None) == (2496,) * 4


def test_a_dark_channel_counts_as_zero_for_the_epoch():
    ids = tc.channel_ids()
    dark = tuple(c == "XYZ>XY" for c in ids)
    a = tc.forward(evens(1), (1, 0, 0, 0), dark)
    assert a == [0, 1, 1, 2]  # hub XY got nothing; undark it and it is [0, 2, 2, 2]


# -- moves -----------------------------------------------------------------------


def test_validate_move():
    ok = tc.validate_move(push(XYZ_XY=4, XY_X=-4))
    assert ok == {"push": {"XYZ>XY": 4, "XY>X": -4}}
    assert tc.validate_move({"push": {}}) == {"push": {}}
    assert tc.validate_move({"push": {"XYZ>XY": 0}}) == {"push": {}}
    with pytest.raises(ValueError):  # over budget
        tc.validate_move(push(XYZ_XY=5, XY_X=4))
    with pytest.raises(ValueError):  # five channels
        tc.validate_move(push(XYZ_XY=1, XY_X=1, XY_W=0, WX_W=1, WX_X=1, YZ_Z=1))
    with pytest.raises(ValueError):  # unknown channel
        tc.validate_move(push(AB_C=1))
    with pytest.raises(ValueError):  # not whole
        tc.validate_move({"push": {"XYZ>XY": 1.0}})
    with pytest.raises(ValueError):
        tc.validate_move({"push": {"XYZ>XY": True}})
    with pytest.raises(ValueError):  # no "back"
        tc.validate_move({"push": {}, "back": "W"})
    with pytest.raises(ValueError):
        tc.validate_move({"XYZ>XY": 1})
    capped = replace(R, unit_cap=4)
    tc.validate_move(push(XYZ_XY=4, XY_X=4), capped)
    with pytest.raises(ValueError):
        tc.validate_move(push(XYZ_XY=5), capped)


def test_pushes_sum_then_clamp():
    w = list(evens(10))
    i = tc.channel_index()["XYZ>XY"]
    out = tc.apply_pushes(tuple(w), [push(XYZ_XY=8), push(XYZ_XY=8)])
    assert out[i] == 20  # 26 clamped
    out = tc.apply_pushes(tuple(w), [push(XYZ_XY=8), push(XYZ_XY=-8)])
    assert out[i] == 10  # pushes net out
    low = tc.apply_pushes(evens(1), [push(XYZ_XY=-8)])
    assert low[i] == 0


# -- points ----------------------------------------------------------------------

T_W = (40, 20, 20, 20)  # wants W
T_X = (20, 40, 20, 20)
PULSES = tc.draw_pulses(b"pts", 1)
W0 = tc.open_network(b"pts")[0]
NO_DARK = (False,) * 24


def pts(moves, tg, widths=W0, dark=NO_DARK, rules=R):
    return tc.epoch_points(widths, moves, tg, PULSES, dark, rules)


def test_empty_push_scores_zero():
    out = pts({"a": {"push": {}}, "b": push(WX_W=4)}, {"a": T_W, "b": T_W})
    assert out["a"] == {"first": 0, "last": 0, "points": 0}


def test_lone_player_first_equals_last():
    out = pts({"a": push(WX_W=4, WY_W=4)}, {"a": T_W})["a"]
    assert out["first"] == out["last"] and out["points"] == 2 * out["first"]


def test_two_players_on_one_route_have_last_above_first():
    # A widens an input channel, B the hub-to-output channel it feeds: they multiply
    # search the routes: an input-to-hub channel and a channel out of that hub
    found = False
    for a in tc.channel_ids()[:12]:
        hub = a.split(">")[1]
        for o in hub:
            b = f"{hub}>{o}"
            if w_cap_ok(a) and w_cap_ok(b):
                tg = {"a": tuple(40 if k == "WXYZ".index(o) else 20 for k in range(4))}
                tg["b"] = tg["a"]
                out = pts({"a": push(**{a.replace(">", "_"): 4}), "b": push(**{b.replace(">", "_"): 4})}, tg)
                if out["a"]["last"] > out["a"]["first"] and out["b"]["last"] > out["b"]["first"]:
                    found = True
    assert found


def w_cap_ok(cid):
    return 4 <= W0[tc.channel_index()[cid]] <= 16


def test_a_push_on_a_dark_channel_scores_zero_that_epoch():
    cid = "WX>W"
    dark = tuple(c == cid for c in tc.channel_ids())
    out = pts({"a": push(WX_W=4)}, {"a": T_W}, dark=dark)["a"]
    assert out == {"first": 0, "last": 0, "points": 0}


def test_a_push_the_clamp_removes_scores_zero():
    w = list(W0)
    w[tc.channel_index()["WX>W"]] = 20
    out = pts({"a": push(WX_W=4)}, {"a": T_W}, widths=tuple(w))["a"]
    assert out["points"] == 0
    w[tc.channel_index()["WX>W"]] = 0
    out = pts({"a": push(WX_W=-4)}, {"a": T_W}, widths=tuple(w))["a"]
    assert out["points"] == 0


def test_points_do_not_move_when_another_players_target_changes():
    mv = {"a": push(WX_W=4, XY_X=-4), "b": push(YZ_Z=4)}
    one = pts(mv, {"a": T_W, "b": T_X})
    two = pts(mv, {"a": T_W, "b": (10, 10, 40, 40)})
    assert one["a"] == two["a"]


def test_a_push_away_from_the_target_scores_below_zero():
    out = pts({"a": push(WX_W=4, WY_W=4)}, {"a": (10, 30, 30, 30)})["a"]
    assert out["points"] < 0


def test_squared_scoring_and_score_modes():
    sq = replace(R, score_power=2)
    mv = {"a": push(WX_W=4, WY_W=4), "b": push(YZ_Z=4, XZ_Z=4)}
    tg = {"a": T_W, "b": T_X}
    lin, quad = pts(mv, tg), pts(mv, tg, rules=sq)
    assert lin["a"]["points"] != quad["a"]["points"]
    assert tc.distance((3000, 2000, 2500, 2500), (40, 20, 20, 20), sq) == 1000**2 + 500**2 + 500**2
    last = pts(mv, tg, rules=replace(R, score_mode="last"))
    assert last["a"]["points"] == lin["a"]["last"]
    mn = pts(mv, tg, rules=replace(R, score_mode="min"))
    assert mn["a"]["points"] == 2 * min(lin["a"]["first"], lin["a"]["last"])


# -- pull ------------------------------------------------------------------------


def test_pull_rows_and_toward():
    pl = tc.pull(W0, PULSES)
    assert list(pl) == list(tc.channel_ids())
    base = tc.mix(W0, PULSES, None)
    row = pl["WX>W"]
    w = list(W0)
    w[tc.channel_index()["WX>W"]] = min(20, w[tc.channel_index()["WX>W"]] + 4)
    assert tuple(a + b for a, b in zip(base, row["up"], strict=False)) == tc.mix(tuple(w), PULSES, None)
    g = tc.toward(row, base, T_W)
    assert g["up"] == tc.distance(base, T_W) - tc.distance(tc.mix(tuple(w), PULSES, None), T_W)
    # a clamped push reports the clamped effect
    capped = list(W0)
    capped[tc.channel_index()["WX>W"]] = 20
    assert tc.pull(tuple(capped), PULSES)["WX>W"]["up"] == (0, 0, 0, 0)


# -- game ------------------------------------------------------------------------


def _scripted_game():
    seeds = [hashlib.sha256(f"e{e}".encode()).digest() for e in range(1, 6)]
    moves = []
    for _e in range(5):
        moves.append(
            {
                "a": push(WX_W=4, WY_W=2, XYZ_XY=2),
                "b": push(YZ_Z=4, XZ_X=-3),
                "c": {"push": {}},
            }
        )
    tg = {"a": T_W, "b": T_X, "c": (10, 10, 40, 40)}
    return b"game", seeds, moves, tg


def test_full_game_replays_identically():
    s0, seeds, moves, tg = _scripted_game()
    pub1, pts1, tot1 = tc.play_game(s0, seeds, moves, tg)
    pub2, pts2, tot2 = tc.play_game(s0, seeds, moves, tg)
    assert pub1 == pub2 and pts1 == pts2 and tot1 == tot2
    assert len(pub1) == 5 and tot1["c"] == 0
    assert tot1["a"] == sum(p["a"]["points"] for p in pts1)
    # widths carry over and pushes are published with their owners at once
    assert pub1[1].widths_before == pub1[0].widths_after
    assert pub1[0].pushes["a"] == {"WX>W": 4, "WY>W": 2, "XYZ>XY": 2}
    assert tc.winners(tot1) == [max(tot1, key=tot1.get)]
    assert len(pub1[0].pull) == 24


def test_play_game_refuses_a_bad_move():
    s0, seeds, moves, tg = _scripted_game()
    moves[2]["a"] = push(WX_W=9)
    with pytest.raises(ValueError):
        tc.play_game(s0, seeds, moves, tg)


def test_winners_and_table_index():
    assert tc.winners({"a": 5, "b": 9, "c": 9}) == ["b", "c"]
    assert tc.winners({"a": -1, "b": -2}) == ["a"]
    idx = tc.table_index({"a": 300, "b": 100, "c": -50, "d": 0})
    assert idx == {"a": 3000, "b": 1000, "c": 0, "d": 0}
    assert sum(idx.values()) <= 4000
    assert tc.table_index({"a": -1, "b": 0}) == {"a": 0, "b": 0}
    assert tc.table_index({"a": 10, "b": 10}) == {"a": 1000, "b": 1000}


def test_module_has_no_float_division_or_fraction():
    tree = ast.parse(inspect.getsource(tc))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            assert not isinstance(node.value, float)
        if isinstance(node, ast.BinOp):
            assert not isinstance(node.op, (ast.Div, ast.Pow))
        if isinstance(node, ast.Name):
            assert node.id not in ("float", "Fraction")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert "fractions" not in names and "random" not in names


def test_a_table_defaults_to_three_seats_the_least_the_rules_allow():
    from emporia.modules import tesseract as T
    assert T.DEFAULTS["players"] == 3 == T.BOUNDS["players"][0]
