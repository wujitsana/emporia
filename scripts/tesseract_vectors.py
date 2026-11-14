"""Tesseract's vectors: a fixed four-seat game (fixed beacon signatures, secrets, pushes and nonces) and everything the
pure rules give for it, so a second implementation (the dashboard's TypeScript, T2) is held to the Python one.

    .venv/bin/python scripts/tesseract_vectors.py > dashboard/src/tesseract.vectors.json

`tests/test_tesseract_vectors.py` fails until the committed file is what this makes, and replays the stored game
through the game module and `scripts/verify_tesseract.py`. Nothing here is random: the same file every run.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "src", REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from emporia import commit_reveal, rand  # noqa: E402
from emporia.modules import tesseract_core as core  # noqa: E402

RULES = core.Rules(width_cap=12, open_hi=12, unit_cap=4)
SEATS = ["agent-a", "agent-b", "agent-c", "agent-d"]
SCOPE = "sess_vectors"
R0 = 70_000_000
#: what each seat pushes in each of the five rounds (a sit-out is an empty entry: it seals nothing; "late" seals and does
#: not open)
PUSHES = [
    {"agent-a": {"WX>W": 4, "XY>X": -3}, "agent-b": {"XYZ>XY": 4, "YZ>Z": 4}, "agent-c": {"WY>Y": 2}, "agent-d": {}},
    {"agent-a": {"WX>W": 4, "WYZ>WY": 4}, "agent-b": {"XY>Y": -4, "XZ>Z": 3}, "agent-c": {"WZ>W": 4, "WZ>Z": -4}, "agent-d": {"YZ>Z": 4}},
    {"agent-a": {"WXY>WX": 4}, "agent-b": {"XYZ>XY": -4, "XZ>X": 4}, "agent-c": {}, "agent-d": {"WX>X": 4, "YZ>Y": 4}},
    {"agent-a": {"WX>W": -4, "WY>W": 4}, "agent-b": {"WXZ>WX": 4}, "agent-c": {"WY>Y": 4, "XY>Y": 4}, "agent-d": {"XYZ>YZ": 4}},
    {"agent-a": {"WZ>W": 4, "WX>W": 4}, "agent-b": {"YZ>Z": -4}, "agent-c": {"XZ>Z": 4, "XZ>X": -4}, "agent-d": {"WYZ>WY": 4}},
]


def at(rnd: int, back: int) -> str:
    """The time `back` seconds before drand round `rnd` is published (a seal names a round 4 rounds ahead of its time)."""
    return datetime.fromtimestamp(rand.round_time(rnd) - back, UTC).isoformat()


def beacon_sig(rnd: int) -> str:
    """A fixed stand-in 48-byte signature for a round (the test beacon)."""
    return hashlib.sha512(f"test-beacon-{rnd}".encode()).hexdigest()[:96]


def secret(seat: int) -> str:
    return hashlib.sha256(f"tesseract-vectors-secret-{seat}".encode()).hexdigest()


def nonce(seat: int, epoch: int) -> str:
    return hashlib.sha256(f"tesseract-vectors-nonce-{seat}-{epoch}".encode()).hexdigest()


def game_data() -> dict:
    """The stored state of the fixed game, as the module keeps it (what `verify_session` reads)."""
    commits = {p: {"hash": rand.commit_hash(bytes.fromhex(secret(i)), bytes.fromhex(secret(i + 10))), "seat": i,
                   "beacon_round": R0 - 5 + i} for i, p in enumerate(SEATS)}
    seals, opens = {}, {}
    rounds = {R0}
    last = R0
    for e, pushes in enumerate(PUSHES, start=1):
        rnd = last + 4 + e
        rounds.add(rnd)
        last = rnd
        seals[str(e)], opens[str(e)] = {}, {}
        for i, p in enumerate(SEATS):
            mv = core.validate_move({"push": pushes.get(p, {})}, RULES)
            seals[str(e)][p] = {"commitment": commit_reveal.commitment(mv, nonce(i, e)), "round": rnd + (i % 2) * -1,
                                "at": at(rnd + (i % 2) * -1, 12)}
            opens[str(e)][p] = {"move": mv, "nonce": nonce(i, e), "at": at(rnd, 6)}
        # the round a seal names: the latest of the four is `rnd`
        rounds |= {s["round"] for s in seals[str(e)].values()}
    return {"players": 4, "move_time_limit_s": 600, "reveal_window_s": 600, "seats": list(SEATS),
            "seals": seals, "opens": opens, "phase_started_at": at(R0, 0),
            "rand": {"scope": SCOPE, "commits": commits, "round": R0, "beacon_sig": beacon_sig(R0),
                     "beacons": {str(r): beacon_sig(r) for r in sorted(rounds)},
                     "reveals": {p: {"s": secret(i), "nonce": secret(i + 10)} for i, p in enumerate(SEATS)}}}


def build() -> dict:
    data = game_data()
    ids = list(core.channel_ids())
    sig0 = bytes.fromhex(beacon_sig(R0))
    seed0 = rand.rand(SCOPE, "tesseract", sig0)
    widths, attempt = core.open_network(seed0, RULES)
    opening = {"widths": list(widths), "attempt": attempt,
               "mix": list(core.mix(widths, core.draw_pulses(seed0, 0, RULES), None, RULES))}
    targets = {p: core.deal_target(rand.priv(SCOPE, i, "target", sig0, bytes.fromhex(secret(i))), RULES)
               for i, p in enumerate(SEATS)}
    epochs, cur, last = [], widths, R0
    for e in range(1, core.DEFAULT.epochs + 1):
        named = [int(s["round"]) for s in data["seals"][str(e)].values()]
        rnd = max(named)
        moves = {p: data["opens"][str(e)][p]["move"] for p in SEATS}
        pub = core.play_epoch(rand.rand(SCOPE, f"tesseract:{e}", bytes.fromhex(beacon_sig(rnd))), e, cur, moves, RULES)
        pts = core.epoch_points(cur, moves, targets, pub.pulses, pub.dark, RULES)
        epochs.append({"epoch": e, "round": rnd, "pulses": [list(x) for x in pub.pulses],
                       "dark": [ids[i] for i, d in enumerate(pub.dark) if d], "widths": list(pub.widths_after),
                       "mix": list(pub.mix), "pushes": {p: m["push"] for p, m in moves.items() if m["push"]},
                       "pull": {c: {k: list(v) for k, v in row.items()} for c, row in pub.pull.items()},
                       "points": {p: pts[p] for p in SEATS}})
        cur, last = pub.widths_after, rnd
    totals = {p: sum(x["points"][p]["points"] for x in epochs) for p in SEATS}
    return {
        "game": core.GAME_ID, "rules": {"width_cap": 12, "open_hi": 12, "unit_cap": 4, "budget": 8, "channels": 4},
        "channels": ids,
        "moves": {"legal": [{"push": {"WX>W": 4, "YZ>Z": -3}}, {"push": {}}, {"push": {"XY>X": 4, "XY>Y": 0}}],
                  "illegal": [{"push": {"WX>W": 5}}, {"push": {"WX>W": 4, "YZ>Z": 4, "XY>X": 1}},
                              {"push": {"A>B": 1}}, {"push": {"WX>W": 1.5}}, {"push": {"WX>W": True}},
                              {"push": {c: 2 for c in ids[:5]}}, {"push": {}, "back": 1}]},
        "targets": {"count": len(core.targets(RULES)), "first": list(core.targets(RULES)[0]),
                    "last": list(core.targets(RULES)[-1]),
                    "deal": [{"draw": hashlib.sha256(str(i).encode()).hexdigest(),
                              "target": list(core.deal_target(hashlib.sha256(str(i).encode()).digest(), RULES))}
                             for i in range(8)]},
        "mix": {"all_ones": list(core.mix(tuple([1] * 24), ((1, 1, 1, 1),) * 16, None, RULES)),
                "none": list(core.mix(tuple([0] * 24), ((1, 1, 1, 1),) * 16, None, RULES))},
        "table_index": {"totals": {"a": 3000, "b": 1000, "c": -500, "d": 0},
                        "index": core.table_index({"a": 3000, "b": 1000, "c": -500, "d": 0}),
                        "none_positive": core.table_index({"a": 0, "b": -1})},
        "game_data": data, "scope": SCOPE, "round": R0, "opening": opening,
        "secrets": {p: secret(i) for i, p in enumerate(SEATS)},
        "targets_dealt": {p: list(t) for p, t in targets.items()},
        "epochs": epochs, "totals": totals,
        "table_index_dealt": core.table_index(totals),
        "order": sorted(SEATS, key=lambda p: (-totals[p], SEATS.index(p))),
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n")
