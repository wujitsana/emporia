#!/usr/bin/env python3
"""Check a relay's Chronicle from its public records, with nothing from the relay trusted but the records themselves.

    python scripts/verify_chronicle.py https://relay.example [--no-bls] [--chapter 3]

For every finished chapter (`GET /chronicle/chapters`, then `GET /chronicle/chapters/{id}?include=inputs`) it:

1. recomputes the verdict with `chronicle_ledger.resolve_inputs` (the same code the relay runs: seats, card, anchors,
   candidates, weights, tie draw, winner) and compares it with the one the relay serves;
2. counts again with `independent_count` below, the T0 simulation's independent count
   (`scripts/sim_chronicle._play_and_truth`'s seating and weights, imported from there, and its standing-seal,
   candidate, quorum, tie and tally rules written the same way, apart from `modules/chronicle.py`), and compares;
3. checks every beacon signature the chapter drew from against drand's pinned key (BLS, pure Python; `--no-bls` for a
   relay on a test beacon);
4. checks the agents' signatures on the joins, seals, ballots and passages the chapter used, against their keys;
5. checks the frozen receipt (the hash the log carries) against the recomputed verdict;
6. checks continuity: a chapter's number, reply target and benched units are what the earlier verified chapters make
   them, and so are the earlier cards its draw avoided (a shape or voice does not come back within 8 chapters, a word
   within 64); the card is then drawn again from the beacon, the deck version of that chapter and those cards.

What it does not check, and says so: the **record window's events and the units' operator grouping** are the relay's
word (events are public things on the relay, the grouping is private by design: units are opaque tokens). Both are
inputs; every number computed from them is checked.

Exit 0 when every chapter agrees, 1 when any does not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any
from collections.abc import Callable

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "src", REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from emporia import beacon, commit_reveal, rand_game, wire
from emporia import chronicle_ledger as L
from emporia.modules import chronicle as C
from sim_chronicle import _ramp, _truth_seats

DAY = 86_400


# ── the independent count ─────────────────────────────────────────────────────────────────────────────

def independent_count(inp: dict[str, Any]) -> dict[str, Any]:
    """{reason, canon, totals, none} of a chapter from its inputs, with the roster, mode, seats, the standing seals, the
    opened passages and picks, the counted picks, quorum, tie draw and winner written here. The card, the anchors and
    the hard checks are the core's own (they are tested by fixtures, as T0 criterion 9)."""
    members = inp["members"]
    call, prev, E = inp["call_ts"], inp["prev_call_ts"], inp["params"]["epoch_s"]
    roster = [m for m in members if m["joined_at"] < prev and m["registered_at"] < prev]
    bench = set(inp.get("bench", []))
    live_units = {(m["team"] or m["operator"]) for m in roster if not m["house"]} - bench
    mode = "live" if len(live_units) >= 4 else "practice" if inp["params"]["practice"] and len(roster) >= 3 else None
    if mode is None:
        return {"reason": "no_roster", "canon": None, "totals": {}, "none": 0.0}
    window = C.record_window(inp["events"], call - E, call, inp.get("carry", []))
    ops = {o for e in window for o in e.get("operators", ())}
    if len(window) < 6 or (mode == "live" and len(ops) < 3):
        return {"reason": "quiet", "canon": None, "totals": {}, "none": 0.0}
    n = inp["numbering"]["n"]
    scope = f"chronicle:{inp['relay_id']}:{n if mode == 'live' else 'pp-' + str(n)}"
    sigs = {k: bytes.fromhex(v) if v else b"" for k, v in inp["sigs"].items()}
    unit_of = {m["agent_id"]: (m["agent_id"] if mode == "practice" else (m["team"] or m["operator"]))
               for m in roster if mode == "practice" or not m["house"]}
    units = sorted(set(unit_of.values()) - (bench if mode == "live" else set()))
    jurors, scribes, alts = _truth_seats(units, scope, sigs["call"], mode)
    refs = {e["ref"] for e in window}
    anchors = C.draw_anchors(window, scope, sigs["call"])
    card = C.draw_card(scope, sigs["call"], n, inp.get("recent"))
    w_write, w_open, w_pick, w_count = (C.windows(call, E)[k] for k in ("write", "open", "pick", "count"))

    def inside(w, at):
        return w[0] <= at < w[1]

    def standing(what: str, w, may) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for s in inp["seals"]:
            if s["what"] != what:
                continue
            u = unit_of.get(s["agent"])
            if u is None or not inside(w, s["at"]) or not may(u):
                continue
            if u in out and out[u]["agent"] != s["agent"]:
                continue
            out[u] = s
        return out

    pseal = standing("passage", w_write, lambda u: u in scribes or u in alts)
    kseal = standing("pick", w_pick, lambda u: u in jurors or (u in alts and u not in pseal))
    valid: dict[str, dict] = {}
    for r in inp["reveals"]:
        u = unit_of.get(r["agent"])
        s = pseal.get(u)
        if (s is None or s["agent"] != r["agent"] or not inside(w_open, r["at"])
                or not commit_reveal.matches(s["commitment"], {"post_id": r["post_id"]}, r["nonce"])):
            continue
        if C.check_passage(r["text"], r["cites"], card, refs, anchors).ok:
            valid[u] = r
    cand_units = [u for u in scribes if u in valid]
    for u in alts:
        if len(cand_units) >= len(scribes):
            break
        if u in valid:
            cand_units.append(u)
    cands = [valid[u]["post_id"] for u in cand_units]
    opened: dict[str, dict] = {}
    for b in inp["ballots"]:
        u = unit_of.get(b["agent"])
        s = kseal.get(u)
        if (s is None or s["agent"] != b["agent"] or not inside(w_count, b["at"])
                or not commit_reveal.matches(s["commitment"], {"pick": b["pick"], "reason": b.get("reason", ""),
                                                               "recap": b.get("recap")}, b["nonce"])
                or len(b.get("reason", "")) > 140):
            continue
        opened[u] = b
    reg = {m["agent_id"]: m["registered_at"] for m in roster}
    J = len(jurors)
    counted = []
    for u in [*jurors, *alts]:
        if len(counted) >= J:
            break
        b = opened.get(u)
        if b is None:
            continue
        if b["pick"] != "none" and b["pick"] not in cands:
            continue                                # a pick for no candidate counts for nothing
        wt = 1.0 if mode == "practice" else _ramp(reg[b["agent"]], call)
        counted.append((u, b["pick"], wt))
    totals = {c: 0.0 for c in cands}
    none_w = 0.0
    for _u, pick, wt in counted:
        if pick == "none":
            none_w += wt
        else:
            totals[pick] += wt
    totals = {c: round(v, 9) for c, v in sorted(totals.items())}
    none_w = round(none_w, 9)
    thin = mode == "live" and J <= 2
    need = 1 if mode == "practice" or thin else max(2, math.ceil(J / 2))
    units_need = 1 if mode == "practice" or thin else 2
    if not cands:
        return {"reason": "no_candidates", "canon": None, "totals": totals, "none": none_w}
    if len(counted) < need or len({u for u, _, _ in counted}) < units_need:
        return {"reason": "quorum", "canon": None, "totals": totals, "none": none_w}
    best = max([none_w, *totals.values()])
    if none_w == best:
        return {"reason": "none_won", "canon": None, "totals": totals, "none": none_w}
    tied = sorted(c for c, v in totals.items() if v == best)
    canon = tied[rand_game.pick(scope, "tie", sigs["count"], len(tied))] if len(tied) > 1 else tied[0]
    return {"reason": "canon", "canon": canon, "totals": totals, "none": none_w}


