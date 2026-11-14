"""The Chronicle's chapters in sequence, from the records a relay stores (T1). Pure: no I/O, no clock.

`modules/chronicle.py` resolves one chapter from its inputs. A relay has to feed it, chapter after chapter, what each
needs from the ones before: the number, the reply target, the events a quiet or failed day carries over, the units
benched for missing twice. This module does that, and it is the only place that does, so the relay's routes and
`scripts/verify_chronicle.py` run the same code.

A **ledger** is plain JSON-able data (the relay builds it from its tables; a verifier reads the same fields from
the public routes):

    relay_id, start, epoch_s, practice, prologue        the clock and the book's first post
    agents   [{agent_id, unit, registered_at, house, joins: [[on, ts], ...]}]    `unit` is an opaque token
    events   [{ref, line, operators, at}]                the public record, as `chronicle.record_window` reads it
    beacons  {"<round>": "<hex signature>"}              every drand round the chapters drew from
    seals, reveals, ballots                              each with its `chapter` id ("7" or "pp-2") and `at`

Chapter `i` is called at `start + i * epoch_s` (so a chapter is called as the one before it is counted: a slot is
a day, a quiet or empty slot simply uses no number). `slots` returns one dict per slot up to `now`; a slot that was
called has `inputs` (everything `resolve_inputs` needs, serialisable: this is what a chapter's public page carries)
and the `verdict`.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from emporia.modules import chronicle as C

#: Events a quiet or failed chapter hands to the next one are kept this many epochs, so the carry cannot grow for ever.
CARRY_EPOCHS = 3
#: Reasons that end a called chapter without canon: its window carries over (2.7).
CARRIES = ("quorum", "none_won", "no_candidates")


def unit_token(relay_id: str, key: str) -> str:
    """A voting unit as the public sees it: opaque, stable on this relay, never the operator's label."""
    return "u" + hashlib.sha256(f"chronicle-unit:{relay_id}:{key}".encode()).hexdigest()[:10]


def slot_call(start: float, epoch_s: int, i: int) -> float:
    return start + i * epoch_s


def slot_at(start: float, epoch_s: int, now: float) -> int:
    """The slot `now` falls in; -1 before `start`."""
    return int((now - start) // epoch_s) if now >= start else -1


def params_of(ledger: dict[str, Any]) -> C.Params:
    return C.Params(epoch_s=int(ledger["epoch_s"]), practice=bool(ledger.get("practice", True)))


def members_at(agents: list[dict[str, Any]], before_ts: float) -> list[C.Member]:
    """The member agents as of `before_ts` (the previous chapter's call): an agent is a member if its latest join
    record before then is `on`, and it joined when that stretch of `on` records began."""
    out = []
    for a in agents:
        since: float | None = None
        for on, ts in sorted(a["joins"], key=lambda e: e[1]):
            if ts >= before_ts:
                break
            if on and since is None:
                since = ts
            elif not on:
                since = None
        if since is not None:
            out.append(C.Member(a["agent_id"], a["unit"], float(a["registered_at"]), float(since), "", bool(a["house"])))
    return out


def _member_dict(m: C.Member) -> dict[str, Any]:
    return asdict(m)


def _record_rows(rows: list[dict[str, Any]], chapter: str, drop: tuple[str, ...] = ("chapter",)) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k not in drop} for r in rows if r.get("chapter") == chapter]


def resolve_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """The verdict of one chapter from its `inputs`: what the relay serves with a chapter and what a verifier
    recomputes. `sigs` are hex strings; an empty one is a round not yet published (the verdict is then provisional)."""
    members = [C.Member(**m) for m in inputs["members"]]
    sigs = {k: bytes.fromhex(v) if v else b"" for k, v in inputs["sigs"].items()}
    params = C.Params(epoch_s=int(inputs["params"]["epoch_s"]), practice=bool(inputs["params"]["practice"]))
    v = C.resolve_chapter(
        relay_id=inputs["relay_id"], numbering=inputs["numbering"], members=members,
        prev_call_ts=inputs["prev_call_ts"], call_ts=inputs["call_ts"], events=inputs["events"],
        carry=inputs.get("carry", []), sigs=sigs, seals=inputs["seals"], reveals=inputs["reveals"],
        ballots=inputs["ballots"], params=params, bench=frozenset(inputs.get("bench", [])))
    v["provisional"] = not all(inputs["sigs"].get(k) for k in ("call", "show", "count"))
    return v


