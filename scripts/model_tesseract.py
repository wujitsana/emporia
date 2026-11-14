"""Tesseract with a real model in one seat (R4 probe) against the same seeds with `lite` there.

Seats: the model (or `lite` in the baseline) plus three bots: random, greedy, smart (the main
4-seat tournament's table with its `lite` seat given to the model). Recommended rules
(width_cap=12, open_hi=12, unit_cap=4). The seat is shuffled per seed; targets and chance depend
on the seed and seat name only, so model and baseline games are paired by seed.

The model is called one-shot through a throwaway Hermes profile, no tools:
    hermes -p tesseract-probe chat -Q --oneshot --provider nous -m <model> -t "" --ignore-rules --query-file F

    PYTHONPATH=src python scripts/model_tesseract.py --games 20 --out DIR [--dry]
    --dry: the model seat plays a stub (no calls) to test the harness.

WARNING: `-t ""` does NOT disable tools. In the first run 15 of 89 answered calls used the profile's
file/terminal tools (scratch scripts to optimise the push). A clean run needs every toolset
disabled first (e.g. `hermes -p tesseract-probe tools disable <each toolset>`) and one checked
call; see docs/games/tesseract-model-test.md.

Hard cap on model calls (default 120); beyond it the seat pushes nothing and says so. Every call
is logged to DIR/calls.jsonl; games to DIR/games.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import subprocess
import sys
import threading
from dataclasses import replace
from multiprocessing.pool import ThreadPool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_tesseract as sim  # noqa: E402
from emporia.modules import tesseract_core as tc  # noqa: E402

RULES = replace(tc.DEFAULT, width_cap=12, open_hi=12, unit_cap=4)
BOTS = ["random", "greedy", "smart"]
PROFILE, MODEL, PROVIDER = "tesseract-probe", "deepseek/deepseek-v4-flash-0731", "nous"

RULES_TEXT = """You play one seat in a 4-player network game, 5 epochs. Rules:
- 24 channels, each with a width 0..12. Pulses flow through the channels and end in 4 outputs W X Y Z; the mix is each output's share in basis points (10000 total).
- You have a private target mix (percent, sums to 100). You score by moving the mix toward your target. Your score counts how much your own pushes cut the distance (sum over outputs of |mix - target|), both added first and added after everyone else's. Other players have different targets.
- Each epoch every player pushes at once. A push is whole numbers per channel (+ widens, - narrows). At most 4 channels, at most 4 units on one channel, total |units| at most 8. All players' pushes are summed per channel and widths are clamped to 0..12. Dark channels (width 0 for that epoch) happen at random.
- The table below shows, for each channel, the change of the 4 output shares (basis points) for +4 or -4 on that channel alone ("up"/"down", order W X Y Z) on last epoch's pulses, and "gain" = the drop in your distance to your target if the mix moved by that change (positive is good for you; other players' pushes are not included).
- Pushes of all players are published after each epoch.
Answer: you may reason briefly, then the LAST line must be only a JSON object like {"push": {"XYZ>XY": 4, "XY>X": -4}} (an empty push is {"push": {}})."""


def _gains(widths, pulses, tgt):
    pl = tc.pull(widths, pulses, RULES)
    base = tc.mix(widths, pulses, None, RULES)
    rows = []
    for cid, w in zip(tc.channel_ids(), widths):
        g = tc.toward(pl[cid], base, tgt, RULES)
        rows.append(f"{cid} w={w} up={list(pl[cid]['up'])} down={list(pl[cid]['down'])} gain up={g['up']} down={g['down']}")
    return base, rows


def build_prompt(e, epochs, me, tgt, widths, pulses, last_mix, history, error=None):
    if pulses is None:  # epoch 1: no pulses seen yet; nominal ones stand in
        pulses = sim.NOMINAL * 4
    base, rows = _gains(widths, pulses, tgt)
    shown_mix = last_mix if last_mix is not None else base
    lines = [RULES_TEXT, "", f"You are {me}. Epoch {e} of {epochs}.",
             f"Your target (percent W X Y Z): {list(tgt)}",
             f"Current widths (channel order as in the table): {list(widths)}",
             ("Mix after last epoch (bp W X Y Z): " if last_mix is not None else
              "Mix of the opening network on nominal pulses (bp W X Y Z): ") + str(list(shown_mix))]
    if history:
        lines.append("Pushes in the last epoch: " + json.dumps({p: m["push"] for p, m in history[-1].items()}))
    else:
        lines.append("No pushes yet.")
    lines += ["Table:"] + rows
    if error:
        lines += ["", f"Your previous answer was invalid: {error}. Answer again."]
    return "\n".join(lines)


def parse_move(text):
    """Last JSON object in the text that has a "push" key."""
    for m in reversed([m.start() for m in re.finditer(r"\{", text)]):
        depth, j = 0, m
        for k in range(m, len(text)):
            depth += (text[k] == "{") - (text[k] == "}")
            if depth == 0:
                j = k + 1
                break
        else:
            continue
        try:
            obj = json.loads(text[m:j])
        except ValueError:
            continue
        if isinstance(obj, dict) and "push" in obj:
            return obj
    raise ValueError("no JSON object with a push found")


class Caller:
    def __init__(self, outdir: Path, cap: int, dry: bool):
        self.dir, self.cap, self.dry = outdir, cap, dry
        self.lock = threading.Lock()
        (self.dir / "q").mkdir(exist_ok=True)
        # resume: answers already logged are reused for an identical prompt (games replay the
        # same moves, so the same prompts come back) and are not new calls
        self.cache = {}
        log = self.dir / "calls.jsonl"
        if log.exists():
            for line in log.read_text().splitlines():
                r = json.loads(line)
                qf = self.dir / "q" / f"{r['call']}.txt"
                if qf.exists() and r["rc"] == 0:
                    self.cache[hashlib.sha256(qf.read_text().encode()).hexdigest()] = r["response"]
        self.n = max([int(f.stem) for f in (self.dir / "q").glob("*.txt")] or [0])
        self.reused = 0

    def call(self, prompt, meta):
        with self.lock:
            hit = self.cache.get(hashlib.sha256(prompt.encode()).hexdigest())
            if hit is not None:
                self.reused += 1
                return hit
            if self.n >= self.cap:
                return None
            self.n += 1
            k = self.n
        if self.dry:
            out = 'stub\n{"push": {}}'
            rc = 0
        else:
            qf = self.dir / "q" / f"{k}.txt"
            qf.write_text(prompt)
            try:
                p = subprocess.run(
                    ["hermes", "-p", PROFILE, "chat", "-Q", "--oneshot", "--provider", PROVIDER, "-m", MODEL,
                     "-t", "", "--ignore-rules", "--query-file", str(qf)],
                    capture_output=True, text=True, timeout=240)
                out, rc = p.stdout, p.returncode
            except subprocess.TimeoutExpired:
                out, rc = "", -9  # a call that never answered: counts as an invalid output
            out = re.sub(r"\n*session_id: \S+\s*$", "", out).strip()
        rec = {"call": k, "prompt_chars": len(prompt), "rc": rc, "response": out, **meta}
        with self.lock:
            with open(self.dir / "calls.jsonl", "a") as f:
                f.write(json.dumps(rec) + "\n")
        return out


STATS = {"calls": 0, "invalid": 0, "fallback": 0, "capped": 0}
SLOCK = threading.Lock()


def model_move(caller, seed, e, me, tgt, widths, pulses, last_mix, history):
    err = None
    for attempt in (1, 2):
        prompt = build_prompt(e, RULES.epochs, me, tgt, widths, pulses, last_mix, history, err)
        out = caller.call(prompt, {"seed": seed, "epoch": e, "attempt": attempt})
        if out is None:
            with SLOCK:
                STATS["capped"] += 1
                STATS["fallback"] += 1
            return {"push": {}}, "capped"
        with SLOCK:
            STATS["calls"] += 1
        try:
            mv = tc.validate_move(parse_move(out), RULES)
            tc.apply_pushes(widths, [mv], RULES)
            return mv, out
        except (ValueError, TypeError) as ex:
            err = str(ex)
            with SLOCK:
                STATS["invalid"] += 1
    with SLOCK:
        STATS["fallback"] += 1
    return {"push": {}}, "fallback: " + (err or "")



def run_game(seed, seats, caller, epochs=5):
    """Like sim_tesseract.run_game; seat `model` is the real model. Returns (totals, record)."""
    rules = RULES
    sb = str(seed).encode()
    n = len(seats)
    widths, _ = tc.open_network(sb, rules)
    names = [f"p{i}" for i in range(n)]
    tgt = {p: tc.deal_target(hashlib.sha256(sb + b"|target|" + p.encode()).digest(), rules) for p in names}
    totals = {p: 0 for p in names}
    rngs = {p: random.Random(sim.seed_int(seed, p)) for p in names}
    prev_widths = last_samples = last_mix = None
    my_last = {p: None for p in names}
    history, log = [], []
    for e in range(1, epochs + 1):
        moves, notes = {}, {}
        for p, s in zip(names, seats):
            if s == "model":
                moves[p], notes[p] = model_move(caller, seed, e, p, tgt[p], widths, last_samples, last_mix, history)
                continue
            v = sim.View()
            v.rules, v.n, v.me, v.epoch, v.widths = rules, n, p, e, widths
            v.prev_widths, v.last_samples, v.my_last = prev_widths, last_samples, my_last[p]
            v.tfrac = [x / 100 for x in tgt[p]]
            v.rng, v.history = rngs[p], history
            moves[p] = sim.STRATS[s](v)
        moves = {p: tc.validate_move(moves[p], rules) for p in names}
        pub = tc.play_epoch(sim.epoch_seed(sb, e), e, widths, moves, rules, with_pull=False)
        pts = tc.epoch_points(widths, moves, tgt, pub.pulses, pub.dark, rules)
        for p, d in pts.items():
            totals[p] += d["points"]
        log.append({"epoch": e, "moves": {p: m["push"] for p, m in moves.items()},
                    "points": {p: d["points"] for p, d in pts.items()}, "notes": notes})
        history.append(moves)
        my_last = dict(moves)
        prev_widths, widths, last_samples, last_mix = widths, pub.widths_after, pub.pulses, pub.mix
    return totals, {"seats": seats, "targets": tgt, "log": log}


def seats_for(seed, who):
    seats = list(BOTS) + [who]
    random.Random(sim.seed_int("modeltest", seed)).shuffle(seats)
    return seats


def share(totals, p):
    top = max(totals.values())
    w = [q for q, t in totals.items() if t == top]
    return 1 / len(w) if p in w else 0.0


def run_set(who, seeds, caller, workers):
    def one(seed):
        seats = seats_for(seed, who)
        tot, rec = run_game(seed, seats, caller if who == "model" else None, RULES.epochs) if who == "model" \
            else run_game_lite(seed, seats)
        me = f"p{seats.index(who)}"
        return {"seed": seed, "seat": me, "score": tot[me] // 2, "win": share(tot, me), "totals": tot, "rec": rec}
    with ThreadPool(workers) as pool:
        return pool.map(one, seeds)


def run_game_lite(seed, seats):
    # `lite` is a scripted bot: the same loop, with the model path never taken
    return run_game(seed, seats, None, RULES.epochs)


def ci(xs):
    return 1.96 * statistics.stdev(xs) / len(xs) ** 0.5 if len(xs) > 1 else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--cap", type=int, default=120)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    seeds = [f"mt-{i}" for i in range(a.games)]
    caller = Caller(out, a.cap, a.dry)
    base = run_set("lite", seeds, None, 1)
    mod = run_set("model", seeds, caller, min(a.workers, 2))
    with open(out / "games.jsonl", "w") as f:
        for tag, rs in (("lite", base), ("model", mod)):
            for r in rs:
                f.write(json.dumps({"who": tag, **r}) + "\n")
    ms, ls = [r["score"] for r in mod], [r["score"] for r in base]
    diff = [m - l for m, l in zip(ms, ls)]
    res = {"games": a.games, "calls": caller.n, "reused": caller.reused, **STATS,
           "model_mean": statistics.mean(ms), "model_ci": ci(ms), "lite_mean": statistics.mean(ls), "lite_ci": ci(ls),
           "diff_mean": statistics.mean(diff), "diff_ci": ci(diff),
           "model_win": statistics.mean(r["win"] for r in mod), "lite_win": statistics.mean(r["win"] for r in base)}
    (out / "summary.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