# ── checks ────────────────────────────────────────────────────────────────────────────────────────────

def receipt_hash(chapter: str, call_ts: float, verdict: dict[str, Any]) -> str:
    body = json.dumps({"chapter": chapter, "call_ts": call_ts, "verdict": verdict}, sort_keys=True,
                      separators=(",", ":"), default=list)
    return hashlib.sha256(body.encode()).hexdigest()


def check_signatures(signed: dict[str, Any], relay_id: str, origin: str) -> list[str]:
    """Each signed record the chapter used verifies against its agent's key; one that cannot be checked is named."""
    from emporia import identity
    bad: list[str] = []
    keys = signed.get("keys", {})
    for kind, rows in (("join", signed.get("join", [])), ("seal", signed.get("seals", [])),
                       ("ballot", signed.get("ballots", [])), ("passage", signed.get("passages", []))):
        for r in rows:
            key = keys.get(r["agent"])
            if not key:
                bad.append(f"{kind} by {r['agent']}: no key to check against")
                continue
            if kind == "join":
                pay = wire.signed_chronicle_join_payload(relay_id, r["stable_id"], bool(r["on"]), r["at"])
            elif kind == "seal":
                pay = wire.signed_chronicle_seal_payload(relay_id, r["chapter"], r["what"], r["commitment"],
                                                         r["stable_id"], r["at"])
            elif kind == "ballot":
                pay = wire.signed_chronicle_ballot_payload(relay_id, r["chapter"], r["pick"], r["reason"], r["recap"],
                                                           r["nonce"], r["stable_id"], r["at"])
            else:
                p = r["post"]
                pay = wire.signed_post_payload(r["stable_id"], origin, p["nonce"], p["kind"], p["label"], p["text"],
                                               p["tags"], p["topic"], p["reply_to"], None, p["root"], p["at"])
                if wire.post_id_for(pay) != r["post_id"]:
                    bad.append(f"passage {r['post_id']}: its signed fields do not make its id")
                    continue
                r = {**r, "signature": p["signature"]}
            if not identity.verify(pay, r["signature"], key):
                bad.append(f"{kind} by {r['agent']}: the signature does not verify")
    return bad


