#!/usr/bin/env python
"""Estuary prototype simulation (planning aid; indicative only).

Plays seeded games of the pure rules in ``emporia.modules.estuary_core`` with scripted bots and
prints the numbers the E0 report needs. Verdicts come from the core (exact Fractions); the bots
use floats only to *choose* moves. No relay, no network, no model.

    python scripts/sim_estuary.py --games 200 [--only tourn,seats,...] [--workers 4]
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import random
import statistics
import sys
import time
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from emporia.modules import estuary_core as ec  # noqa: E402

NOMINAL = ((4, 4, 4, 4), (5, 5, 5, 5), (2, 7, 2, 7), (7, 2, 7, 2))


# -- float helpers for choosing moves (never a verdict) ----------------------------------


def fshare(widths, rules, samples=NOMINAL):
    m = rules.mouths
    acc = [0.0] * m
    for s in samples:
        amt = ec.forward(widths, s, None, rules)
        t = sum(amt)
        if t == 0:
            acc = [a + 1 / m for a in acc]
        else:
            acc = [a + x / t for a, x in zip(acc, amt)]
    return [a / len(samples) for a in acc]


def plan(widths, rules, obj, budget=None, max_ch=None, unit=4):
    """Greedy gradient plan: returns ({channel_index: push}, widths_after_plan)."""
    budget = rules.push_budget if budget is None else budget
    max_ch = rules.push_channels if max_ch is None else max_ch
    cap = rules.width_cap
    cur = list(widths)
    push: dict[int, int] = {}
    left = budget
    while left > 0:
        step = min(unit, left)
        base = obj(fshare(tuple(cur), rules))
        best, best_n, best_sign = 0.0, None, 0
        for n in range(len(cur)):
            if n not in push and len(push) >= max_ch:
                continue
            if cur[n] >= cap:
                continue
            cur[n] += 1
            g = obj(fshare(tuple(cur), rules)) - base
            cur[n] -= 1
            if abs(g) > best:
                best, best_n, best_sign = abs(g), n, 1 if g > 0 else -1
        if best_n is None:
            break
        amt = step
        new = max(-cap, min(cap, cur[best_n] + best_sign * amt))
        used = abs(new - cur[best_n])
        if used == 0:
            break
        cur[best_n] = new
        push[best_n] = push.get(best_n, 0) + best_sign * used
        left -= used
    return push, tuple(cur)


def to_move(back, push, rules):
    ids = ec.channel_ids(rules.sizes())
    return {"back": back, "push": {ids[n]: v for n, v in push.items() if v}}


def random_push(rng, rules):
    ids = ec.channel_ids(rules.sizes())
    chans = rng.sample(range(len(ids)), rules.push_channels)
    left = rules.push_budget
    out = {}
    for n in chans:
        if left <= 0:
            break
        a = rng.randint(1, left)
        out[ids[n]] = a if rng.random() < 0.5 else -a
        left -= a
    return out


class View:
    def __init__(self, rules, n, me, epoch, widths, last, others_last_backs, rng):
        self.rules, self.n, self.me, self.epoch = rules, n, me, epoch
        self.widths, self.last, self.rng = widths, last, rng
        self.crowd = others_last_backs  # {mouth(1-based): count of OTHER players' backs last epoch}

    def expected_backers(self, t):  # t 0-based; includes me
        if self.last is None:
            return 1 + (self.n - 1) / self.rules.mouths
        return 1 + self.crowd.get(t + 1, 0)


def smart_choice(v, planned=True):
    best = None
    for t in range(v.rules.mouths):
        if planned:
            p, w = plan(v.widths, v.rules, lambda s, t=t: s[t])
        else:
            p, w = {}, v.widths
        val = fshare(w, v.rules)[t] / v.expected_backers(t) ** v.rules.crowd_pow
        if best is None or val > best[0]:
            best = (val, t, p)
    return best[1], best[2]


# -- strategies (view -> move) -----------------------------------------------------------


def s_random(v):
    return {"back": v.rng.randint(1, v.rules.mouths), "push": random_push(v.rng, v.rules)}


def s_lite(v):
    """What a model reading the published pull table does: back the biggest mouth, push the
    three strongest channels one step each way. No planning, no crowd model."""
    r = v.rules
    if v.last is not None:
        samples, w = v.last.samples, v.last.widths_after
    else:
        samples, w = NOMINAL, v.widths
    sh = fshare(w, r, samples)
    t = max(range(r.mouths), key=lambda i: sh[i])
    base = sh[t]
    g = []
    for n in range(len(w)):
        if w[n] >= r.width_cap:
            continue
        ww = list(w)
        ww[n] += 1
        g.append(
            (
                abs(fshare(tuple(ww), r, samples)[t] - base),
                n,
                1 if fshare(tuple(ww), r, samples)[t] > base else -1,
            )
        )
    g.sort(reverse=True)
    push = {n: sgn * (r.push_budget // 3) for _, n, sgn in g[:3]}
    return to_move(t + 1, push, r)


def s_greedy(v):
    r = v.rules
    t = max(range(r.mouths), key=lambda i: fshare(v.widths, r)[i])
    p, _ = plan(v.widths, r, lambda s: s[t])
    return to_move(t + 1, p, r)


def s_smart(v):
    t, p = smart_choice(v)
    return to_move(t + 1, p, v.rules)


def s_contrarian(v):
    r = v.rules
    if v.last is None:
        t = max(range(r.mouths), key=lambda i: fshare(v.widths, r)[i])
    else:
        sh = fshare(v.widths, r)
        t = min(range(r.mouths), key=lambda i: (v.crowd.get(i + 1, 0), -sh[i]))
    p, _ = plan(v.widths, r, lambda s: s[t])
    return to_move(t + 1, p, r)


def s_copycat(v):
    if v.last is None:
        return s_random(v)
    leader = max(
        (p for p in v.last.scores if p != v.me), key=lambda p: v.last.scores[p], default=None
    )
    if leader is None:
        return s_random(v)
    return v.last.moves[leader]


def s_freerider(v):
    t, _ = smart_choice(v, planned=False)
    return {"back": t + 1, "push": {}}


def s_spoiler(v):
    r = v.rules
    half = r.push_budget // 2
    t, _ = smart_choice(v)
    p, w = plan(v.widths, r, lambda s: s[t], budget=half, max_ch=r.push_channels // 2)
    if v.last is None:
        rival = max((i for i in range(r.mouths) if i != t), key=lambda i: fshare(w, r)[i])
    else:
        rival = max(
            (i for i in range(r.mouths) if i != t),
            key=lambda i: (v.crowd.get(i + 1, 0), fshare(w, r)[i]),
        )
    p2, _ = plan(
        w, r, lambda s: -s[rival], budget=r.push_budget - half, max_ch=r.push_channels - len(p)
    )
    for n, x in p2.items():
        p[n] = p.get(n, 0) + x
    return to_move(t + 1, p, r)


def s_backonly_smart(v):  # crowd-aware backing, random pushes
    t, _ = smart_choice(v, planned=False)
    return {"back": t + 1, "push": random_push(v.rng, v.rules)}


def s_pushonly_smart(v):  # random backing, planned pushes
    r = v.rules
    t = v.rng.randrange(r.mouths)
    p, _ = plan(v.widths, r, lambda s: s[t])
    return to_move(t + 1, p, r)


STRATS = {
    "random": s_random,
    "lite": s_lite,
    "greedy": s_greedy,
    "smart": s_smart,
    "contrarian": s_contrarian,
    "copycat": s_copycat,
    "freerider": s_freerider,
    "spoiler": s_spoiler,
    "backonly": s_backonly_smart,
    "pushonly": s_pushonly_smart,
}


# -- teams: one controller, two seats -------------------------------------------------------


def team_moves(kind, va, vb):
    r = va.rules
    if kind == "pair_same":
        t, pa = smart_choice(va)
        _, w = plan(va.widths, r, lambda s: s[t], budget=0)
        pa, wa = plan(va.widths, r, lambda s: s[t])
        pb, _ = plan(wa, r, lambda s: s[t])
        return to_move(t + 1, pa, r), to_move(t + 1, pb, r)
    if kind == "pair_feeder":  # B backs the emptiest other mouth, both push for A's mouth
        t, _ = smart_choice(va)
        pa, wa = plan(va.widths, r, lambda s: s[t])
        pb, _ = plan(wa, r, lambda s: s[t])
        other = min((i for i in range(r.mouths) if i != t), key=lambda i: va.expected_backers(i))
        return to_move(t + 1, pa, r), to_move(other + 1, pb, r)
    if kind == "pair_two":  # hold two mouths, push 24 for their combined share
        best = None
        for t1 in range(r.mouths):
            for t2 in range(t1 + 1, r.mouths):
                p1, w1 = plan(va.widths, r, lambda s, a=t1, b=t2: s[a] + s[b], budget=r.push_budget)
                sh = fshare(w1, r)
                val = (
                    sh[t1] / va.expected_backers(t1) ** r.crowd_pow
                    + sh[t2] / va.expected_backers(t2) ** r.crowd_pow
                )
                if best is None or val > best[0]:
                    best = (val, t1, t2)
        _, t1, t2 = best
        pa, wa = plan(va.widths, r, lambda s: s[t1] + s[t2])
        pb, _ = plan(wa, r, lambda s: s[t1] + s[t2])
        return to_move(t1 + 1, pa, r), to_move(t2 + 1, pb, r)
    raise KeyError(kind)


# -- game loop -------------------------------------------------------------------------------


def seed_int(*parts):
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


class _Res:
    """EpochResult plus the revealed moves, for strategies that read the public record."""


def run_game(seed, rules, seats, epochs=3):
    """seats: list of strategy names, or ('team:KIND', team_id) for team seats.
    Returns totals {seat: Fraction}, history list of dicts."""
    sb = str(seed).encode()
    n = len(seats)
    widths = ec.open_widths(sb, rules)
    names = [f"p{i}" for i in range(n)]
    totals = {p: ec.Fraction(0) for p in names}
    last = None
    hist = []
    rngs = {p: random.Random(seed_int(seed, p)) for p in names}
    for e in range(1, epochs + 1):
        moves = {}
        teams = defaultdict(list)
        for p, s in zip(names, seats):
            others = defaultdict(int)
            if last is not None:
                for q, m in last.backs.items():
                    if q != p:
                        others[m] += 1
            v = View(rules, n, p, e, widths, last, others, rngs[p])
            if s.startswith("team:"):
                teams[s].append(v)
                continue
            moves[p] = STRATS[s](v)
        for s, vs in teams.items():
            ma, mb = team_moves(s.split(":")[1].split("#")[0], vs[0], vs[1])
            moves[vs[0].me], moves[vs[1].me] = ma, mb
        moves = {p: ec.validate_move(m, rules) for p, m in moves.items()}
        res = ec.play_epoch(sb, e, widths, moves, rules, with_credit=False, with_pull=False)
        res.moves = moves
        for p, s in res.scores.items():
            totals[p] += s
        hist.append({"backs": dict(res.backs), "avg": [float(x) for x in res.avg]})
        widths, last = res.widths_after, res
    return totals, hist


def _job(args):
    seed, rules, seats, epochs = args
    t, h = run_game(seed, rules, seats, epochs)
    return {p: float(x) for p, x in t.items()}, h


def pmap(jobs, workers):
    if workers <= 1:
        return [_job(j) for j in jobs]
    with Pool(workers) as pool:
        return pool.map(_job, jobs, chunksize=2)


# -- statistics -------------------------------------------------------------------------------


def ci(xs):
    if len(xs) < 2:
        return 0.0
    return 1.96 * statistics.stdev(xs) / len(xs) ** 0.5


def win_shares(totals):
    top = max(totals.values())
    w = [p for p, t in totals.items() if abs(t - top) < 1e-12]
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
    per = defaultdict(lambda: {"score": [], "win": []})
    allscores, labels = [], []
    for seats, (tot, hist) in zip(perms, out):
        ws = win_shares(tot)
        for p, s in zip(tot, seats):
            per[s]["score"].append(tot[p])
            per[s]["win"].append(ws[p])
            allscores.append(tot[p])
            labels.append(s)
    n = len(composition)
    rows = {}
    for s, d in per.items():
        rows[s] = {
            "mean": statistics.mean(d["score"]),
            "ci": ci(d["score"]),
            "win": statistics.mean(d["win"]) * 1.0,
            "lift": statistics.mean(d["win"]) / (1 / n),
        }
    gm = statistics.mean(allscores)
    ssb = sum(len(d["score"]) * (statistics.mean(d["score"]) - gm) ** 2 for d in per.values())
    sst = sum((x - gm) ** 2 for x in allscores)
    return rows, (ssb / sst if sst else 0.0), out


def show(title, rows, eta=None):
    print(f"\n### {title}")
    print("| strategy | mean total | +-95% | win share | lift vs fair |")
    print("|---|---|---|---|---|")
    for s, r in sorted(rows.items(), key=lambda kv: -kv[1]["mean"]):
        print(f"| {s} | {r['mean']:.3f} | {r['ci']:.3f} | {r['win']:.3f} | {r['lift']:.2f}x |")
    if eta is not None:
        print(f"strategy explains {eta * 100:.1f}% of the variance in a player's total")


def hhi(hist_list, mouths):
    vals = []
    for hist in hist_list:
        for h in hist:
            c = defaultdict(int)
            for m in h["backs"].values():
                c[m] += 1
            n = sum(c.values())
            vals.append(sum((x / n) ** 2 for x in c.values()))
    return statistics.mean(vals)


# -- experiments ---------------------------------------------------------------------------------

BASE = ec.DEFAULT
MIX5 = ["random", "lite", "greedy", "smart", "contrarian"]
MIX8 = ["random", "lite", "greedy", "smart", "contrarian", "copycat", "freerider", "spoiler"]


def compose(base, n):
    return [base[i % len(base)] for i in range(n)]


def exp_tourn(a):
    R = BASE
    for n, base in (
        (3, ["random", "lite", "smart"]),
        (5, MIX5),
        (8, MIX8),
        (12, MIX8 + ["random", "smart", "lite", "greedy"]),
    ):
        rows, eta, _ = tournament(
            f"tourn{n}", R, base if n != 12 else base, a.games, a.epochs, a.workers
        )
        show(f"Mixed table, {n} players, {a.epochs} epochs, {a.games} games", rows, eta)


def exp_seats(a):
    R = BASE
    print("\n### Seat fairness (all players the same strategy)")
    for strat in ("smart", "random"):
        for n in (3, 5, 8, 12):
            g = a.games if strat == "smart" else a.games * 2
            rows = pmap(
                [(f"seat-{strat}-{n}-{i}", R, [strat] * n, a.epochs) for i in range(g)], a.workers
            )
            means = [statistics.mean(t[f"p{i}"] for t, _ in rows) for i in range(n)]
            wins = [statistics.mean(win_shares(t)[f"p{i}"] for t, _ in rows) for i in range(n)]
            sd = statistics.stdev([t[f"p{i}"] for t, _ in rows for i in range(n)])
            se = sd / g**0.5
            chi = sum((w - 1 / n) ** 2 / (1 / n) for w in wins) * g
            print(
                f"{strat:6} n={n:2}: mean total by seat {[round(m, 3) for m in means]} (se {se:.3f}); "
                f"win share by seat {[round(w, 3) for w in wins]}; chi2={chi:.1f} on {n - 1} dof"
            )


def exp_backing(a):
    R = BASE
    comp = ["random", "random", "backonly", "backonly", "pushonly", "pushonly", "smart", "smart"]
    rows, eta, _ = tournament("backing", R, comp, a.games, a.epochs, a.workers)
    show(
        "Backing vs pushing (8 players: random/random, crowd-backing + random pushes, "
        "random backing + planned pushes, smart both)",
        rows,
        eta,
    )
    comp = ["random", "backonly", "pushonly", "smart", "random"]
    rows, eta, _ = tournament("backing5", R, comp, a.games, a.epochs, a.workers)
    show("Same at 5 players", rows, eta)


def exp_degenerate(a):
    R = BASE
    print("\n### Degenerate strategies")
    for label, comp in (
        ("everyone greedy (all chase the biggest mouth)", ["greedy"] * 5),
        ("everyone smart (crowd-aware)", ["smart"] * 5),
        ("everyone random", ["random"] * 5),
        ("everyone freerider (back only, never push)", ["freerider"] * 5),
    ):
        out = pmap([(f"deg-{label}-{i}", R, comp, a.epochs) for i in range(a.games)], a.workers)
        mean_total = statistics.mean(x for t, _ in out for x in t.values())
        print(
            f"- {label}: mean total {mean_total:.3f} (of {a.epochs} max); back concentration (HHI, 1/3=even, 1=all same) "
            f"{hhi([h for _, h in out], R.mouths):.2f}"
        )
    for strat in ("freerider", "spoiler", "copycat", "random", "greedy", "lite", "contrarian"):
        comp = ["smart"] * 4 + [strat]
        rows, eta, _ = tournament(f"one-{strat}", R, comp, a.games, a.epochs, a.workers)
        s, m = rows[strat], rows["smart"]
        print(
            f"- one {strat} among 4 smart: {strat} {s['mean']:.3f} +-{s['ci']:.3f} (win {s['win']:.3f}) vs smart {m['mean']:.3f} "
            f"+-{m['ci']:.3f} (win {m['win']:.3f})"
        )


def exp_coalition(a):
    R = BASE
    print(
        "\n### Coalitions (pair of seats pooled, compared with the same two seats playing as solo smart)"
    )
    for n in (5, 8, 12):
        solo = [("smart")] * n
        base_jobs = [(f"co-base-{n}-{i}", R, solo, a.epochs) for i in range(a.games)]
        base = pmap(base_jobs, a.workers)
        base_pair = statistics.mean(t["p0"] + t["p1"] for t, _ in base)
        base_pair_ci = ci([t["p0"] + t["p1"] for t, _ in base])
        base_win = statistics.mean(win_shares(t)["p0"] + win_shares(t)["p1"] for t, _ in base)
        print(
            f"n={n}: two solo smart seats together: total {base_pair:.3f} +-{base_pair_ci:.3f}, win share (either) {base_win:.3f}"
        )
        for kind in ("pair_same", "pair_feeder", "pair_two"):
            comp = [f"team:{kind}", f"team:{kind}"] + ["smart"] * (n - 2)
            out = pmap(
                [(f"co-{kind}-{n}-{i}", R, comp, a.epochs) for i in range(a.games)], a.workers
            )
            pair = [t["p0"] + t["p1"] for t, _ in out]
            win = statistics.mean(win_shares(t)["p0"] + win_shares(t)["p1"] for t, _ in out)
            others = statistics.mean(
                statistics.mean(t[f"p{i}"] for i in range(2, n)) for t, _ in out
            )
            print(
                f"  {kind:11}: pair total {statistics.mean(pair):.3f} +-{ci(pair):.3f} "
                f"({statistics.mean(pair) / base_pair:.2f}x solo pair), win share (either) {win:.3f}; "
                f"other smart seats avg {others:.3f}"
            )


def exp_variance(a):
    R = BASE
    print("\n### How much is chance (rain, silt)? epoch-1 moves fixed, chance redrawn 40 times")
    rows_all = defaultdict(lambda: {"both": [], "rain": [], "silt": []})
    gap = defaultdict(list)
    games = max(10, a.games // 4)
    K = 40
    for g in range(games):
        seed = f"var-{g}".encode()
        seats = list(MIX5)
        random.Random(g).shuffle(seats)
        rngs = {f"p{i}": random.Random(seed_int(g, i)) for i in range(5)}
        widths = ec.open_widths(seed, R)
        moves = {}
        for i, s in enumerate(seats):
            v = View(R, 5, f"p{i}", 1, widths, None, {}, rngs[f"p{i}"])
            moves[f"p{i}"] = ec.validate_move(STRATS[s](v), R)
        after = ec.apply_pushes(widths, list(moves.values()), R)
        backs = {p: m["back"] for p, m in moves.items()}
        fixed_rain = ec.draw_rain(seed, 1, R)
        sc = defaultdict(lambda: {"both": [], "rain": [], "silt": []})
        for k in range(K):
            rs = f"rs{g}-{k}".encode()
            rain = ec.draw_rain(rs, 1, R)
            silt = ec.draw_silt(rs, 1, R)
            for key, (r_, s_) in (
                ("both", (rain, silt)),
                ("rain", (rain, None)),
                ("silt", (fixed_rain, silt)),
            ):
                sc_ = ec.score(backs, ec.avg_share(after, r_, s_, R))
                for p, x in sc_.items():
                    sc[p][key].append(float(x))
        for i, s in enumerate(seats):
            for key in ("both", "rain", "silt"):
                rows_all[s][key].append(statistics.pstdev(sc[f"p{i}"][key]))
            gap[s].append(statistics.mean(sc[f"p{i}"]["both"]))
    print(
        "| strategy | mean epoch-1 score | sd from chance (rain+silt) | sd rain only | sd silt only |"
    )
    print("|---|---|---|---|---|")
    for s in MIX5:
        d = rows_all[s]
        print(
            f"| {s} | {statistics.mean(gap[s]):.3f} | {statistics.mean(d['both']):.3f} | "
            f"{statistics.mean(d['rain']):.3f} | {statistics.mean(d['silt']):.3f} |"
        )


def exp_sweep(a):
    print(
        "\n### Parameter sweep (table: random, lite, greedy, smart, contrarian; smart/random totals and smart lift)"
    )
    base = BASE
    variants = [("default", base)]
    variants += [(f"mouths={m}", replace(BASE, mouths=m)) for m in (2, 4, 5)]
    variants += [
        (f"budget={b}", replace(BASE, push_budget=b, push_channels=6 if b <= 12 else 8))
        for b in (6, 24)
    ]
    variants += [("no silt", replace(BASE, silt_den=0)), ("silt 1/4", replace(BASE, silt_den=4))]
    variants += [("cap=10", replace(BASE, width_cap=10))]
    for label, R in variants:
        rows, eta, _ = tournament(
            f"sw-{label}", R, MIX5, max(40, a.games // 2), a.epochs, a.workers
        )
        print(
            f"- {label:10}: smart {rows['smart']['mean']:.3f} (lift {rows['smart']['lift']:.2f}x), "
            f"lite {rows['lite']['mean']:.3f}, greedy {rows['greedy']['mean']:.3f}, contrarian {rows['contrarian']['mean']:.3f}, "
            f"random {rows['random']['mean']:.3f} (lift {rows['random']['lift']:.2f}x); skill share of variance {eta * 100:.1f}%"
        )
    print("\n### Epoch count (default rules, same table)")
    for e in (1, 3, 6):
        rows, eta, _ = tournament(f"ep{e}", base, MIX5, a.games, e, a.workers)
        print(
            f"- {e} epochs: smart lift {rows['smart']['lift']:.2f}x, random lift {rows['random']['lift']:.2f}x, skill share {eta * 100:.1f}%"
        )


EXPS = {
    "tourn": exp_tourn,
    "seats": exp_seats,
    "backing": exp_backing,
    "degenerate": exp_degenerate,
    "coalition": exp_coalition,
    "variance": exp_variance,
    "sweep": exp_sweep,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--crowd-pow", type=int, default=1)
    ap.add_argument("--mouths", type=int, default=3)
    ap.add_argument("--silt-den", type=int, default=8)
    ap.add_argument("--only", default=",".join(EXPS))
    a = ap.parse_args()
    global BASE
    BASE = ec.Rules(mouths=a.mouths, crowd_pow=a.crowd_pow, silt_den=a.silt_den)
    print(f"<!-- rules: {BASE} -->")
    for name in a.only.split(","):
        t0 = time.time()
        EXPS[name](a)
        print(f"[{name}: {time.time() - t0:.0f}s]", file=sys.stderr)


if __name__ == "__main__":
    main()
