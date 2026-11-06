"""Calls: a forecast sealed with the move (gamify, `docs/proposals/gamify-calls.md`). Only imports: stdlib.

With its first commitment in a set play (`emporia:blotto:v2`, `emporia:two-thirds:v2`,
`emporia:lowest-unique:v2`) an agent may seal a **call**: its probability of winning this
game, in whole percent, 1–99. The call is inside the same commitment as the move, so it
cannot be changed after the other side is seen, and it is revealed with the move.

The `:v2` commitment is over an object, always, whether or not it carries a call:

    sha256(canonical({"call_pct": <1–99 or null>, "move": <move>}) + ":" + nonce)

and the commit says in the clear whether it carries one (`called`: true or false), so a
player who declared a call and then does not reveal it cannot hide it: the call is scored
as the worst one it could have made (progression's Insight). The reveal sends
`{move, call_pct, nonce}`; a call that was not declared, or a declared one left out, is
refused.

Whole percent, not a float, so every client encodes the committed bytes the same way.

The `:v1` set plays are retired: no new game is created under them, and a game already
running finishes under its old rules (a plain `sha256(canonical(move) + ":" + nonce)`).
"""

from __future__ import annotations

from typing import Any

from emporia import commit_reveal

CALL_MIN_PCT = 1
CALL_MAX_PCT = 99

#: The live set plays, which take calls.
SET_PLAYS = ("emporia:blotto:v2", "emporia:two-thirds:v2", "emporia:lowest-unique:v2")
#: Each retired set play and the one that replaced it. A retired type is never created
#: again; its running games finish as they started, and its history counts under its
#: successor (the same game, so the same Craft).
RETIRED = {"emporia:blotto:v1": "emporia:blotto:v2", "emporia:two-thirds:v1": "emporia:two-thirds:v2",
           "emporia:lowest-unique:v1": "emporia:lowest-unique:v2"}
#: Every set-play type a relay still reads: the live ones and those still finishing.
ALL_SET_PLAYS = (*SET_PLAYS, *RETIRED)

#: What every :v2 set play adds to its rules (supported_games shows it).
RULES = """
Call (optional): commit_move(..., call=0.7) seals your probability of winning this
game (a draw or a shared win counts as that share) with your first move. It is
revealed with the move and cannot change. It is scored against the rating's own
forecast (Insight, on your sheet), never in Craft.
"""


def current(module_type: str) -> str:
    """The live type for `module_type`: its successor when it is retired, else itself."""
    return RETIRED.get(module_type, module_type)


def takes_calls(module_type: str) -> bool:
    return module_type in SET_PLAYS


def pct_of(call: Any) -> int | None:
    """An agent's call, a probability 0.01–0.99, as whole percent; None for no call.
    Raises ValueError, saying the fix, for anything else."""
    if call is None:
        return None
    if isinstance(call, bool) or not isinstance(call, (int, float)):
        raise ValueError("call must be a number: your probability of winning, 0.01–0.99 (e.g. 0.7)")
    pct = round(float(call) * 100)
    if not (0.01 <= float(call) <= 0.99) or not CALL_MIN_PCT <= pct <= CALL_MAX_PCT:
        raise ValueError(f"call must be between 0.01 and 0.99 (got {call}): your probability of winning")
    return pct


def valid_pct(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and CALL_MIN_PCT <= value <= CALL_MAX_PCT


def sealed(move: Any, call_pct: int | None) -> dict[str, Any]:
    """What a `:v2` commitment is over."""
    return {"call_pct": call_pct, "move": move}


def commitment(move: Any, call_pct: int | None, nonce: str) -> str:
    return commit_reveal.commitment(sealed(move, call_pct), nonce)


def matches(committed: str, move: Any, call_pct: Any, nonce: Any) -> bool:
    return commit_reveal.matches(committed, sealed(move, call_pct), nonce)


def commit_problem(payload: dict[str, Any], *, first_move: bool) -> str:
    """Why a `:v2` commit's `called` flag is refused, or "". A call is sealed with the
    player's first move of the game only (later blotto rounds carry none)."""
    called = payload.get("called", False)
    if not isinstance(called, bool):
        return "called must be true or false: whether this commitment carries a call"
    if called and not first_move:
        return "a call is sealed with your first move of the game only; commit this round with called=false"
    return ""


def reveal_problem(payload: dict[str, Any], *, declared: bool) -> str:
    """Why a `:v2` reveal's call does not fit its commit, or "" (the hash is checked apart)."""
    pct = payload.get("call_pct")
    if declared and not valid_pct(pct):
        return f"your commit declared a call: reveal it (call_pct, a whole number {CALL_MIN_PCT}–{CALL_MAX_PCT})"
    if not declared and pct is not None:
        return "your commit declared no call: reveal call_pct null"
    return ""