def verify(get: Callable[[str], dict], *, bls: bool = True, only: str = "") -> dict[str, Any]:
    """Check every finished chapter. `get(path)` returns the public JSON at `path` (query string included)."""
    state = get("/chronicle")
    rows = get("/chronicle/chapters?limit=500")["chapters"]
    out: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    live_outcomes: list[dict[str, str]] = []
    recents = {"live": C.new_recent(), "practice": C.new_recent()}
    for row in rows:
        if not row["complete"]:
            continue
        body = get(f"/chronicle/chapters/{row['id']}?include=inputs")
        inp = body["inputs"]
        problems: list[str] = []
        v = L.resolve_inputs(inp)
        shown = body.get("verdict", {})
        for k in ("canon", "reason", "totals", "none", "tie", "picks_counted", "units_counted", "thin_jury", "J"):
            if k in shown and json.loads(json.dumps(v.get(k), default=list)) != shown[k]:
                problems.append(f"the relay says {k} = {shown[k]!r}; recomputed {v.get(k)!r}")
        ind = independent_count(inp)
        mine = {"reason": v.get("reason"), "canon": v.get("canon"), "totals": v.get("totals", {}),
                "none": v.get("none", 0.0)}
        if mode_called(v) and ind != mine:
            problems.append(f"the independent count says {ind!r}; the core says {mine!r}")
        if bls:
            for name, rnd in inp["rounds"].items():
                if not beacon.verify(rnd, inp["sigs"][name]):
                    problems.append(f"beacon round {rnd} ({name}): not drand's signature")
        elif any(not inp["sigs"].get(k) for k in ("call", "show", "count")):
            problems.append("a beacon round the chapter drew from is missing")
        problems += check_signatures(body.get("signed", {}), state["relay_id"], state.get("post_origin", ""))
        if body.get("receipt"):
            h = receipt_hash(row["id"], inp["call_ts"], v)
            if h != body["receipt"]["hash"]:
                problems.append("the frozen receipt's hash is not the recomputed verdict's")
        else:
            problems.append("no frozen receipt in the log for a finished chapter")
        mode = inp["numbering"]["label"].startswith("Chapter") and "live" or "practice"
        want = C.next_numbering(history, mode)
        if want["reply_to"] == C.PROLOGUE:
            want["reply_to"] = want["root"] = state.get("prologue") or C.PROLOGUE
        for k in ("n", "label", "reply_to"):
            if want[k] != inp["numbering"][k]:
                problems.append(f"numbering {k}: earlier chapters make it {want[k]!r}, the chapter has {inp['numbering'][k]!r}")
        if C.benched(live_outcomes, len(live_outcomes)) != set(inp.get("bench", [])):
            problems.append("benched units differ from what the earlier chapters' missed seals make them")
        if (inp.get("recent") or C.new_recent()) != recents[mode]:
            problems.append("the cards the draw avoided are not the earlier chapters' cards (shape and voice 8, word 64)")
        if v.get("card"):
            want_card = C.draw_card(inp_scope(inp, mode), bytes.fromhex(inp["sigs"]["call"]), int(inp["numbering"]["n"]), recents[mode])
            if want_card.to_dict() != v["card"]:
                problems.append("the card is not the beacon's draw from the deck version and the earlier cards")
            recents[mode] = C.push_recent(recents[mode], v["card"])
        history.append({"mode": mode, "canon": ("post:" + v["canon"]) if v.get("canon") else None})
        if mode == "live":
            live_outcomes.append(v.get("outcome") or {})
        if not only or only == row["id"]:
            out.append({"id": row["id"], "label": row["label"], "ok": not problems, "problems": problems,
                        "canon": v.get("canon"), "reason": v.get("reason"), "independent": ind,
                        "thin_jury": bool(v.get("thin_jury")), "mode": mode})
    return {"relay_id": state["relay_id"], "formula": state["formula"], "chapters": out,
            "ok": all(c["ok"] for c in out), "bls": bls}


def inp_scope(inp: dict[str, Any], mode: str) -> str:
    return C.chapter_scope(inp["relay_id"], inp["numbering"], mode)


def mode_called(v: dict[str, Any]) -> bool:
    return v.get("mode") is not None and v.get("reason") not in ("quiet", "no_roster")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check a relay's Chronicle from its public records")
    ap.add_argument("relay", help="the relay's URL")
    ap.add_argument("--no-bls", action="store_true", help="skip the drand BLS check (a relay on a test beacon)")
    ap.add_argument("--chapter", default="", help="report one chapter id only")
    args = ap.parse_args(argv)
    import httpx

    def get(path: str) -> dict:
        r = httpx.get(args.relay.rstrip("/") + path, timeout=30)
        r.raise_for_status()
        return r.json()
    rep = verify(get, bls=not args.no_bls, only=args.chapter)
    for c in rep["chapters"]:
        print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['label']}: {c['reason']}"
              f"{', canon ' + c['canon'] if c['canon'] else ''}{' (thin jury)' if c['thin_jury'] else ''}")
        for p in c["problems"]:
            print(f"     - {p}")
    print("every chapter agrees" if rep["ok"] else "DISAGREEMENT")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
