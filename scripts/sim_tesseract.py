"""Tesseract simulation: scripted bots play many seeded games on the pure core.

Verdicts (widths, mixes, points) are whole numbers from ``tesseract_core``; the bots use
floats only to choose a move. Runs on a shared 4-CPU machine: never more than two worker
processes (``--workers`` is capped at 2).

    python scripts/sim_tesseract.py --only tourn,same --games 100 --workers 2
    python scripts/sim_tesseract.py --only levers --configs base,sq --games 100

Experiments: tourn skill same degenerate coalition luck sweep levers.
Totals are printed as points // 2 (basis points of movement toward the own target) for the
sum-of-gaps score; for the squared score they are points // 20000 (percentage-point squares).
"""

from __future__ import annotations

import argparse
import hashlib
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import replace
from multiprocessing import Pool

from emporia.modules import tesseract_core as tc

NOMINAL = ((4, 4, 4, 4), (5, 5, 5, 5), (2, 7, 2, 7), (7, 2, 7, 2))


# -- float helpers (choosing only) ---------------------------------------------------------


def fmix(widths, rules, samples=NOMINAL):
    acc = [0.0] * 4
    for s in samples:
        amt = tc.forward(widths, s, None, rules)
        t = sum(amt)
        if t == 0:
            acc = [a + 0.25 for a in acc]
        else:
            acc = [a + x / t for a, x in zip(acc, amt)]
    return [a / len(samples) for a in acc]


def fdist(sh, tfrac, rules):
    if rules.score_power == 2:
        return sum((a - b) ** 2 for a, b in zip(sh, tfrac))
    return sum(abs(a - b) for a, b in zip(sh, tfrac))


def per_channel_cap(rules):
    return rules.unit_cap or rules.push_budget


def plan(widths, rules, obj, max_ch=None, unit=4, samples=NOMINAL, start=None, cap_units=None):
    """Greedy plan maximising obj(share list), ``unit`` at a time. Returns ({channel index:
    push}, widths after)."""
    max_ch = rules.push_channels if max_ch is None else max_ch
    cap_units = per_channel_cap(rules) if cap_units is None else cap_units
    cur = list(widths)
    push = dict(start or {})
    left = rules.push_budget - sum(abs(v) for v in push.values())
    while left > 0:
        step = min(unit, left)
        base = obj(fmix(tuple(cur), rules, samples))
        best, best_n, best_sign = 1e-12, None, 0
        for n in range(len(cur)):
            if n not in push and len(push) >= max_ch:
                continue
            for sign in (1, -1):
                new = max(rules.width_min, min(rules.width_cap, cur[n] + sign * step))
                used = abs(new - cur[n])
                if used == 0 or abs(push.get(n, 0) + sign * used) > cap_units:
                    continue
                old = cur[n]
                cur[n] = new
                g = obj(fmix(tuple(cur), rules, samples)) - base
                cur[n] = old
                if g > best:
                    best, best_n, best_sign = g, n, sign
        if best_n is None:
            break
        new = max(rules.width_min, min(rules.width_cap, cur[best_n] + best_sign * step))
        used = abs(new - cur[best_n])
        cur[best_n] = new
        push[best_n] = push.get(best_n, 0) + best_sign * used
        left -= used
    return {n: v for n, v in push.items() if v}, tuple(cur)


def to_move(push):
    ids = tc.channel_ids()
    return {"push": {ids[n]: v for n, v in push.items() if v}}


def random_push(rng, rules):
    ids = tc.channel_ids()
    chans = rng.sample(range(len(ids)), rules.push_channels)
    left = rules.push_budget
    out = {}
    cap = per_channel_cap(rules)
    for n in chans:
        if left <= 0:
            break
        a = rng.randint(1, min(left, cap))
        out[ids[n]] = a if rng.random() < 0.5 else -a
        left -= a
    return {"push": out}


class View:
    pass


def _obj(v):
    t, r = v.tfrac, v.rules
    return lambda sh: -fdist(sh, t, r)


