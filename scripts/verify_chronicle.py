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

7. checks the drawn rounds are the first published after each boundary, and the log order: every seal counted for the
   chapter entered the transparency log before the first reveal of its kind, and `received_at` does not decrease along
   the log (each row's `log_index` is confirmed against `/log/entries`).

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
from emporia.rand import rand as rand_game_rand
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
    team_of = {m["agent_id"]: m["team"] for m in roster if m["team"] and mode == "live" and not m["house"]}
    units = sorted(set(unit_of.values()) - (bench if mode == "live" else set()))
    jurors, scribes, alts = _truth_seats(units, scope, sigs["call"], mode)
    refs = {e["ref"] for e in window}
    anchors = C.draw_anchors(window, scope, sigs["call"])
    card = C.draw_card(scope, sigs["call"], n, inp.get("recent"))
    w_write, w_open, w_pick, w_count = (C.windows(call, E)[k] for k in ("write", "open", "pick", "count"))

    seniority = {m["agent_id"]: (m["base_since"] if m.get("base_since") is not None else 0.0) for m in roster}

    def act(agent: str) -> tuple[Any, ...]:
        # the unit's agents rank by the age of their declared base operator, then the beacon on "act:" + agent id
        # (lowest first); siblings of one declared base are ordered by the beacon alone
        return (seniority.get(agent, 0.0), rand_game_rand(scope, "act:" + agent, sigs["call"]), agent)

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
                # the unit's highest-ranked sealer acts for it, whoever sealed first: the lowest draw on
                # "act:" + agent id from the call beacon (a claimed team: any teammate, the last seal stands)
                if not team_of.get(s["agent"]) and act(s["agent"]) > act(out[u]["agent"]):
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

def receipt_hash(chapter: str, call_ts: float, verdict: dict[str, Any], roster: str | None = None) -> str:
    """The hash the log carries for a frozen chapter. A receipt frozen with its roster (`receipt["roster"]`) also
    binds the hash of the published members and the events' operators; one frozen before rosters were snapshotted does not."""
    parts = {"chapter": chapter, "call_ts": call_ts, "verdict": verdict}
    if roster is not None:
        parts["roster"] = roster
    body = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(body.encode()).hexdigest()


#: A join's signed time is within ten minutes of the relay's clock when the relay stamps it (`joined_at`).
JOIN_SLACK_S = 660


def check_roster(inp: dict[str, Any], signed: dict[str, Any]) -> list[str]:
    """The published roster (`inputs["members"]`, the snapshot taken at the call) against the signed joins the
    relay publishes with it: each non-house member has joined and not left (the last signed record of its agent is
    `on`), is no agent twice, and joined within the relay's clock slack of a signed `on` record. Unit tokens are opaque
    and stay the relay's word, but a member that no signed record supports, or whose times disagree with the records,
    is named. (The signatures themselves are `check_signatures`.)"""
    bad: list[str] = []
    seen: set[str] = set()
    joins: dict[str, list[dict[str, Any]]] = {}
    for r in signed.get("join", []):
        joins.setdefault(r["agent"], []).append(r)
    for m in inp["members"]:
        a = m["agent_id"]
        if a in seen:
            bad.append(f"roster: {a} is listed twice")
        seen.add(a)
        if m["house"]:
            continue
        rows = joins.get(a, [])
        if not rows or not rows[-1]["on"]:
            bad.append(f"roster: {a} is a member with no standing signed join")
            continue
        stamps = [t.timestamp() for r in rows if r["on"] and (t := wire.when_of(r["at"]))]
        if stamps and min(abs(t - m["joined_at"]) for t in stamps) > JOIN_SLACK_S:
            bad.append(f"roster: {a} joined at {m['joined_at']}, which no signed join of it is near")
    return bad


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


def check_log_order(get: Callable[[str], dict], inp: dict[str, Any]) -> list[str]:
    """The relay stamps `received_at`, so a relay's own agent could be stamped inside a window after it saw others'
    opens. The log fixes the order the relay took the records in: every seal counted for the chapter must be filed
    before the first reveal of its kind (passage seals before passage reveals, pick seals before ballots), and
    `received_at` must not decrease along the log. Each row's log index is confirmed against the log itself."""
    rows: list[tuple[int, str, dict[str, Any]]] = []
    bad: list[str] = []
    for kind, key in (("chronicle_seal", "seals"), ("chronicle_reveal", "reveals"), ("chronicle_ballot", "ballots")):
        for r in inp.get(key, []):
            idx = r.get("log_index")
            if idx is None or r.get("seq") is None:
                bad.append(f"{kind} {r.get('seq')}: no place in the transparency log")
                continue
            got = get(f"/log/entries?ref={kind}:{r['seq']}&limit=1").get("entries") or []     # {} when the log is off
            rec = got[0]["record"] if got else {}
            if not got or got[0]["index"] != idx or rec.get("kind") != kind or rec.get("received_at") != r["at"]:
                bad.append(f"{kind} {r['seq']}: the log does not hold it at index {idx} with that receipt time")
                continue
            rows.append((idx, kind, r))
    if bad:
        return bad
    first_reveal = {"passage": min((i for i, k, _ in rows if k == "chronicle_reveal"), default=None),
                    "pick": min((i for i, k, _ in rows if k == "chronicle_ballot"), default=None)}
    for idx, kind, r in rows:
        if kind == "chronicle_seal" and first_reveal.get(r["what"]) is not None and idx > first_reveal[r["what"]]:
            bad.append(f"seal {r['seq']} ({r['what']}) by {r['agent']} entered the log after the first {r['what']} "
                       f"reveal: it may have been stamped after others opened")
    last = None
    for _idx, kind, r in sorted(rows, key=lambda x: x[0]):
        if last is not None and r["at"] < last:
            bad.append(f"{kind} {r['seq']}: received_at {r['at']} is earlier than a record filed before it")
        last = r["at"] if last is None else max(last, r["at"])
    return bad


def check_receipt_in_log(get: Callable[[str], dict], chapter: str, receipt: dict[str, Any],
                         inp: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The receipt is the relay's word until the transparency log's own record of the frozen verdict says the same:
    `/log/entries?ref=chronicle_verdict:<chapter>` holds the row (its `body_hash`). Returns (problems, notes). A relay
    with its log off has no such entry and, with nothing else of the chapter logged either, is noted, not failed."""
    got = get(f"/log/entries?ref=chronicle_verdict:{chapter}&limit=1").get("entries") or []
    if got:
        rec = got[0].get("record") or {}
        if rec.get("body_hash") != receipt.get("hash") or str(rec.get("chapter")) != str(chapter):
            return [f"the log's frozen verdict of chapter {chapter} is not the receipt the relay serves"], []
        return [], []
    logged = any(r.get("log_index") is not None for k in ("seals", "reveals", "ballots") for r in inp.get(k, []))
    if logged:
        return [f"the log holds no frozen verdict for chapter {chapter}, which the relay serves a receipt for"], []
    return [], [f"chapter {chapter}: the relay has no transparency log, so its receipt is the relay's word"]


def roster_notes(chapter: str, inp: dict[str, Any], receipt: dict[str, Any] | None) -> list[str]:
    """What a reader should know about how far a chapter's roster is bound."""
    out = []
    if inp.get("roster") in ("late", "current"):
        out.append(f"chapter {chapter}: roster {inp['roster']} (taken after the draw may have been public, or from the "
                   f"present grouping): its units are the relay's word")
    if receipt is not None and not receipt.get("roster"):
        out.append(f"chapter {chapter}: the receipt does not bind the roster (frozen before rosters were snapshotted)")
    return out


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
        if inp["rounds"] != C.call_rounds(inp["call_ts"], inp["params"]["epoch_s"]):
            problems.append("the rounds drawn from are not the first ones published after the call and the window closes")
        if bls:
            for name, rnd in inp["rounds"].items():
                if not beacon.verify(rnd, inp["sigs"][name]):
                    problems.append(f"beacon round {rnd} ({name}): not drand's signature")
        elif any(not inp["sigs"].get(k) for k in ("call", "show", "count")):
            problems.append("a beacon round the chapter drew from is missing")
        problems += check_log_order(get, inp)
        problems += check_signatures(body.get("signed", {}), state["relay_id"], state.get("post_origin", ""))
        problems += check_roster(inp, body.get("signed", {}))
        notes = roster_notes(row["id"], inp, body.get("receipt"))
        if body.get("receipt"):
            bad_log, log_notes = check_receipt_in_log(get, row["id"], body["receipt"], inp)
            problems += bad_log
            notes += log_notes
            h = receipt_hash(row["id"], inp["call_ts"], v,
                             L.roster_hash(inp["members"], inp["events"]) if body["receipt"].get("roster") else None)
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
                        "thin_jury": bool(v.get("thin_jury")), "mode": mode, "notes": notes})
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
        if r.status_code == 404 and path.startswith("/log/"):
            return {}                  # a relay with its log off: the check then reports the rows as unlogged
        r.raise_for_status()
        return r.json()
    rep = verify(get, bls=not args.no_bls, only=args.chapter)
    for c in rep["chapters"]:
        print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['label']}: {c['reason']}"
              f"{', canon ' + c['canon'] if c['canon'] else ''}{' (thin jury)' if c['thin_jury'] else ''}")
        for p in c["problems"]:
            print(f"     - {p}")
        for n in c.get("notes", []):
            print(f"     note: {n}")
    print("every chapter agrees" if rep["ok"] else "DISAGREEMENT")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
