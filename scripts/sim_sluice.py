#!/usr/bin/env python3
"""Sluice balance report (offline, seeded; not part of the test run).

Plays many games through the module's own pure functions (`deal`, `first`, `gate`, `place`, `verdict`) between
simple reference bots and prints how often the first and the second mover win, and how often the better bot beats
the weaker one. The bots are not strong players; read the numbers as direction, not as measured balance.

    .venv/bin/python scripts/sim_sluice.py [--games 1000] [--seed 1] [--drops 7]

Acceptance for the module (games plan, WP G2): between two look-ahead bots the first mover wins 45-55 %; the
look-ahead bot beats the greedy bot at least 75 %. Outside that, stop and report: do not tune silently.
Bots: `random`, `greedy` (win, else block, else random), `look1` and `look2` (1 and 2 plies of search on a
line-count heuristic; the next gate is unknown to a bot, so the opponent's reply ranges over every channel).
"""

from __future__ import annotations

import argparse
import hashlib
import random
from collections import Counter

from emporia.modules import sluice as S

SEATS = ["A", "B"]


def _marks(who: str) -> tuple[str, str]:
    return ("A", "B") if who == "A" else ("B", "A")


def heuristic(grid: S.Grid, me: str) -> float:
    """Open lines weighted by how many stones each side has in them (a line holding both is dead)."""
    mine, theirs = _marks(me)
    score = 0.0
    weight = {0: 0.0, 1: 1.0, 2: 6.0, 3: 40.0, 4: 10000.0}
    for line in S.LINES:
        cells = [S._at(grid, c) for c in line]
        a, b = cells.count(mine), cells.count(theirs)
        if a and b:
            continue
        score += weight[a] if a else -weight[b]
    return score


def _wins(grid: S.Grid, mark: str, channels: list[int]) -> list[int]:
    return [c for c in channels if S.four_line(S.place(grid, c, mark)[0], mark)]


def choose(bot: str, grid: S.Grid, me: str, options: list[int], free: list[int], rng: random.Random) -> int:
    mine, theirs = _marks(me)
    if bot == "random":
        return rng.choice(options)
    if w := _wins(grid, mine, options):
        return rng.choice(w)
    if bot == "greedy":
        block = [c for c in _wins(grid, theirs, free) if c in options]
        return rng.choice(block or options)
    best, best_v = [], -1e18
    for c in options:
        g1, _ = S.place(grid, c, mine)
        if bot == "look1":
            v = heuristic(g1, me) + rng.random() * 1e-3
        else:           # look2: the opponent replies in any free channel; take its best
            replies = S.free_channels(g1)
            if not replies:
                v = heuristic(g1, me)
            else:
                v = min((-1e6 if S.four_line(S.place(g1, r, theirs)[0], theirs) else heuristic(S.place(g1, r, theirs)[0], me))
                        for r in replies) + rng.random() * 1e-3
        if v > best_v:
            best, best_v = [c], v
    return best[0]


def play(bots: dict[str, str], seed: int, drops: int = 7) -> dict:
    """One game. `bots` maps seat A/B to a bot name. Returns who won, how, and who moved first."""
    rng = random.Random(seed)
    scope = f"sim:{seed}"
    sig0 = hashlib.sha256(f"{seed}:0".encode()).digest()
    d = S.deal(scope, sig0)
    opening = {"silt": d["silt"], "stones": {"A": d["stones"][0], "B": d["stones"][1]},
               "first": S.first(scope, sig0, SEATS), "attempt": d["attempt"]}
    data = {"seats": SEATS, "drops": drops, "opening": opening, "moves": []}
    grid = S._grid_of(data, opening)
    order = S._order(data, opening)
    for n in range(1, 2 * drops + 1):
        free = S.free_channels(grid)
        if not free:
            break
        sig = hashlib.sha256(f"{seed}:{n}".encode()).digest()
        g = S.gate(scope, n, sig, free)
        options = [c for c in free if c != g]
        who = order[(n - 1) % 2]
        ch = choose(bots[who], grid, who, options, free, rng)
        grid, _ = S.place(grid, ch, who)
        data["moves"].append({"by": who, "channel": ch})
        if S.four_line(grid, who):
            break
    res = S.verdict(data)
    assert res is not None
    return {"winner": res["winner"], "how": res["termination"], "first": opening["first"], "moves": len(data["moves"]),
            "attempt": opening["attempt"]}


def run(a: str, b: str, games: int, seed: int, drops: int) -> dict:
    """`a` plays seat A in even games, seat B in odd games, so seat and bot are not confounded."""
    first_wins = a_wins = 0
    how: Counter = Counter()
    first_by_how: Counter = Counter()
    moves = 0
    redeals = 0
    for i in range(games):
        bots = {"A": a, "B": b} if i % 2 == 0 else {"A": b, "B": a}
        r = play(bots, seed * 1_000_003 + i, drops)
        first_wins += r["winner"] == r["first"]
        a_wins += bots[r["winner"]] == a
        how[r["how"]] += 1
        if r["winner"] == r["first"]:
            first_by_how[r["how"]] += 1
        moves += r["moves"]
        redeals += r["attempt"] > 0
    return {"a": a, "b": b, "games": games, "first_mover_win_pct": 100 * first_wins / games,
            "a_win_pct": 100 * a_wins / games, "mean_moves": moves / games,
            "by": {k: f"{v} ({100 * v / games:.0f} %)" for k, v in sorted(how.items())},
            "first_won_by": dict(first_by_how), "redealt_pct": 100 * redeals / games}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--games", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--drops", type=int, default=7)
    ap.add_argument("--pairs", default="look2:look2,look2:greedy,look1:look1,greedy:greedy,look2:random")
    args = ap.parse_args()
    for pair in args.pairs.split(","):
        a, b = pair.split(":")
        r = run(a, b, args.games, args.seed, args.drops)
        print(f"{a} v {b}: {r['games']} games, drops {args.drops}, mean moves {r['mean_moves']:.1f}, "
              f"redealt {r['redealt_pct']:.1f} %")
        print(f"  first mover wins {r['first_mover_win_pct']:.1f} %   {a} wins {r['a_win_pct']:.1f} %")
        print(f"  ended by {r['by']}   first mover's wins by {r['first_won_by']}")


if __name__ == "__main__":
    main()