def _first_order(v, samples):
    """(gain, channel, sign) for +-1 on every channel on ``samples``, best first."""
    r, w = v.rules, v.widths
    d0 = fdist(fmix(w, r, samples), v.tfrac, r)
    g = []
    for n in range(len(w)):
        for sign in (1, -1):
            if not r.width_min <= w[n] + sign <= r.width_cap:
                continue
            ww = list(w)
            ww[n] += sign
            g.append((d0 - fdist(fmix(tuple(ww), r, samples), v.tfrac, r), n, sign))
    g.sort(reverse=True)
    return g


# -- strategies ------------------------------------------------------------------------------


def s_random(v):
    return random_push(v.rng, v.rules)


def s_idle(v):
    return {"push": {}}


def s_lite(v):
    """Read the pull table: the best channel moves by first-order gain toward the own target on
    last epoch's pulses, 4 units each (what a model can do by reading the table)."""
    r = v.rules
    amount = min(4, per_channel_cap(r))
    push = {}
    for gain, n, sign in _first_order(v, v.last_samples or NOMINAL):
        if gain <= 0 or len(push) >= max(1, r.push_budget // amount):
            break
        if n not in push:
            push[n] = sign * amount
    return to_move(push)


def s_lite_thin(v):
    """A weaker reader, for a thin table with no ``best`` list and +1 rows only: looks at the
    one output with the largest gap and takes the two channels whose +/-1 row moves that output
    most the right way, 4 units each. Only a proxy for a model that must do the sums itself."""
    r = v.rules
    samples = v.last_samples or NOMINAL
    cur = fmix(v.widths, r, samples)
    k = max(range(4), key=lambda i: abs(v.tfrac[i] - cur[i]))
    want = 1 if v.tfrac[k] > cur[k] else -1
    cand = []
    for n in range(len(v.widths)):
        for sign in (1, -1):
            if not r.width_min <= v.widths[n] + sign <= r.width_cap:
                continue
            ww = list(v.widths)
            ww[n] += sign
            cand.append((want * (fmix(tuple(ww), r, samples)[k] - cur[k]), n, sign))
    cand.sort(reverse=True)
    amount = min(4, per_channel_cap(r))
    push = {}
    for g, n, sign in cand:
        if g <= 0 or len(push) >= max(1, r.push_budget // amount):
            break
        if n not in push:
            push[n] = sign * amount
    return to_move(push)


def s_greedy(v):
    return to_move(plan(v.widths, v.rules, _obj(v))[0])


def s_greedy1(v):
    """A deeper search: one unit at a time."""
    return to_move(plan(v.widths, v.rules, _obj(v), unit=1)[0])


def _others_drift(v):
    if v.prev_widths is None:
        return None
    idx = tc.channel_index()
    d = [a - b for a, b in zip(v.widths, v.prev_widths)]
    for cid, x in (v.my_last or {"push": {}})["push"].items():
        d[idx[cid]] -= x
    return d


def s_smart(v):
    """Assume the others repeat last epoch's net pushes; plan on top of that network."""
    r = v.rules
    d = _others_drift(v)
    if d is None:
        return s_greedy(v)
    pred = tuple(max(r.width_min, min(r.width_cap, w + x)) for w, x in zip(v.widths, d))
    return to_move(plan(pred, r, _obj(v))[0])


def _moved(v, sign):
    r = v.rules
    if v.prev_widths is None:
        return None
    d = [(abs(a - b), n, sign * (1 if a > b else -1)) for n, (a, b) in enumerate(zip(v.widths, v.prev_widths)) if a != b]
    d.sort(reverse=True)
    amount = min(r.push_budget // 3, per_channel_cap(r))
    return to_move({n: s * amount for _, n, s in d[:3]})


def s_copy_agg(v):
    """Copy the table: push the three channels that moved most last epoch, the same way."""
    return _moved(v, 1) or s_random(v)


def s_spoiler(v):
    """Push back whatever moved most last epoch."""
    return _moved(v, -1) or s_random(v)


def s_copy_player(v):
    """Copy the latest published move of the biggest pusher (owners are public at once, so
    this is the previous epoch's move)."""
    if not v.history:
        return s_random(v)
    others = {p: m for p, m in v.history[-1].items() if p != v.me and m["push"]}
    if not others:
        return s_random(v)
    p = max(sorted(others), key=lambda q: sum(abs(x) for x in others[q]["push"].values()))
    return {"push": dict(others[p]["push"])}


def s_oneshot(v):
    """Everything on the single best channel (as much as the rules allow on one channel)."""
    r = v.rules
    amt = per_channel_cap(r)
    return to_move(plan(v.widths, r, _obj(v), max_ch=1, unit=amt)[0])


STRATS = {
    "random": s_random, "idle": s_idle, "lite": s_lite, "lite_thin": s_lite_thin,
    "greedy": s_greedy, "greedy1": s_greedy1, "smart": s_smart, "copy_agg": s_copy_agg,
    "copy_player": s_copy_player, "spoiler": s_spoiler, "oneshot": s_oneshot,
}


# -- teams: one brain, two seats, both targets known ---------------------------------------------


def team_moves(kind, va, vb):
    r = va.rules
    w0, ta, tb = va.widths, va.tfrac, vb.tfrac
    if kind == "pair_talk":  # B plans on top of A's plan
        pa, wa = plan(w0, r, lambda sh: -fdist(sh, ta, r))
        pb, _ = plan(wa, r, lambda sh: -fdist(sh, tb, r))
        return to_move(pa), to_move(pb)
    if kind == "pair_joint":  # greedy on the sum of both players' scores
        pa, pb = {}, {}

        def wplus(*pushes):
            w = list(w0)
            for p in pushes:
                for n, x in p.items():
                    w[n] += x
            return tuple(max(r.width_min, min(r.width_cap, x)) for x in w)

        base = fmix(w0, r)

        def total(pa, pb):
            full = fmix(wplus(pa, pb), r)
            la = fdist(fmix(wplus(pb), r), ta, r) - fdist(full, ta, r)
            lb = fdist(fmix(wplus(pa), r), tb, r) - fdist(full, tb, r)
            fa = fdist(base, ta, r) - fdist(fmix(wplus(pa), r), ta, r)
            fb = fdist(base, tb, r) - fdist(fmix(wplus(pb), r), tb, r)
            return la + lb + fa + fb

        left = {0: r.push_budget, 1: r.push_budget}
        cur = total(pa, pb)
        cap = per_channel_cap(r)
        while left[0] > 0 or left[1] > 0:
            best = (1e-12, None)
            for who in (0, 1):
                if left[who] <= 0:
                    continue
                mine = pa if who == 0 else pb
                step = min(4, left[who])
                for n in range(len(w0)):
                    if n not in mine and len(mine) >= r.push_channels:
                        continue
                    for sign in (1, -1):
                        if abs(mine.get(n, 0) + sign * step) > cap:
                            continue
                        trial = dict(mine)
                        trial[n] = trial.get(n, 0) + sign * step
                        val = total(trial, pb) if who == 0 else total(pa, trial)
                        if val - cur > best[0]:
                            best = (val - cur, (who, n, sign, step))
            if best[1] is None:
                break
            who, n, sign, step = best[1]
            mine = pa if who == 0 else pb
            mine[n] = mine.get(n, 0) + sign * step
            left[who] -= step
            cur += best[0]
        return to_move(pa), to_move(pb)
    if kind == "pair_wrestle":  # one channel, opposite ways, where their wants differ most
        base = fmix(w0, r)
        amt = min(r.push_budget, per_channel_cap(r))
        best = None
        for n in range(len(w0)):
            if w0[n] + amt > r.width_cap or w0[n] - amt < r.width_min:
                continue
            up, dn = list(w0), list(w0)
            up[n] += amt
            dn[n] -= amt
            mu, md = fmix(tuple(up), r), fmix(tuple(dn), r)
            v1 = (fdist(md, ta, r) - fdist(base, ta, r)) + (fdist(mu, tb, r) - fdist(base, tb, r))
            v2 = (fdist(mu, ta, r) - fdist(base, ta, r)) + (fdist(md, tb, r) - fdist(base, tb, r))
            for val, sa in ((v1, 1), (v2, -1)):
                if best is None or val > best[0]:
                    best = (val, n, sa)
        if best is None or best[0] <= 0:
            return team_moves("pair_talk", va, vb)
        _, n, sa = best
        return to_move({n: amt * sa}), to_move({n: -amt * sa})
    if kind == "pair_feed":  # both push for A's target only
        pa, wa = plan(w0, r, lambda sh: -fdist(sh, ta, r))
        pb, _ = plan(wa, r, lambda sh: -fdist(sh, ta, r))
        return to_move(pa), to_move(pb)
    raise KeyError(kind)


# -- game loop --------------------------------------------------------------------------------------


def seed_int(*parts):
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def epoch_seed(seed0: bytes, e: int) -> bytes:
    return hashlib.sha256(seed0 + b"|epoch|" + str(e).encode()).digest()


def run_game(seed, rules, seats, epochs=5, verify=False):
    """One game. Returns (totals {player: int}, record). Targets are dealt per seat by
    ``deal_target`` from a hash of the game seed and the seat, as the real game will."""
    sb = str(seed).encode()
    n = len(seats)
    widths, _ = tc.open_network(sb, rules)
    names = [f"p{i}" for i in range(n)]
    tgt = {p: tc.deal_target(hashlib.sha256(sb + b"|target|" + p.encode()).digest(), rules) for p in names}
    totals = {p: 0 for p in names}
    rngs = {p: random.Random(seed_int(seed, p)) for p in names}
    prev_widths, last_samples = None, None
    my_last = {p: None for p in names}
    history, per_epoch, all_moves = [], [], []
    for e in range(1, epochs + 1):
        moves, teams = {}, defaultdict(list)
        for p, s in zip(names, seats):
            v = View()
            v.rules, v.n, v.me, v.epoch, v.widths = rules, n, p, e, widths
            v.prev_widths, v.last_samples, v.my_last = prev_widths, last_samples, my_last[p]
            v.tfrac = [x / 100 for x in tgt[p]]
            v.rng = rngs[p]
            v.history = history  # who pushed what, published at once after each epoch
            if s.startswith("team:"):
                teams[s].append(v)
                continue
            moves[p] = STRATS[s](v)
        for s, vs in teams.items():
            ma, mb = team_moves(s.split(":")[1], vs[0], vs[1])
            moves[vs[0].me], moves[vs[1].me] = ma, mb
        moves = {p: tc.validate_move(moves[p], rules) for p in names}
        es = epoch_seed(sb, e)
        pub = tc.play_epoch(es, e, widths, moves, rules, with_pull=False)
        pts = tc.epoch_points(widths, moves, tgt, pub.pulses, pub.dark, rules)
        for p, d in pts.items():
            totals[p] += d["points"]
        per_epoch.append({"mix": pub.mix, "scores": {p: d["points"] for p, d in pts.items()}})
        all_moves.append(moves)
        history.append(moves)
        my_last = dict(moves)
        prev_widths, widths, last_samples = widths, pub.widths_after, pub.pulses
    if verify:
        _, _, replay = tc.play_game(sb, [epoch_seed(sb, e) for e in range(1, epochs + 1)], all_moves, tgt, rules)
        assert replay == totals, "replay differs from the live game"
    fd = {p: tc.distance(per_epoch[-1]["mix"], tgt[p], replace(rules, score_power=1)) for p in names}
    return totals, {"targets": tgt, "epochs": per_epoch, "final_dist": fd}


def _job(args):
    seed, rules, seats, epochs = args
    return run_game(seed, rules, seats, epochs, verify=seed_int(seed) % 25 == 0)


_POOL = None


def pmap(jobs, workers):
    global _POOL
    workers = min(max(workers, 1), 2)  # machine rule: at most two simulation processes
    if workers <= 1:
        return [_job(j) for j in jobs]
    if _POOL is None:
        _POOL = Pool(workers)
    return _POOL.map(_job, jobs, chunksize=2)


# -- statistics ----------------------------------------------------------------------------------------


def scale_of(rules):
    return 20000 if rules.score_power == 2 else 2


def ci(xs):
    return 1.96 * statistics.stdev(xs) / len(xs) ** 0.5 if len(xs) > 1 else 0.0


def win_shares(totals):
    top = max(totals.values())
    w = [p for p, t in totals.items() if t == top]
    return {p: (1 / len(w) if p in w else 0.0) for p in totals}


def tournament(tag, rules, composition, games, epochs, workers, shuffle=True):
    jobs, perms = [], []
    for g in range(games):
        seats = list(composition)
        if shuffle:
            random.Random(seed_int(tag, g)).shuffle(seats)
        jobs.append((f"{tag}-{g}", rules, seats, epochs))
        perms.append(seats)
    out = pmap(jobs, workers)
    sc = scale_of(rules)
    per = defaultdict(lambda: {"score": [], "win": []})
    allscores, ties = [], 0
    for seats, (tot, _) in zip(perms, out):
        ws = win_shares(tot)
        if sum(1 for x in ws.values() if x > 0) > 1:
            ties += 1
        for p, s in zip(tot, seats):
            per[s]["score"].append(tot[p] / sc)
            per[s]["win"].append(ws[p])
            allscores.append(tot[p] / sc)
    rows = {}
    for s, d in per.items():
        k = composition.count(s)
        rows[s] = {"mean": statistics.mean(d["score"]), "ci": ci(d["score"]), "sd": statistics.pstdev(d["score"]),
                   "win": statistics.mean(d["win"]) * k}
    gm = statistics.mean(allscores)
    ssb = sum(len(d["score"]) * (statistics.mean(d["score"]) - gm) ** 2 for d in per.values())
    sst = sum((x - gm) ** 2 for x in allscores)
    return rows, (ssb / sst if sst else 0.0), out, ties, perms


def show(title, rows, eta=None, ties=None):
    print(f"\n### {title}")
    print("| strategy | mean total | +-95% | sd | win share (all its seats) |")
    print("|---|---|---|---|---|")
    for s, r in sorted(rows.items(), key=lambda kv: -kv[1]["mean"]):
        print(f"| {s} | {r['mean']:.0f} | {r['ci']:.0f} | {r['sd']:.0f} | {r['win']:.3f} |")
    if eta is not None:
        print(f"strategy explains {eta * 100:.1f}% of the variance in a player's total; games tied for first: {ties}")
    sys.stdout.flush()


BASE = tc.DEFAULT
MIX = {
    3: ["random", "lite", "smart"],
    4: ["random", "lite", "greedy", "smart"],
    5: ["random", "lite", "greedy", "smart", "copy_agg"],
    6: ["random", "lite", "greedy", "smart", "copy_agg", "spoiler"],
}


def exp_tourn(a):
    for n in [int(x) for x in a.ns.split(",")]:
        rows, eta, _, ties, _ = tournament(f"tourn{n}", BASE, MIX[n], a.games, a.epochs, a.workers)
        show(f"Mixed table, {n} players, {a.epochs} epochs, {a.games} games", rows, eta, ties)


def exp_skill(a):
    """No deliberately bad bots, two seats each where the table allows."""
    for n, comp in ((3, ["random", "lite", "greedy"]), (6, ["random", "random", "lite", "lite", "greedy", "greedy"]),
                    (4, ["lite", "lite", "greedy", "greedy"]), (5, ["idle", "random", "lite", "greedy", "smart"])):
        rows, eta, _, ties, _ = tournament(f"skill{n}", BASE, comp, a.games, a.epochs, a.workers)
        show(f"Skill table {comp}", rows, eta, ties)


def exp_same(a):
    """Identical bots: ties, seat fairness, how the mean depends on table size, table index."""
    print("\n### Identical bots: ties, seats, table size")
    sc = scale_of(BASE)
    for strat in ("smart", "greedy", "random"):
        for n in (3, 4, 5, 6):
            g = a.games if strat != "random" else a.games * 3
            out = pmap([(f"same-{strat}-{n}-{i}", BASE, [strat] * n, a.epochs) for i in range(g)], a.workers)
            means = [statistics.mean(t[f"p{i}"] for t, _ in out) / sc for i in range(n)]
            wins = [statistics.mean(win_shares(t)[f"p{i}"] for t, _ in out) for i in range(n)]
            ties = sum(1 for t, _ in out if sum(1 for x in win_shares(t).values() if x > 0) > 1)
            same_t = sum(1 for _, h in out if len(set(h["targets"].values())) < n)
            allv = [t[f"p{i}"] / sc for t, _ in out for i in range(n)]
            chi = sum((w - 1 / n) ** 2 / (1 / n) for w in wins) * g
            idx = [x for t, _ in out for x in tc.table_index(t).values()]
            print(
                f"{strat:6} n={n}: mean total {statistics.mean(allv):.0f} (sd {statistics.pstdev(allv):.0f}); by seat "
                f"{[round(m) for m in means]}; win by seat {[round(w, 3) for w in wins]}; chi2={chi:.1f} on {n - 1} dof; "
                f"tied games {ties}/{g}; repeated target {same_t}; table index mean {statistics.mean(idx):.0f} "
                f"sd {statistics.pstdev(idx):.0f}"
            )
            sys.stdout.flush()


ODD = ("idle", "random", "copy_agg", "copy_player", "spoiler", "lite", "lite_thin", "greedy", "smart", "oneshot", "greedy1")


def degenerate_cells(rules, base, strats, games, epochs, workers, n=5, tag="deg"):
    """One odd bot among n-1 ``base`` bots. Same game seeds in every cell (paired), odd bot in
    the last seat. Returns {strat: dict}."""
    res = {}
    for strat in strats:
        if strat == base:
            continue
        comp = [base] * (n - 1) + [strat]
        out = pmap([(f"{tag}-{i}", rules, comp, epochs) for i in range(games)], workers)
        sc = scale_of(rules)
        odd = [t[f"p{n - 1}"] / sc for t, _ in out]
        mine = [statistics.mean(t[f"p{i}"] for i in range(n - 1)) / sc for t, _ in out]
        win_odd = statistics.mean(win_shares(t)[f"p{n - 1}"] for t, _ in out)
        diff = [m - o for m, o in zip(mine, odd)]
        res[strat] = {"odd": statistics.mean(odd), "odd_ci": ci(odd), "base": statistics.mean(mine), "base_ci": ci(mine),
                      "win": win_odd, "diff": statistics.mean(diff), "diff_ci": ci(diff)}
    return res


def exp_degenerate(a):
    print(f"\n### One odd player among {a.solo} players (5 seats, same seeds in every cell)")
    r = degenerate_cells(BASE, a.solo, ODD, a.games, a.epochs, a.workers)
    for s, d in r.items():
        print(f"- one {s} among 4 {a.solo}: {s} {d['odd']:.0f} +-{d['odd_ci']:.0f} (win {d['win']:.3f}) vs {a.solo} "
              f"{d['base']:.0f} +-{d['base_ci']:.0f}")
    sys.stdout.flush()


def coalition_rows(rules, solo, n, games, epochs, workers, kinds):
    base = pmap([(f"co-{n}-{i}", rules, [solo] * n, epochs) for i in range(games)], workers)
    bp = [t["p0"] + t["p1"] for t, _ in base]
    bw = statistics.mean(win_shares(t)["p0"] + win_shares(t)["p1"] for t, _ in base)
    rows = {"solo": {"pair": statistics.mean(bp), "ratio": 1.0, "diff": 0.0, "diff_ci": 0.0, "win": bw, "others": 0.0}}
    for kind in kinds:
        comp = [f"team:{kind}", f"team:{kind}"] + [solo] * (n - 2)
        out = pmap([(f"co-{n}-{i}", rules, comp, epochs) for i in range(games)], workers)
        pair = [t["p0"] + t["p1"] for t, _ in out]
        diff = [x - y for x, y in zip(pair, bp)]
        win = statistics.mean(win_shares(t)["p0"] + win_shares(t)["p1"] for t, _ in out)
        others = statistics.mean(statistics.mean(t[f"p{i}"] for i in range(2, n)) for t, _ in out)
        rows[kind] = {"pair": statistics.mean(pair), "ratio": statistics.mean(pair) / statistics.mean(bp),
                      "diff": statistics.mean(diff), "diff_ci": ci(diff), "win": win, "others": others}
    return rows


def exp_coalition(a):
    sc = scale_of(BASE)
    print("\n### Pairs (two seats, one brain, both targets known) against the same two seats solo")
    for solo in a.solo.split(","):
        for n in [int(x) for x in a.ns.split(",")]:
            rows = coalition_rows(BASE, solo, n, a.games, a.epochs, a.workers,
                                  ("pair_talk", "pair_joint", "pair_wrestle", "pair_feed"))
            print(f"n={n}, others {solo}: two solo seats total {rows['solo']['pair'] / sc:.0f}, win (either) {rows['solo']['win']:.3f}")
            for k, d in rows.items():
                if k == "solo":
                    continue
                print(f"  {k:12}: pair total {d['pair'] / sc:.0f} ({d['ratio']:.2f}x, paired diff {d['diff'] / sc:+.0f} "
                      f"+-{d['diff_ci'] / sc:.0f}), win (either) {d['win']:.3f}; other seats avg {d['others'] / sc:.0f}")
                sys.stdout.flush()


def exp_luck(a):
    print("\n### Luck of the deal (all greedy, 5 seats)")
    out = pmap([(f"luck-{i}", BASE, ["greedy"] * 5, a.epochs) for i in range(a.games)], a.workers)
    sc = scale_of(BASE)
    allv = [t[p] / sc for t, _ in out for p in t]
    game_means = [statistics.mean(t.values()) / sc for t, _ in out]
    within = [(t[p] - statistics.mean(t.values())) / sc for t, _ in out for p in t]
    print(f"mean {statistics.mean(allv):.0f}, sd {statistics.pstdev(allv):.0f}; sd of table means "
          f"{statistics.pstdev(game_means):.0f}; sd within a table {statistics.pstdev(within):.0f}")
    fd = [h["final_dist"][p] for _, h in out for p in h["final_dist"]]
    print(f"final distance to own target (sum of gaps, bp) mean {statistics.mean(fd):.0f} (start about 4000)")


# -- sweeps and the lever battery -----------------------------------------------------------------------------


LEVERS = {
    "base": {},
    "sq": {"score_power": 2},
    "cap4": {"unit_cap": 4},
    "w12": {"width_cap": 12, "open_hi": 12},
    "sq+cap4": {"score_power": 2, "unit_cap": 4},
    "sq+w12": {"score_power": 2, "width_cap": 12, "open_hi": 12},
    "cap4+w12": {"unit_cap": 4, "width_cap": 12, "open_hi": 12},
    "sq+cap4+w12": {"score_power": 2, "unit_cap": 4, "width_cap": 12, "open_hi": 12},
}

SWEEPS = {
    "dark": [(f"dark 1/{d}" if d else "no dark", {"dark_den": d}) for d in (0, 16, 24, 32)],
    "budget": [("budget 6 over 3", {"push_budget": 6, "push_channels": 3}), ("budget 8 over 4", {}),
               ("budget 12 over 6", {"push_budget": 12, "push_channels": 6})],
    "epochs": [("3 epochs", {"epochs": 3}), ("5 epochs", {}), ("6 epochs", {"epochs": 6})],
    "score": [("score first_last", {}), ("score last", {"score_mode": "last"}), ("score min", {"score_mode": "min"})],
    "levers": [(k, v) for k, v in LEVERS.items()],
}


def exp_sweep(a):
    print(f"\n### Sweep '{a.sweep}': skill share (eta^2) and mean totals, mixed tables")
    for label, over in SWEEPS[a.sweep]:
        rules = replace(BASE, **over)
        line = []
        for n in [int(x) for x in a.ns.split(",")]:
            rows, eta, _, ties, _ = tournament(f"sw{n}", rules, MIX[n], a.games, rules.epochs, a.workers)
            line.append(f"{n} seats {eta * 100:.1f}% (best {max(r['mean'] for r in rows.values()):.0f}, ties {ties})")
        print(f"- {label}: " + "; ".join(line))
        sys.stdout.flush()


def battery(label, rules, games, workers, ns=(3, 4, 5, 6), pair_kinds=("pair_talk", "pair_joint"), strong=False):
    """Everything the lever question needs, for one rule set. Prints one block, returns a dict."""
    ep = rules.epochs
    sc = scale_of(rules)
    res = {"label": label}
    print(f"\n#### {label}  ({rules.score_power=}, {rules.unit_cap=}, {rules.width_cap=})")
    eta = {}
    for n in ns:
        _, e, _, ties, _ = tournament(f"lv-t{n}", rules, MIX[n], games, ep, workers)
        eta[n] = (e, ties)
    res["eta"] = eta
    print("skill share: " + " / ".join(f"{n} seats {eta[n][0] * 100:.1f}%" for n in ns)
          + f"  (ties {sum(t for _, t in eta.values())})")
    odd = ["oneshot", "lite", "lite_thin", "idle", "spoiler", "random", "copy_player", "copy_agg"] + (["greedy1"] if strong else [])
    cells = degenerate_cells(rules, "greedy", odd, games, ep, workers, tag="lv-d")
    res["cells"] = cells
    g_idle = cells["idle"]["base"]
    for s in odd:
        d = cells[s]
        rel = 100 * d["diff"] / abs(d["base"]) if d["base"] else 0
        print(f"  one {s:11} among 4 greedy: {s} {d['odd']:.0f} +-{d['odd_ci']:.0f} win {d['win']:.2f}; greedy beside "
              f"{d['base']:.0f}; greedy minus {s} {d['diff']:+.0f} +-{d['diff_ci']:.0f} ({rel:+.1f}% of greedy)")
    res["spoiler_cost_pct"] = 100 * (g_idle - cells["spoiler"]["base"]) / abs(g_idle) if g_idle else 0
    print(f"spoiler cost to the 4 planners: {res['spoiler_cost_pct']:.1f}% "
          f"({g_idle:.0f} beside idle -> {cells['spoiler']['base']:.0f} beside spoiler)")
    pr = coalition_rows(rules, "greedy", 5, games, ep, workers, pair_kinds)
    res["pairs"] = pr
    for k in pair_kinds:
        d = pr[k]
        print(f"pair {k} at 5 seats: {d['ratio']:.2f}x two solo seats (paired diff {d['diff'] / sc:+.0f} +-{d['diff_ci'] / sc:.0f}), "
              f"win either {d['win']:.2f} vs solo {pr['solo']['win']:.2f}")
    chis = {}
    for n in (4, 5):
        out = pmap([(f"lv-r{n}-{i}", rules, ["random"] * n, ep) for i in range(games * 3)], workers)
        wins = [statistics.mean(win_shares(t)[f"p{i}"] for t, _ in out) for i in range(n)]
        chis[n] = sum((w - 1 / n) ** 2 / (1 / n) for w in wins) * len(out)
    res["chi"] = chis
    print("seat fairness (all random): " + ", ".join(f"{n} seats chi2 {c:.1f} on {n - 1} dof" for n, c in chis.items()))
    sys.stdout.flush()
    return res


def exp_levers(a):
    names = a.configs.split(",") if a.configs else list(LEVERS)
    print(f"\n### Depth levers, {a.games} games per cell")
    for name in names:
        battery(name, replace(BASE, **LEVERS[name]), a.games, a.workers, tuple(int(x) for x in a.ns.split(",")),
                strong=a.strong)


EXPS = {"tourn": exp_tourn, "skill": exp_skill, "same": exp_same, "degenerate": exp_degenerate,
        "coalition": exp_coalition, "luck": exp_luck, "sweep": exp_sweep, "levers": exp_levers}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--workers", type=int, default=2, help="capped at 2")
    ap.add_argument("--dark-den", type=int, default=24)
    ap.add_argument("--budget", type=int, default=8)
    ap.add_argument("--channels", type=int, default=4)
    ap.add_argument("--score", default="first_last", choices=("first_last", "last", "min"))
    ap.add_argument("--score-power", type=int, default=1, choices=(1, 2))
    ap.add_argument("--unit-cap", type=int, default=0, help="most units on one channel (0: no limit)")
    ap.add_argument("--width-cap", type=int, default=20)
    ap.add_argument("--solo", default="greedy")
    ap.add_argument("--ns", default="3,4,5,6")
    ap.add_argument("--only", default="tourn")
    ap.add_argument("--sweep", default="dark", choices=sorted(SWEEPS))
    ap.add_argument("--configs", default="", help="levers: comma list of " + ",".join(LEVERS))
    ap.add_argument("--strong", action="store_true", help="levers: also a one-unit-at-a-time searcher")
    a = ap.parse_args()
    global BASE
    BASE = tc.Rules(dark_den=a.dark_den, push_budget=a.budget, push_channels=a.channels, epochs=a.epochs,
                    score_mode=a.score, score_power=a.score_power, unit_cap=a.unit_cap, width_cap=a.width_cap,
                    open_hi=a.width_cap)
    print(f"<!-- {BASE} -->")
    for name in a.only.split(","):
        t0 = time.time()
        EXPS[name](a)
        print(f"[{name}: {time.time() - t0:.0f}s]", file=sys.stderr)


if __name__ == "__main__":
    main()
