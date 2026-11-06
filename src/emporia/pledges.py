"""Pledges: a signed, public promise to a counterparty, and its track record (gamify `pledges`).

`docs/proposals/gamify-pledges.md`. Talk is free and word is scored: table talk may
bluff, but a pledge is a promise the maker signs (`wire.signed_pledge_payload`), tied
to a session (a game or a deal) or a listing, to one counterparty. What a pledge may
promise is a **fixed, published list** (`KINDS`), so nothing vague counts:

- `deliver_by` — the seller delivers the deal by a time (`{"by": ISO}`);
- `respond_within` — the buyer confirms or disputes within N seconds of delivery;
- `reveal_within` — the player reveals within N seconds of its reveal turn opening,
  in every round of the game from the pledge on;
- `tell` — a declared move in a commit-reveal game: `even_split` (blotto),
  `pick_below` / `pick_above` a number (pick games), or the exact `move`;
- `refund` — the seller accepts a refund if the buyer is not satisfied.

**Resolved by evidence where the relay can check it**: the deadline against the
deal's delivery or response time, the reveal against when its turn opened, the
declared move against the revealed one. **Otherwise by the counterparty**, which
attests `kept` or `broken`, signed (`wire.signed_pledge_act_payload`); the maker may
contest a `broken`, and the pledge then shows as `disputed`. A pledge whose subject
ended without testing it (a game over before a reveal, a listing nobody bought) is
`void` and counts nothing.

Computed, never issued: `resolve()` reads the subject's record and the signed acts,
so anyone holding the same records gets the same status.

Against farming (each a stop, the proposal's farm table):

- **To yourself:** refused. **To your own operator's agents, or with the house:**
  shown, and counted nothing.
- **A real counterparty in a real session:** the counterparty must be in the game or
  the deal (a listing's pledge waits for a deal with it), and a subject that never
  started tests nothing.
- **Made before it is known:** only reveal turns that open after the pledge, moves
  committed after it, and deliveries or responses still to come count.
- **One voice:** one attestation per pledge, by its counterparty, final; in Trust a
  counterparty operator's nth kept pledge with the same maker weighs 1/n, attested
  "kept" swapped between two operators weighs nothing, and an attestation weighs
  what its giver stands for (verified, 30 days in).

**Season-0 shows the record only** (kept / broken / disputed, with recency). The
Trust weighting is built behind `EMPORIA_PLEDGES_TRUST` (default off); `trust_points`
is its formula, `pledges:v1/trust`.

Pure: records in, statuses out, no I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from emporia.calls import ALL_SET_PLAYS, SET_PLAYS as LIVE_SET_PLAYS

FORMULA = "pledges:v1"
TRUST_FORMULA = "pledges:v1/trust"

DEALS = ("emporia:service:v1",)
# gamify-all: the commit-reveal set plays are :v2 (calls); a :v1 game still running keeps its
# pledges too. Bargain is no commit-reveal game, so it takes no reveal or tell pledge.
COMMIT_REVEAL = ALL_SET_PLAYS
BLOTTO = ("emporia:blotto:v2", "emporia:blotto:v1")
PICK_GAMES = ("emporia:two-thirds:v2", "emporia:lowest-unique:v2", "emporia:two-thirds:v1", "emporia:lowest-unique:v1")
TELLS = ("even_split", "pick_below", "pick_above", "move")
STATUSES = ("open", "kept", "broken", "disputed", "void")
VERDICTS = ("kept", "broken")
ACTS = ("attest", "contest")

MAX_NOTE = 200
MAX_DAYS = 30                       # a deadline at most this far ahead
MIN_SECONDS, MAX_SECONDS = 10, 12 * 3600
ATTEST_DAYS = 7                     # the counterparty attests within this long of the subject's end
CONTEST_DAYS = 7                    # the maker contests within this long of the attestation
RECENT = 10                         # resolved pledges shown on the sheet
RECENT_DAYS = 30
DAY_CAP = 20                        # pledges one agent may make in a day

# Trust, behind EMPORIA_PLEDGES_TRUST (off in season-0). A broken pledge costs twice
# what a kept one earns, and is not flattened; gains are.
KEPT_EVIDENCE = 0.5
KEPT_ATTESTED = 0.25
BROKEN = -1.0
DISPUTED = -0.25


@dataclass(frozen=True)
class Kind:
    name: str
    about: str
    maker: str            # seller | buyer | player
    subjects: tuple[str, ...]
    evidence: bool        # the relay's record decides it; else the counterparty attests


KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind("deliver_by", "deliver the deal by a time: terms {\"by\": ISO time}", "seller", ("session", "listing"), True),
    Kind("respond_within", "confirm or dispute within N seconds of delivery: terms {\"seconds\": N}", "buyer",
         ("session",), True),
    Kind("reveal_within", "reveal within N seconds of your reveal turn opening, every round from now: terms "
         "{\"seconds\": N}", "player", ("session",), True),
    Kind("tell", "a declared move: terms {\"tell\": \"even_split\"} (blotto), {\"tell\": \"pick_below\"|\"pick_above\", "
         "\"value\": N} (pick games) or {\"tell\": \"move\", \"value\": <move>}", "player", ("session",), True),
    Kind("refund", "accept a refund if the buyer is not satisfied; the buyer attests: terms {}", "seller",
         ("session", "listing"), False),
)}


def trust_on() -> bool:
    """Whether kept and broken pledges move Trust on this relay (EMPORIA_PLEDGES_TRUST; off)."""
    return os.getenv("EMPORIA_PLEDGES_TRUST", "").strip().lower() in ("1", "on", "true", "yes")


def pledge_id_for(maker: str, nonce: str) -> str:
    """A pledge's id, from its maker and the nonce it picked, so the maker signs before it exists."""
    return "plg_" + hashlib.sha256(f"emporia:pledge-id:{maker}:{nonce}".encode()).hexdigest()[:16]


