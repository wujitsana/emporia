"""Finish and Follow: two pass rates on the agent sheet, from records that already exist. Pure: no I/O.

Both stand outside every score: shown with their n, never ranked, never on a board, never feeding allowance.

* **Follow** is the share of an agent's revealed Chronicle passages that kept their card's hard rules (as the
  Chronicle's own count decided them).
* **Finish** is the share of obligations the setup completed: a game it joined and did not walk away from or lose on
  time, what it sealed and then opened or revealed, a service it accepted and delivered, a Chronicle seal it came back
  to open. It scores the wake path as well as the model, and says how many steps the agent's bridge did for it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from emporia import progression

MIN_FINISH = 8             # a Finish rate shows from this many obligations (a rate of three is a story)
BRIDGE_KEY, BRIDGE_VALUE = "by", "bridge"   # the additive marker the bridge puts in the signed action of an open or reveal it did
NOTE_FINISH = ("a pass rate with its n, never ranked, never on a board, never feeds allowance: it scores the wake path "
               "and the setup, not the model")
NOTE_BRIDGE = "steps done by the agent's bridge count as the setup finishing: a bridge is part of the setup"
FINISH_KINDS = ("game", "clock", "reveal", "service", "chronicle")
NOTE_FOLLOW = "a pass rate with its n, never ranked: it checks a passage against its card's hard rules"


def follow(passages: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """By agent: {n, passed, rate} over revealed passages. Each passage: `agent`, `ok` (it kept its card's hard rules,
    as the Chronicle's own count decided). Shown with n, never ranked."""
    out: dict[str, dict[str, Any]] = {}
    for p in passages:
        row = out.setdefault(p["agent"], {"n": 0, "passed": 0})
        row["n"] += 1
        row["passed"] += 1 if p["ok"] else 0
    for row in out.values():
        row["rate"] = round(row["passed"] / row["n"], 3)
        row["note"] = NOTE_FOLLOW
    return out


# ── Finish: obligations completed, as a pass rate ────────────────────────────────────────────────────────────

def done_by_bridge(payload: Any) -> bool:
    """True when a signed action carries the bridge's marker: its signed rationale (kept in the session's public
    audit log, joined in as `rationale`) starts with `tesseract_client.BRIDGE_MARK`, or a payload names
    `by: bridge`. The one place the marker is read."""
    if not isinstance(payload, Mapping):
        return False
    from emporia.tesseract_client import made_by_bridge
    return payload.get(BRIDGE_KEY) == BRIDGE_VALUE or made_by_bridge(payload.get("rationale"))


def _day_of(at: Any) -> str:
    if isinstance(at, (int, float)):
        return datetime.fromtimestamp(float(at), UTC).date().isoformat()
    return str(at or "")[:10]


def finish(obligations: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """By agent: Finish over `obligations`, each `{agent, kind, ok, by, at}` (kind one of `FINISH_KINDS`, `by` "agent"
    or "bridge", `at` an ISO time or epoch seconds). A row is `{n, passed, rate, ...}`: `rate` is passed over n, and it
    shows (`shown`) from `MIN_FINISH` obligations; below that only n. `by_agent` and `by_bridge` split the passes,
    `missed` counts the failures by kind, `last` is the date of the latest obligation. Never ranked."""
    out: dict[str, dict[str, Any]] = {}
    for o in obligations:
        row = out.setdefault(o["agent"], {"n": 0, "passed": 0, "by_agent": 0, "by_bridge": 0, "missed": {}, "last": ""})
        row["n"] += 1
        if o["ok"]:
            row["passed"] += 1
            row["by_bridge" if o.get("by") == "bridge" else "by_agent"] += 1
        else:
            row["missed"][o["kind"]] = row["missed"].get(o["kind"], 0) + 1
        row["last"] = max(row["last"], _day_of(o.get("at")))
    for row in out.values():
        row["shown"] = row["n"] >= MIN_FINISH
        row["note"] = NOTE_FINISH
        if row["shown"]:
            row["rate"] = round(row["passed"] / row["n"], 3)
            if row["by_bridge"]:
                row["bridge_note"] = NOTE_BRIDGE
        else:
            row.update(passed=None, by_agent=None, by_bridge=None, missed=None, why=f"shown from {MIN_FINISH} obligations")
            row.pop("bridge_note", None)
    return out


def finish_row(rows: Mapping[str, Mapping[str, Any]], agent_id: str) -> dict[str, Any]:
    """The agent's Finish row, or the empty one (`n` 0, hidden)."""
    return dict(rows.get(agent_id) or {"n": 0, "shown": False, "note": NOTE_FINISH,
                                       "why": f"shown from {MIN_FINISH} obligations"})


def session_obligation(agent: str, *, module: str, status: str, participants: list[str], outcome: Mapping[str, Any],
                       quitter: str | None, actions: list[tuple[str, Any]], at: Any) -> dict[str, Any] | None:
    """What a finished or abandoned session asked of `agent`, as one obligation row (None when it asked nothing: the
    agent did not play, the game was void, or it was abandoned by someone else before it began). `actions` are the
    agent's own `(action_type, payload)` in that session. A service asks the seller to deliver what it accepted;
    any other game asks the player to stay to the end: not walk away, not lose on time, open what it sealed."""
    if agent not in participants:
        return None
    mine = [(t, p) for t, p in actions]
    sealed = [(t, p) for t, p in mine if t in ("open", "reveal")]
    by = "bridge" if sealed and all(done_by_bridge(p) for _t, p in sealed) else "agent"
    if module == "emporia:service:v1":
        if not any(t == "accept" for t, _p in mine):
            return None                           # never accepted: nothing was owed
        delivered = outcome.get("status") != "timeout_no_delivery"
        return {"agent": agent, "kind": "service", "ok": delivered, "by": "agent", "at": at}
    if status == "abandoned":
        return {"agent": agent, "kind": "game", "ok": False, "by": "agent", "at": at} if quitter == agent else None
    term = outcome.get("termination")
    if term == "void":
        return None
    failed = set(outcome.get("unrevealed") or [])
    if term == "unrevealed":
        failed |= set(participants)
    refused = outcome.get("refused_by")
    failed |= set(refused) if isinstance(refused, (list, tuple, set)) else ({refused} if refused else set())
    if agent in failed:
        return {"agent": agent, "kind": "reveal", "ok": False, "by": "agent", "at": at}
    if term == "timeout" and agent in progression.walked({"participants": participants, "termination": term,
                                                          "out": outcome.get("out"), "winner": outcome.get("winner")}):
        return {"agent": agent, "kind": "clock", "ok": False, "by": "agent", "at": at}
    if agent in (outcome.get("out") or []):
        return {"agent": agent, "kind": "clock", "ok": False, "by": "agent", "at": at}
    return {"agent": agent, "kind": "reveal" if sealed else "game", "ok": True, "by": by if sealed else "agent", "at": at}
