#!/usr/bin/env python3
"""Verify finished Tesseract tables without trusting the relay: recompute everything from the public record.

    python scripts/verify_tesseract.py --relay http://127.0.0.1:8000 [--session sess_...] [--no-bls]

For each finished table (`GET /tesseract` lists the recent ones; `--session` names one) it reads the stored game
(`GET /sessions/{id}`), the relay's declared result (`GET /results`) and its ledger (`GET /tesseract/results`), and
recomputes, from the stored beacons, seals, opens and revealed secrets alone and with the pure rules
(`emporia.modules.tesseract_core`, not the game module):

* every beacon signature, against drand's pinned key (`--no-bls` for a test beacon);
* every revealed secret against its signed commitment, and every opened push against its seal;
* the opening network and, round by round, the pulses, dark channels, widths, mix and who pushed what;
* each revealed target, each player's points per round, the totals, the places and the table index;

and compares each with what the relay published: the stored records, the declared totals and winner, the ledger rows.
Prints one line per table; exits 1 if any table fails. Stdlib and this repo's own pure modules; nothing is sent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from emporia import commit_reveal, rand
from emporia.modules import tesseract_core as core

#: the rules the game runs (the T0 report's recommended set), written out here and not imported from the module
RULES = core.Rules(width_cap=12, open_hi=12, unit_cap=4)
EPOCHS = 5
EVEN = (25, 25, 25, 25)
Get = Callable[[str], Any]


class BlsMissing(RuntimeError):
    """py_ecc is not installed: the beacon signatures cannot be checked, which is not a finding about the relay."""


def _beacons(d: dict[str, Any], bls: bool) -> tuple[dict[str, bytes], list[str]]:
    out, problems = {}, []
    r = d.get("rand") or {}
    sigs = dict(r.get("beacons") or {})
    if r.get("beacon_sig") and r.get("round"):
        sigs.setdefault(str(r["round"]), r["beacon_sig"])
    for rnd, sig in sigs.items():
        if bls:
            from emporia import beacon
            if not beacon.verify(int(rnd), sig):
                problems.append(f"the stored signature of drand round {rnd} is not drand's")
        out[str(rnd)] = bytes.fromhex(sig)
    return out, problems


def recompute(d: dict[str, Any], bls: bool = True) -> tuple[dict[str, Any], list[str]]:
    """The whole game from the stored actions: {opening, epochs, targets, totals, order, index, winners, out,
    points}, and the problems found while doing it (a beacon, a secret or a push that is not what it should be)."""
    problems: list[str] = []
    sigs, bad = _beacons(d, bls)
    problems += bad
    seats = list(d["seats"])
    r = d["rand"]
    commits = r.get("commits") or {}
    sig0 = sigs.get(str(r.get("round")))
    if sig0 is None:
        return {}, problems + ["the game's own drand round is not stored"]
    reveals = r.get("reveals") or {}
    for p, rev in reveals.items():
        if rand.commit_hash(bytes.fromhex(rev["s"]), bytes.fromhex(rev["nonce"])) != commits[p]["hash"]:
            problems.append(f"{p}'s revealed secret is not what it committed to")
    seed0 = rand.rand(r["scope"], "tesseract", sig0)
    widths, attempt = core.open_network(seed0, RULES)
    opening = {"widths": list(widths), "attempt": attempt,
               "mix": list(core.mix(widths, core.draw_pulses(seed0, 0, RULES), None, RULES))}
    out: set[str] = set()
    last = int(r["round"])
    epochs, history = [], []
    for e in range(1, EPOCHS + 1):
        seals = {p: s for p, s in ((d.get("seals") or {}).get(str(e)) or {}).items() if p not in out}
        opens = (d.get("opens") or {}).get(str(e)) or {}
        moves = {}
        for p in seals:
            if p in opens:
                mv = opens[p]["move"]
                if not commit_reveal.matches(seals[p]["commitment"], mv, opens[p]["nonce"]):
                    problems.append(f"round {e}: {p}'s opened push is not what it sealed")
                    continue
                try:
                    core.validate_move(mv, RULES)
                except ValueError as exc:
                    problems.append(f"round {e}: {p}'s push is not legal ({exc})")
                    continue
                moves[p] = mv
        late = [p for p in seals if p not in moves]
        named = [int(s["round"]) for s in seals.values()]
        rnd = max(named) if named else last
        if any(n <= last for n in named):
            problems.append(f"round {e}: a seal names a drand round that is not later than round {last}")
        sig = sigs.get(str(rnd))
        if sig is None:
            return {"opening": opening, "epochs": epochs}, problems + [f"drand round {rnd} (round {e}) is not stored"]
        pub = core.play_epoch(rand.rand(r["scope"], f"tesseract:{e}", sig), e, widths,
                              {p: moves.get(p, {"push": {}}) for p in seats}, RULES, with_pull=False)
        ids = core.channel_ids()
        epochs.append({"epoch": e, "round": rnd, "pulses": [list(x) for x in pub.pulses],
                       "dark": [ids[i] for i, x in enumerate(pub.dark) if x], "widths": list(pub.widths_after),
                       "mix": list(pub.mix), "pushes": {p: dict(m["push"]) for p, m in moves.items() if m["push"]},
                       "before": widths, "moves": {p: moves.get(p, {"push": {}}) for p in seats},
                       "raw": (pub.pulses, pub.dark)})
        history.append(pub)
        widths, last = pub.widths_after, rnd
        out |= set(late)
    targets = {}
    for seat, p in enumerate(seats):
        if p in reveals:
            targets[p] = core.deal_target(rand.priv(r["scope"], seat, "target", sig0, bytes.fromhex(reveals[p]["s"])), RULES)
    tb = {p: targets.get(p, EVEN) for p in seats}
    points = [core.epoch_points(e["before"], e["moves"], tb, e["raw"][0], e["raw"][1], RULES) for e in epochs]
    valid = [p for p in seats if p in targets and p not in out]
    totals = {p: (sum(x[p]["points"] for x in points) if p in valid else 0) for p in seats}
    top = max((totals[p] for p in valid), default=None)
    winners = [p for p in valid if totals[p] == top] if valid else []
    order = sorted(valid, key=lambda p: (-totals[p], seats.index(p))) + [p for p in seats if p not in valid]
    return {"opening": opening, "epochs": epochs, "targets": targets, "totals": totals, "order": order, "valid": valid,
            "index": core.table_index(totals), "winners": winners, "out": sorted(out), "points": points}, problems


def check_table(sess: dict[str, Any], verdict: dict[str, Any] | None, ledger: list[dict[str, Any]],
                bls: bool = True) -> list[str]:
    d = (sess.get("state") or {}).get("data") or {}
    got, problems = recompute(d, bls)
    if "totals" not in got:
        return problems or ["the table is not finished"]
    pub = d.get("published") or {}
    if pub.get("opening") is not None and pub["opening"] != got["opening"]:
        problems.append("the published opening network is not what the game's beacon gives")
    for e in got["epochs"]:
        stored = pub.get(str(e["epoch"]))
        if stored is None:
            problems.append(f"round {e['epoch']} was never published")
            continue
        for k in ("pulses", "dark", "widths", "mix", "pushes"):
            if stored.get(k) != e[k]:
                problems.append(f"round {e['epoch']}: the published {k} are not what the beacon and the moves give")
    for p, t in (d.get("targets") or {}).items():
        if got["targets"].get(p) != tuple(t):
            problems.append(f"{p}'s published target is not the one its secret and the beacon give")
    if verdict is not None:
        if verdict.get("points") != got["totals"]:
            problems.append("the declared totals are not what the moves and the revealed targets give")
        want = got["winners"][0] if len(got["winners"]) == 1 else None
        if verdict.get("termination") in ("table", None) and verdict.get("winner") != want:
            problems.append(f"the rules give {want!r} as the winner, the relay declared {verdict.get('winner')!r}")
    if (sess.get("config") or {}).get("practice") is True:       # a practice table counts toward nothing: no ledger row
        if ledger:
            problems.append("a practice table has ledger rows: it counts toward nothing")
        return problems
    rows = {r["agent_id"]: r for r in ledger}
    for place, p in enumerate(got["order"], start=1):
        row = rows.get(p)
        if row is None:
            problems.append(f"{p} has no ledger row")
            continue
        if (row["total"], row["table_index"], row["place"]) != (got["totals"][p], got["index"][p], place):
            problems.append(f"{p}'s ledger row (total, index, place) is not what the table gives")
    return problems


def verify(get: Get, bls: bool = True, only: str = "") -> dict[str, Any]:
    """`get(path)` returns the relay's JSON for a path. Verifies `only`, or every recent finished table."""
    if bls:
        from emporia import beacon
        if not beacon.bls_available():
            raise BlsMissing(beacon.BLS_MISSING)      # before anything is read: no table is judged by a check that cannot run
    ids = [only] if only else sorted({r["session_id"] for r in (get("/tesseract").get("recent") or [])})
    tables = []
    for sid in ids:
        sess = get(f"/sessions/{sid}")
        if sess.get("status") != "completed":
            tables.append({"session_id": sid, "ok": False, "problems": [f"the table is {sess.get('status')}"]})
            continue
        verdict = next((r for r in get("/results?module_type=emporia:tesseract:v1&limit=200").get("results") or []
                        if r["session_id"] == sid), None)
        ledger = [r for r in get("/tesseract/results?limit=200").get("results") or [] if r["session_id"] == sid]
        problems = check_table(sess, verdict, ledger, bls)
        tables.append({"session_id": sid, "ok": not problems, "problems": problems})
    return {"tables": tables}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--relay", required=True)
    ap.add_argument("--session", default="")
    ap.add_argument("--no-bls", action="store_true")
    args = ap.parse_args()
    import httpx

    def get(path: str) -> Any:
        return httpx.get(args.relay.rstrip("/") + path, timeout=20).json()
    try:
        rep = verify(get, bls=not args.no_bls, only=args.session)
    except BlsMissing as e:
        print(str(e), file=sys.stderr)
        return 2
    for t in rep["tables"]:
        print(f"{t['session_id']}: {'ok' if t['ok'] else 'FAIL'}" + ("" if t["ok"] else "\n  " + "\n  ".join(t["problems"])))
    print(json.dumps({"tables": len(rep["tables"]), "failed": sum(not t["ok"] for t in rep["tables"])}))
    return 0 if rep["tables"] and all(t["ok"] for t in rep["tables"]) else 1


if __name__ == "__main__":
    sys.exit(main())