def _ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _canonical(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


# ── making one ──────────────────────────────────────────────────────────────

def check_terms(kind: str, terms: Mapping[str, Any], module_type: str, now: float) -> str:
    """'' when `terms` are well-formed for `kind` in a subject of `module_type`, else why not."""
    k = KINDS.get(kind)
    if k is None:
        return f"kind: one of {', '.join(KINDS)}"
    if not isinstance(terms, Mapping):
        return "terms: an object"
    allowed = {"deliver_by": {"by"}, "respond_within": {"seconds"}, "reveal_within": {"seconds"},
               "tell": {"tell", "value"}, "refund": set()}[kind]
    if extra := set(terms) - allowed:
        return f"terms: {kind} takes {', '.join(sorted(allowed)) or 'nothing'}, not {', '.join(sorted(extra))}"
    if kind == "deliver_by":
        by = _ts(terms.get("by"))
        if by is None:
            return "terms.by: an ISO 8601 time"
        if by <= now or by > now + MAX_DAYS * 86_400:
            return f"terms.by: a time in the next {MAX_DAYS} days"
    if kind in ("respond_within", "reveal_within"):
        s = terms.get("seconds")
        if isinstance(s, bool) or not isinstance(s, int) or not MIN_SECONDS <= s <= MAX_SECONDS:
            return f"terms.seconds: a whole number, {MIN_SECONDS}-{MAX_SECONDS}"
    if kind in ("reveal_within", "tell") and module_type not in COMMIT_REVEAL:
        return f"{kind}: only in a commit-reveal game ({', '.join(LIVE_SET_PLAYS)})"
    if kind == "tell":
        t, v = terms.get("tell"), terms.get("value")
        if t not in TELLS:
            return f"terms.tell: one of {', '.join(TELLS)}"
        if t == "even_split" and (module_type not in BLOTTO or "value" in terms):
            return "even_split: blotto only, and takes no value"
        if t in ("pick_below", "pick_above") and (module_type not in PICK_GAMES or isinstance(v, bool)
                                                   or not isinstance(v, int)):
            return f"{t}: pick games only, with a whole-number value"
        if t == "move" and v is None:
            return "move: the value is the move you declare"
    if kind in ("deliver_by", "respond_within", "refund") and module_type and module_type not in DEALS:
        return f"{kind}: only on a deal ({', '.join(DEALS)}) or a listing"
    return ""


def check_note(note: str) -> str:
    if len(note) > MAX_NOTE or any(ord(c) < 32 and c not in "\t" for c in note):
        return f"note: at most {MAX_NOTE} characters on one line"
    return ""


# ── resolving one ───────────────────────────────────────────────────────────

def _deal_times(deal: Mapping, actions: list[Mapping]) -> tuple[float | None, float | None, str]:
    """(delivered_at, first response by the buyer after delivery, the response's type)."""
    data = (deal.get("state") or {}).get("data") or deal.get("data") or {}
    delivered = _ts(data.get("delivered_at"))
    if delivered is None:
        delivered = next((_ts(a["created_at"]) for a in actions if a.get("action_type") == "deliver"), None)
    buyer = data.get("buyer")
    resp = next(((_ts(a["created_at"]), a["action_type"]) for a in actions
                 if a.get("agent_id") == buyer and a.get("action_type") in ("confirm", "dispute")
                 and delivered is not None and (_ts(a["created_at"]) or 0) >= delivered), (None, ""))
    return delivered, resp[0], resp[1]


def _abandoned_by(subject: Mapping) -> str | None:
    """The player who abandoned the session (the relay records it), or None."""
    by = ((subject.get("state") or {}).get("metadata") or {}).get("abandoned_by")
    return by if by in (subject.get("participants") or []) else None


def _ended(subject: Mapping) -> tuple[bool, float | None]:
    if subject.get("status") in ("completed", "abandoned"):
        return True, _ts(subject.get("updated_at"))
    return False, None


def _reveal_turns(subject: Mapping, actions: list[Mapping], maker: str) -> list[dict]:
    """Each reveal turn of `maker`: when it opened, and when it revealed (None: not yet).
    A turn opens at the commit that ended the commit phase (a pick game, any order), or at
    the action just before it (turn order: blotto reveals in join order)."""
    turn_order = bool(subject.get("turn_order", True))
    out: list[dict] = []
    last_commit: float | None = None
    prev: float | None = None
    for a in actions:
        t = _ts(a.get("created_at"))
        if a.get("action_type") == "reveal" and a.get("agent_id") == maker:
            opened = prev if turn_order else last_commit
            out.append({"opened": opened, "revealed": t, "move": (a.get("payload") or {}).get("move")})
        if a.get("action_type") == "commit":
            last_commit = t
        prev = t
    data = (subject.get("state") or {}).get("data") or subject.get("data") or {}
    if subject.get("status") != "waiting" and data.get("phase") == "reveal":      # its turn now, or when it ended
        pending = maker in (data.get("pending") or []) if not turn_order else subject.get("current_agent") == maker
        if pending:
            out.append({"opened": prev if turn_order else last_commit, "revealed": None, "move": None})
    return out


def _commits_after(actions: list[Mapping], maker: str, since: float) -> list[tuple[float, Any]]:
    """The maker's reveals whose commit came after `since`: (reveal time, move)."""
    out, committed = [], None
    for a in actions:
        if a.get("agent_id") != maker:
            continue
        t = _ts(a.get("created_at"))
        if a.get("action_type") == "commit":
            committed = t
        elif a.get("action_type") == "reveal" and committed is not None and committed > since:
            out.append((t, (a.get("payload") or {}).get("move")))
    return out


def tell_holds(terms: Mapping[str, Any], move: Any) -> bool:
    t, v = terms.get("tell"), terms.get("value")
    if t == "even_split":
        return isinstance(move, list) and bool(move) and all(isinstance(x, int) for x in move) \
            and max(move) - min(move) <= 1
    if t in ("pick_below", "pick_above"):
        if isinstance(move, bool) or not isinstance(move, int):
            return False
        return move < v if t == "pick_below" else move > v
    return _canonical(move) == _canonical(v)


def _by_attestation(p: Mapping, acts: Mapping[str, Mapping], end: float | None, now: float) -> dict:
    attest, contest = acts.get("attest"), acts.get("contest")
    if attest:
        at = _ts(attest.get("at"))
        if attest.get("verdict") == "broken" and contest:
            return {"status": "disputed", "resolved_by": "attestation", "resolved_at": contest.get("at"),
                    "evidence": {"attested": "broken", "contested_at": contest.get("at")}}
        return {"status": attest.get("verdict"), "resolved_by": "attestation", "resolved_at": attest.get("at"),
                "evidence": {"attested": attest.get("verdict")},
                **({"contest_by": _iso(at + CONTEST_DAYS * 86_400)} if attest.get("verdict") == "broken"
                   and at is not None and now <= at + CONTEST_DAYS * 86_400 else {})}
    if end is None:
        return {"status": "open", "waiting_on": "the deal's end, then the counterparty"}
    if now > end + ATTEST_DAYS * 86_400:
        return {"status": "void", "resolved_at": _iso(end + ATTEST_DAYS * 86_400),
                "evidence": {"why": "not attested in time"}}
    return {"status": "open", "waiting_on": "counterparty", "attest_by": _iso(end + ATTEST_DAYS * 86_400)}


def resolve(p: Mapping[str, Any], subject: Mapping[str, Any] | None, actions: Iterable[Mapping] = (),
            acts: Mapping[str, Mapping] | None = None, now: float | None = None) -> dict:
    """A pledge's status from its subject's record and its signed acts.

    p:       {pledge_id, maker_id, to_agent, kind, subject_type, terms, made_at}
    subject: the session it is on (for a listing, the deal with the counterparty that
             followed it, or None): {status, state:{data}, current_agent, participants,
             updated_at, turn_order}; for a listing with no deal yet, {"listing_expires_at"}.
    actions: the session's actions, oldest first: {agent_id, action_type, payload, created_at}
    acts:    {"attest": {verdict, at}, "contest": {at}}

    Returns {status, resolved_by?, resolved_at?, evidence?, due_at?, waiting_on?}."""
    now = datetime.now(UTC).timestamp() if now is None else now
    acts = acts or {}
    actions = sorted(actions, key=lambda a: str(a.get("created_at") or ""))
    kind, terms, maker = p["kind"], p.get("terms") or {}, p["maker_id"]
    # Review pass 2: "made" is when the relay took the pledge, never earlier. `made_at` is the
    # maker's signed `at`, which may lag the relay's clock by minutes (the signing window), so
    # a pledge dated back counted a reveal or a commit the maker had already made.
    made = max(_ts(p.get("made_at")) or 0.0, _ts(p.get("recorded_at")) or 0.0)
    session = subject if subject and subject.get("status") else None
    ended, end = _ended(session) if session else (False, None)
    if session is None:                       # a listing nobody bought from (yet)
        expires = _ts((subject or {}).get("listing_expires_at"))
        last = _ts(terms.get("by")) if kind == "deliver_by" else expires
        if (expires is not None and now > expires) or (last is not None and now > last):
            return {"status": "void", "resolved_at": _iso(min(t for t in (expires, last) if t is not None)),
                    "evidence": {"why": "no deal with the counterparty"}}
        return {"status": "open", "waiting_on": "a deal with the counterparty",
                **({"due_at": terms.get("by")} if kind == "deliver_by" else {})}
    started = session.get("status") != "waiting" and p.get("to_agent") in (session.get("participants") or [])
    if not started:
        if ended:
            return {"status": "void", "resolved_at": _iso(end), "evidence": {"why": "the session never started"}}
        return {"status": "open", "waiting_on": "the session to start"}

    if kind == "refund":
        return _by_attestation(p, acts, end if ended else None, now)

    if kind == "deliver_by":
        by = _ts(terms.get("by")) or 0.0
        delivered, _, _ = _deal_times(session, actions)
        if delivered is not None:
            ok = delivered <= by
            return {"status": "kept" if ok else "broken", "resolved_by": "evidence", "resolved_at": _iso(delivered),
                    "evidence": {"delivered_at": _iso(delivered), "by": _iso(by)}}
        data = (session.get("state") or {}).get("data") or {}
        # Review pass 2: calling the deal off voids the pledge only when the buyer did it
        # before the deadline; a seller who abandons, or a deal abandoned after `by`, broke it.
        if session.get("status") == "abandoned" and (end is None or end <= by) \
                and _abandoned_by(session) != maker:
            return {"status": "void", "resolved_at": _iso(end), "evidence": {"why": "the deal was called off"}}
        if ended or now > by:
            when = min(t for t in (end, by) if t is not None) if ended else by
            return {"status": "broken", "resolved_by": "evidence", "resolved_at": _iso(when),
                    "evidence": {"delivered_at": None, "by": _iso(by), "deal": data.get("status")}}
        return {"status": "open", "due_at": _iso(by), "waiting_on": "maker", "do": "deliver"}

    if kind == "respond_within":
        secs = int(terms.get("seconds") or 0)
        delivered, answered, how = _deal_times(session, actions)
        if delivered is None:
            if ended:
                return {"status": "void", "resolved_at": _iso(end), "evidence": {"why": "nothing was delivered"}}
            return {"status": "open", "waiting_on": "the seller's delivery"}
        due = delivered + secs
        if answered is not None:
            return {"status": "kept" if answered <= due else "broken", "resolved_by": "evidence",
                    "resolved_at": _iso(answered),
                    "evidence": {"delivered_at": _iso(delivered), "answered_at": _iso(answered), "answer": how,
                                 "within_s": secs}}
        if now > due or ended:
            return {"status": "broken", "resolved_by": "evidence", "resolved_at": _iso(due),
                    "evidence": {"delivered_at": _iso(delivered), "answered_at": None, "within_s": secs}}
        return {"status": "open", "due_at": _iso(due), "waiting_on": "maker", "do": "respond"}

    if kind == "reveal_within":
        secs = int(terms.get("seconds") or 0)
        turns = [t for t in _reveal_turns(session, actions, maker) if t["opened"] is not None
                 and t["opened"] > made]
        # Review pass 2: a turn still open when the game ended (lost on time, or the maker
        # walked away) is a reveal that never came — broken, not void, whatever the deadline.
        # Only another player abandoning the game excuses it.
        excused = session.get("status") == "abandoned" and _abandoned_by(session) not in (None, maker)
        for t in turns:
            due = t["opened"] + secs
            if t["revealed"] is not None:
                late = t["revealed"] > due
            else:
                late = (ended and not excused) or (not ended and now > due)
            if late:
                return {"status": "broken", "resolved_by": "evidence", "resolved_at": _iso(due),
                        "evidence": {"turn_opened_at": _iso(t["opened"]), "revealed_at": _iso(t["revealed"]),
                                     "within_s": secs}}
        pending = next((t for t in turns if t["revealed"] is None), None)
        if pending and not ended:
            return {"status": "open", "due_at": _iso(pending["opened"] + secs), "waiting_on": "maker", "do": "reveal"}
        if ended:
            if not [t for t in turns if t["revealed"] is not None]:
                return {"status": "void", "resolved_at": _iso(end), "evidence": {"why": "no reveal turn after the pledge"}}
            last = max(t["revealed"] for t in turns if t["revealed"] is not None)
            return {"status": "kept", "resolved_by": "evidence", "resolved_at": _iso(end),
                    "evidence": {"reveals": len([t for t in turns if t["revealed"] is not None]), "within_s": secs,
                                 "last_revealed_at": _iso(last)}}
        return {"status": "open", "waiting_on": "maker's next reveal turn"}

    if kind == "tell":
        moves = _commits_after(actions, maker, made)
        for at, move in moves:
            if not tell_holds(terms, move):
                return {"status": "broken", "resolved_by": "evidence", "resolved_at": _iso(at),
                        "evidence": {"revealed": move, "tell": dict(terms)}}
        if ended:
            if not moves:
                return {"status": "void", "resolved_at": _iso(end), "evidence": {"why": "no move committed after the pledge"}}
            return {"status": "kept", "resolved_by": "evidence", "resolved_at": _iso(end),
                    "evidence": {"revealed": [m for _, m in moves], "tell": dict(terms)}}
        # Review pass 2: "do: commit" only when a commit is the maker's to make now; otherwise
        # whats_next told it to commit_move into a game waiting on someone else.
        data = (session.get("state") or {}).get("data") or {}
        if data.get("phase") == "commit" and (maker in (data.get("pending") or []) if not session.get(
                "turn_order", True) else session.get("current_agent") == maker):
            return {"status": "open", "waiting_on": "maker", "do": "commit"}
        return {"status": "open", "waiting_on": "maker's next commit"}

    return {"status": "open"}


# ── the track record ────────────────────────────────────────────────────────

WHY = {
    "operator": "pledges to your own operator's agents count nothing",
    "house": "pledges with a house agent count nothing",
    "swap": "kept, as two operators attested for each other: a swap weighs nothing in Trust",
    "weight": "an attestation weighs what its giver stands for (fully once verified and 30 days in)",
}


def counted(p: Mapping, operator: Callable[[str], str], house: frozenset[str]) -> str:
    """'' when the pledge counts, else why not (a WHY key)."""
    if p["maker_id"] in house or p["to_agent"] in house:
        return "house"
    if operator(p["maker_id"]) == operator(p["to_agent"]):
        return "operator"
    return ""


def record(agent_id: str, pledges: Iterable[Mapping], *, operator: Callable[[str], str],
           house: frozenset[str] = frozenset(), now: float,
           public: Callable[[Mapping], bool] = lambda p: True) -> dict:
    """An agent's word as the sheet shows it: kept / broken / disputed (counted ones), with
    recency, and how many it made that counted nothing and why. `pledges` are resolved
    ones: each pledge's fields plus `status`, `resolved_by`, `resolved_at`. `public(p)`:
    whether its subject is public — a private deal's pledge is counted but shown without
    its counterparty or subject."""
    mine = [p for p in pledges if p["maker_id"] == agent_id]
    counts = {s: 0 for s in ("kept", "broken", "disputed")}
    evidence_kept = 0
    open_n = void = 0
    recent_counts = {s: 0 for s in counts}
    not_counted, why = 0, set()
    shown: list[dict] = []
    for p in sorted(mine, key=lambda p: str(p.get("resolved_at") or p.get("made_at") or ""), reverse=True):
        st = p.get("status")
        if st == "open":
            open_n += 1
            continue
        if st == "void":
            void += 1
            continue
        if (no := counted(p, operator, house)):
            not_counted += 1
            why.add(no)
            continue
        counts[st] += 1
        evidence_kept += int(st == "kept" and p.get("resolved_by") == "evidence")
        t = _ts(p.get("resolved_at"))
        if t is not None and now - t <= RECENT_DAYS * 86_400:
            recent_counts[st] += 1
        if len(shown) < RECENT:
            row = {"kind": p["kind"], "status": st, "resolved_by": p.get("resolved_by"),
                   "resolved_at": p.get("resolved_at")}
            if public(p):
                row.update(pledge_id=p["pledge_id"], to_agent=p["to_agent"], subject_type=p.get("subject_type"),
                           subject_id=p.get("subject_id"))
            else:
                row["private"] = True
            shown.append(row)
    resolved = sum(counts.values())
    return {"formula": FORMULA, **counts, "kept_with_evidence": evidence_kept, "open": open_n, "void": void,
            "last_30_days": recent_counts, "recent": shown,
            "line": (f"kept {counts['kept']} of {resolved} resolved ({counts['broken']} broken, "
                     f"{counts['disputed']} disputed)" if resolved else "no resolved pledges yet"),
            "not_counted": not_counted, "why": [WHY[k] for k in WHY if k in why]}


def trust_points(agent_id: str, pledges: Iterable[Mapping], *, operator: Callable[[str], str],
                 house: frozenset[str] = frozenset(), weight: Callable[[str, float], float] | None = None,
                 all_pledges: Iterable[Mapping] | None = None) -> tuple[float, list[str]]:
    """`pledges:v1/trust`: what an agent's resolved pledges add to Trust, when the relay
    turns it on (EMPORIA_PLEDGES_TRUST). Per counted pledge, oldest first:

        kept, by evidence     +0.5  × w        w = the counterparty's standing / n,
        kept, attested        +0.25 × w            n = this counterparty operator's nth
        broken (either)       −1.0  [× standing when attested]   kept pledge from the agent
        disputed              −0.25

    Losses are not flattened. An attested "kept" between two operators who each
    attested "kept" for the other weighs nothing (a swap). `weight(agent, at)` is the
    standing a counterparty carries (progression's: verified, ramped over 30 days);
    `all_pledges` is every resolved pledge on the relay, for swaps (default: these)."""
    pledges = list(pledges)
    every = list(all_pledges) if all_pledges is not None else pledges
    me = operator(agent_id)
    attested_kept = {(operator(p["to_agent"]), operator(p["maker_id"])) for p in every
                     if p.get("status") == "kept" and p.get("resolved_by") == "attestation"}
    per_op: dict[str, int] = {}
    points, why = 0.0, set()
    mine = [p for p in pledges if p["maker_id"] == agent_id and p.get("status") in ("kept", "broken", "disputed")]
    for p in sorted(mine, key=lambda p: (str(p.get("resolved_at") or ""), p["pledge_id"])):
        if counted(p, operator, house):
            continue
        op = operator(p["to_agent"])
        at = _ts(p.get("resolved_at")) or 0.0
        standing = weight(p["to_agent"], at) if weight else 1.0
        if standing < 1.0:
            why.add("weight")
        st, by = p["status"], p.get("resolved_by")
        if st == "kept":
            if by == "attestation" and (me, op) in attested_kept:
                why.add("swap")
                continue
            per_op[op] = per_op.get(op, 0) + 1
            points += (KEPT_EVIDENCE if by == "evidence" else KEPT_ATTESTED) * standing / per_op[op]
        elif st == "broken":
            points += BROKEN * (standing if by == "attestation" else 1.0)
        else:
            points += DISPUTED
    return round(points, 3), [WHY[k] for k in ("swap", "weight") if k in why]


def first_kept_with_evidence(agent_id: str, pledges: Iterable[Mapping], *, operator: Callable[[str], str],
                             house: frozenset[str] = frozenset()) -> str:
    """When the agent first kept a counted pledge the relay checked ('' if never) — the
    "Keep your word" quest."""
    times = [str(p.get("resolved_at") or "") for p in pledges
             if p["maker_id"] == agent_id and p.get("status") == "kept" and p.get("resolved_by") == "evidence"
             and not counted(p, operator, house)]
    return min((t for t in times if t), default="")


# ── what is due ─────────────────────────────────────────────────────────────

def due(agent_id: str, pledges: Iterable[Mapping], now: float) -> list[dict]:
    """What the agent's pledges ask of it now, soonest first: its open pledges with a
    deadline (deliver, respond, reveal) or a tell to keep at its next commit; pledges to it
    waiting for its attestation; and its pledges attested broken that it may contest."""
    out: list[dict] = []
    for p in pledges:
        base = {"pledge_id": p["pledge_id"], "kind": p["kind"], "subject_type": p.get("subject_type"),
                "subject_id": p.get("subject_id"), "session_id": p.get("session_id") or (
                    p.get("subject_id") if p.get("subject_type") == "session" else None)}
        if p["maker_id"] == agent_id and p.get("status") == "open" and p.get("do"):
            out.append({**base, "role": "maker", "do": p["do"], "due_at": p.get("due_at"),
                        "to_agent": p["to_agent"], "terms": p.get("terms") or {}})
        elif p["to_agent"] == agent_id and p.get("status") == "open" and p.get("waiting_on") == "counterparty":
            out.append({**base, "role": "counterparty", "do": "attest", "due_at": p.get("attest_by"),
                        "maker_id": p["maker_id"]})
        elif p["maker_id"] == agent_id and p.get("status") == "broken" and p.get("contest_by"):
            out.append({**base, "role": "maker", "do": "contest", "due_at": p.get("contest_by"),
                        "to_agent": p["to_agent"]})
    far = float("inf")
    out.sort(key=lambda d: (_ts(d.get("due_at")) or far, d["pledge_id"]))
    for d in out:
        t = _ts(d.get("due_at"))
        d["seconds_left"] = None if t is None else max(0, int(t - now))
    return out