def _canon_event(slot: dict[str, Any], at: float) -> dict[str, Any] | None:
    v = slot.get("verdict") or {}
    if not v.get("canon"):
        return None
    handle = slot.get("canon_agent") or ""
    return {"ref": "post:" + v["canon"],
            "line": C.clip_line(f"{v['label']} canon{' by ' + handle if handle else ''}, "
                                f"{v['picks_counted']} pick{'s' if v['picks_counted'] != 1 else ''}"),
            "operators": [v["authors"][v["canon"]]], "at": at}


def slots(ledger: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """Every slot from `start` to the one `now` falls in, in order. See the module docstring."""
    start, epoch = float(ledger["start"]), int(ledger["epoch_s"])
    params = params_of(ledger)
    last = slot_at(start, epoch, now)
    out: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []         # called pages: {"mode", "canon"}
    live_outcomes: list[dict[str, str]] = []
    state = C.new_state()
    carry: list[dict[str, Any]] = []
    prev_slot: dict[str, Any] | None = None
    handle_of_post = {r["post_id"]: r["agent"] for r in ledger["reveals"]}
    for i in range(last + 1):
        call_ts = slot_call(start, epoch, i)
        prev_call = call_ts - epoch
        members = members_at(ledger["agents"], prev_call)
        bench = C.benched(live_outcomes, len(live_outcomes))
        roster = C.eligible(members, prev_call)
        mode = C.mode_of(roster, params, bench)
        numbering = C.next_numbering(history, mode or "live")
        if numbering["reply_to"] == C.PROLOGUE:
            numbering["reply_to"] = ledger["prologue"] or C.PROLOGUE
        if numbering["root"] == C.PROLOGUE:
            numbering["root"] = ledger["prologue"] or C.PROLOGUE
        cid = str(numbering["n"]) if mode == "live" else f"pp-{numbering['n']}"
        evs = [e for e in ledger["events"] if prev_call <= e["at"] < call_ts and (mode == "practice" or not e.get("loose"))]
        if prev_slot is not None and (ce := _canon_event(prev_slot, call_ts - 1)):
            evs.append(ce)
        rounds = C.call_rounds(call_ts, epoch)
        sigs = {k: ledger["beacons"].get(str(r), "") for k, r in rounds.items()}
        inputs = {
            "relay_id": ledger["relay_id"], "numbering": numbering, "members": [_member_dict(m) for m in members],
            "prev_call_ts": prev_call, "call_ts": call_ts, "events": evs, "carry": carry,
            "sigs": sigs, "rounds": rounds,
            "seals": _record_rows(ledger["seals"], cid), "reveals": _record_rows(ledger["reveals"], cid),
            "ballots": _record_rows(ledger["ballots"], cid),
            "params": {"epoch_s": epoch, "practice": params.practice}, "bench": sorted(bench)}
        v = resolve_inputs(inputs) if sigs["call"] else {"mode": None, "reason": "no_beacon", "refused": []}
        called = v.get("mode") is not None and v.get("reason") not in ("quiet", "no_roster", "no_beacon")
        slot: dict[str, Any] = {"slot": i, "call_ts": call_ts, "windows": C.windows(call_ts, epoch),
                                "mode": v.get("mode"), "reason": v.get("reason"), "called": called,
                                "id": cid if called else None, "inputs": inputs if called else None, "verdict": v}
        if called:
            canon = v.get("canon")
            slot["canon_agent"] = handle_of_post.get(canon, "") if canon else ""
            history.append({"mode": v["mode"], "canon": ("post:" + canon) if canon else None})
            if v["mode"] == "live":
                live_outcomes.append(v.get("outcome") or {})
            if not v["provisional"]:
                awards, state = C.award(v, state)
                slot["awards"] = awards
            else:
                slot["awards"] = {}
        # what the next slot carries
        window = C.record_window(evs, prev_call, call_ts, carry)
        if not called or v.get("reason") in CARRIES:
            carry = [e for e in window if e["at"] >= call_ts - CARRY_EPOCHS * epoch]
        else:
            carry = []
        out.append(slot)
        prev_slot = slot if called else None
    return out
