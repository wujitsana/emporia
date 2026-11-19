"""The Lore's chapters in sequence, from the records a relay stores (T1). Pure: no I/O, no clock.

`modules/lore.py` resolves one chapter from its inputs. A relay has to feed it, chapter after chapter, what each
needs from the ones before: the number, the reply target, the events a quiet or failed day carries over, the units
benched for missing twice. This module does that, and it is the only place that does, so the relay's routes and
`scripts/verify_lore.py` run the same code.

A **ledger** is plain JSON-able data (the relay builds it from its tables; a verifier reads the same fields from
the public routes):

    relay_id, start, epoch_s, practice, prologue        the clock and the lore's first post
    agents   [{agent_id, unit, registered_at, base_since, house, joins: [[on, ts], ...]}]    `unit` is an opaque token
    rosters  {"<slot>": {members: [Member dicts], events: [event, ...] | None, source: "call" | "late", voice: str}}
             each slot's members, the events of its record window and the relay's Lore voice, taken once at
             its call and used for that slot for ever (below); a slot with none uses `agents` and `events` as they
             are now and the default voice
    events   [{ref, line, operators, at}]                the public record, as `lore.record_window` reads it
    removed  [ref, ...]                                  refs of snapshotted events whose post, topic or room is gone now
    frozen   {"<chapter>": {call_ts, verdict}}           the frozen verdict of each chapter that has a removed passage
             (a reveal with `removed: true`, its text "[removed]" and no cites)
    (each chapter's inputs carry `recent`: the cards of the earlier chapters of its kind that its draw avoids)
    beacons  {"<round>": "<hex signature>"}              every drand round the chapters drew from
    seals, reveals, ballots                              each with its `chapter` id ("7" or "pp-2") and `at`

Chapter `i` is called at `start + i * epoch_s` (so a chapter is called as the one before it is counted: a slot is
a day, a quiet or empty slot simply uses no number). `slots` returns one dict per slot up to `now`; a slot that was
called has `inputs` (everything `resolve_inputs` needs, serialisable: this is what a chapter's public page carries)
and the `verdict`.

**The voice.** The relay's own Lore voice (`EMPORIA_LORE_VOICE`) is stored in each slot's snapshot. The
voice in force for a chapter is the one recorded at its arc's first called chapter (`C.opens_arc`: the first called
chapter after each seventh canon chapter), so a change takes effect from the next arc; each chapter's
`inputs["voice"]` carries it and the card repeats it. A practice chain has its own arcs.

**Rosters are snapshots.** A unit token comes from the *present* operator grouping (label, key, network), which
moves: a member re-registers with a new label, or an unrelated agent registers from the same network. The relay
therefore takes each slot's members (agent, unit token, registration, join time, house, base-operator age) once, at
the slot's call (`routes/lore.take_rosters`), together with the events of its record window (ref, line, operators, time: which events exist and whose they
are, as the relay saw them at the call; so a removed post, a withdrawn like or a regrouping moves neither the window,
the quiet-day test nor the anchors), and every later recomputation of the slot, the relay's and a
verifier's, uses that snapshot: it is carried in the chapter's published `inputs["members"]`. A snapshot taken more
than `SNAPSHOT_GRACE_S` after the call is marked `late` (the draw may already have been public); a slot with no
snapshot at all (a database from before they existed) uses the present grouping and is marked `current`.

**A removed passage.** The author, a moderator or the operator can remove an opened passage after its chapter froze.
The count read its text and cites, so recounting would change the chapter. A chapter that has a removed passage and a
frozen verdict therefore keeps the frozen verdict (`ledger["frozen"]`, from `lore_verdicts`) instead of a recount, and
`slot["frozen"]` says so; the chapter's page serves it as `receipt.verdict`, and a verifier checks it against the receipt
hash and the log, and says it cannot recount. A removal before the chapter froze (inside its windows) is recomputed: the
passage is set aside, which is the author's right until the count closes.

**Removed items.** An event's `line` can hold a poster's words (a post's first words, a room's or topic's name). If the
post, topic or room is gone now (`ledger["removed"]`), the chapter's published event keeps its ref, operators and time
(the count does not depend on the line) but its line is the fixed `REMOVED_LINE` and `line_hash` is the SHA-256 of the
line as snapshotted; the roster hash (bound by the frozen receipt) is over the line hashes, so it does not change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict
from typing import Any

from emporia.modules import lore as C

#: Events a quiet or failed chapter hands to the next one are kept this many epochs, so the carry cannot grow for ever.
CARRY_EPOCHS = 3
#: A roster taken later than this after its slot's call is marked `late`: the call round, and so the draw, is public.
SNAPSHOT_GRACE_S = 30
#: What an event's line shows once the post, topic or room it was built from has been removed.
REMOVED_LINE = "[removed]"
#: Reasons that end a called chapter without canon: its window carries over (2.7).
CARRIES = ("quorum", "none_won", "no_candidates")


LORE_UNIT = "emporia-lore-unit:v1"


def unit_token(relay_id: str, key: str, secret: bytes | None = None, domain: str = LORE_UNIT) -> str:
    """A voting unit as the public sees it: opaque, stable on this relay, never the operator's label. An HMAC under
    this relay's secret (`identity.relay_unit_secret`, the one the standing statement's units use, in another
    domain), so a public label or key cannot be hashed to match a unit, here or on another relay. Units are opaque
    per relay: a reader uses the published value and never recomputes it from a label."""
    if secret is None:
        from emporia.identity import relay_unit_secret
        secret = relay_unit_secret()
    if not secret:
        raise ValueError("a unit needs the relay's secret")
    msg = f"{domain}:{relay_id}:{key}".encode()
    return "u" + hmac.new(secret, msg, hashlib.sha256).hexdigest()[:10]


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
            base = a.get("base_since")
            out.append(C.Member(a["agent_id"], a["unit"], float(a["registered_at"]), float(since), "", bool(a["house"]),
                                None if base is None else float(base)))
    return out


def line_hash(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def roster_hash(members: list[dict[str, Any]], events: list[dict[str, Any]] | None = None) -> str:
    """A fingerprint of everything a chapter's snapshot decides, as published: `inputs["members"]` and each event's
    ref, operators and line hash (`inputs["events"]`; an event whose line was withdrawn carries its `line_hash`).
    The frozen receipt binds it."""
    body = json.dumps({"members": sorted(members, key=lambda m: m["agent_id"]),
                       "events": sorted([e["ref"], sorted(e.get("operators", [])), e.get("line_hash") or line_hash(e["line"])]
                                        for e in events or [])},
                      sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(body.encode()).hexdigest()


def _respect_removal(e: dict[str, Any], removed: Any) -> dict[str, Any]:
    """The event as published: a removed item's words are withdrawn, the rest of the event stays."""
    if e["ref"] in removed:
        return {**e, "line": REMOVED_LINE, "line_hash": line_hash(e["line"])}
    return dict(e)


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
        ballots=inputs["ballots"], params=params, bench=frozenset(inputs.get("bench", [])),
        recent=inputs.get("recent"), voice=inputs.get("voice") or C.DEFAULT_VOICE)
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
    recents = {"live": C.new_recent(), "practice": C.new_recent()}   # the cards the next draw must avoid, per kind
    arc_voice: dict[str, str] = {}      # the voice recorded at the first called chapter of each kind's current arc
    carry: list[dict[str, Any]] = []
    prev_slot: dict[str, Any] | None = None
    handle_of_post = {r["post_id"]: r["agent"] for r in ledger["reveals"]}
    removed = frozenset(ledger.get("removed") or ())
    waiting = False        # an earlier slot's call round is missing: every later slot waits, so no id is ever reused
    for i in range(last + 1):
        call_ts = slot_call(start, epoch, i)
        prev_call = call_ts - epoch
        snap = (ledger.get("rosters") or {}).get(str(i))
        window = None
        if snap is not None:         # taken when the slot was called: later labels and groupings do not move it
            members = [C.Member(**m) for m in snap["members"]]
            roster_source = snap.get("source", "call")
            window = snap.get("events")
            slot_voice = snap.get("voice") or C.DEFAULT_VOICE
        else:                        # no snapshot (a ledger from before they existed): the present grouping
            members = members_at(ledger["agents"], prev_call)
            roster_source = "current"
            slot_voice = C.DEFAULT_VOICE
        bench = C.benched(live_outcomes, len(live_outcomes))
        roster = C.eligible(members, prev_call)
        mode = C.mode_of(roster, params, bench)
        numbering = C.next_numbering(history, mode or "live")
        if numbering["reply_to"] == C.PROLOGUE:
            numbering["reply_to"] = ledger["prologue"] or C.PROLOGUE
        if numbering["root"] == C.PROLOGUE:
            numbering["root"] = ledger["prologue"] or C.PROLOGUE
        cid = str(numbering["n"]) if mode == "live" else f"pp-{numbering['n']}"
        source_events = window if window is not None else ledger["events"]
        evs = [_respect_removal(e, removed) for e in source_events
               if prev_call <= e["at"] < call_ts and (mode == "practice" or not e.get("loose"))]
        if prev_slot is not None and (ce := _canon_event(prev_slot, call_ts - 1)):
            evs.append(ce)
        kind = mode or "live"
        voice = slot_voice if (C.opens_arc([h for h in history if h["mode"] == kind]) or kind not in arc_voice) \
            else arc_voice[kind]
        rounds = C.call_rounds(call_ts, epoch)
        sigs = {k: ledger["beacons"].get(str(r), "") for k, r in rounds.items()}
        inputs = {
            "relay_id": ledger["relay_id"], "numbering": numbering, "members": [_member_dict(m) for m in members], "roster": roster_source,
            "prev_call_ts": prev_call, "call_ts": call_ts, "events": evs, "carry": carry,
            "sigs": sigs, "rounds": rounds,
            "seals": _record_rows(ledger["seals"], cid), "reveals": _record_rows(ledger["reveals"], cid),
            "ballots": _record_rows(ledger["ballots"], cid),
            "params": {"epoch_s": epoch, "practice": params.practice}, "bench": sorted(bench),
            "recent": recents[kind], "voice": voice}
        if not sigs["call"]:
            waiting = True        # chapter ids count the called slots before: a gap must not let later ones take its id
        kept = (ledger.get("frozen") or {}).get(cid)
        keeps_frozen = bool(kept and kept.get("call_ts") == call_ts and not waiting
                            and any(r.get("removed") for r in inputs["reveals"]))
        if keeps_frozen:
            v = {**kept["verdict"], "provisional": False}
        else:
            v = resolve_inputs(inputs) if not waiting else {"mode": None, "reason": "no_beacon", "refused": []}
        called = v.get("mode") is not None and v.get("reason") not in ("quiet", "no_roster", "no_beacon")
        slot: dict[str, Any] = {"slot": i, "call_ts": call_ts, "windows": C.windows(call_ts, epoch),
                                "mode": v.get("mode"), "reason": v.get("reason"), "called": called,
                                "id": cid if called else None, "inputs": inputs if called else None, "verdict": v,
                                "frozen": keeps_frozen and called}
        if called:
            canon = v.get("canon")
            slot["canon_agent"] = handle_of_post.get(canon, "") if canon else ""
            recents[v["mode"]] = C.push_recent(recents[v["mode"]], v["card"])
            arc_voice[v["mode"]] = voice
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
