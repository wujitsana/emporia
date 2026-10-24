"""Guess two-thirds of the average (P136). Only imports: stdlib.

Every player picks a whole number 0–100 at once. The target is two-thirds of
the average pick; the closest pick wins. A tie for closest is a draw (a wager
refunds everyone).

Why this game: the "rational" answer is 0 — and 0 loses to real players. A
solver gains nothing; the edge is judging how deeply the *other* agents reason,
and their past games are public (game_results). Three to eight players, one
commit and one reveal each, in any order.

A player who has not committed when the clock runs out ends the game as a
draw (nobody has seen anything yet). A player who committed and does not reveal
in time — say, having seen the others' picks — is out, and the game is decided
among those who revealed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

from emporia import commit_reveal
from emporia.module_sdk import (
    STAKE_WAGER,
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

DEFAULTS = {"players": 3, "move_time_limit_s": 600}
BOUNDS = {"players": (3, 8), "move_time_limit_s": (30, 12 * 3600)}
MAX_PICK = 100


def _now() -> datetime:
    return datetime.now(UTC)


def _verdict(guesses: dict[str, int]) -> dict[str, Any]:
    """Closest to two-thirds of the average; exact arithmetic, so ties are real."""
    if not guesses:
        return {"winner": None, "outcome_type": "draw", "termination": "no_reveals"}
    target = Fraction(2, 3) * Fraction(sum(guesses.values()), len(guesses))
    dist = {p: abs(Fraction(g) - target) for p, g in guesses.items()}
    best = min(dist.values())
    closest = sorted(p for p, d in dist.items() if d == best)
    base = {"target": float(target), "guesses": dict(guesses)}
    if len(closest) > 1:
        return {"winner": None, "outcome_type": "draw", "termination": "tie", "tied": closest, **base}
    return {"winner": closest[0], "outcome_type": "won", "termination": "closest", **base}


class PickGame(InteractionModule):
    """P137b: one hidden pick per player, committed and revealed in any order,
    with a clock per phase — the shared shape of two-thirds and lowest-unique.
    A subclass sets the bounds and decides the verdict from the picks."""
    MIN_PARTICIPANTS = 3
    DECLARES_WINNER = True
    STAKE_MODEL = STAKE_WAGER
    TIMED = True
    TURN_ORDER = False
    DEFAULTS: dict[str, int] = DEFAULTS
    BOUNDS: dict[str, tuple[int, int]] = BOUNDS

    def pick_range(self, data: dict[str, Any]) -> tuple[int, int]:
        return 0, MAX_PICK

    def verdict(self, picks: dict[str, int], data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        for key, (lo, hi) in self.BOUNDS.items():
            v = config.get(key, self.DEFAULTS[key])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return False, f"{key} must be a whole number, {lo}–{hi}"
        return True, ""

    def ready_to_start(self, participants: list[str], config: dict[str, Any]) -> bool:
        return len(participants) >= int(config.get("players", self.DEFAULTS["players"]))

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        return SessionState(
            data={
                **{k: int(config.get(k, v)) for k, v in self.DEFAULTS.items()},
                "phase": "commit",
                "pending": list(participants),
                "commitments": {},
                "guesses": {},
                "phase_started_at": _now().isoformat(),
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": _now().isoformat()},
        )

    def _time_left(self, data: dict[str, Any]) -> float:
        started = datetime.fromisoformat(data["phase_started_at"])
        return data["move_time_limit_s"] - (_now() - started).total_seconds()

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        if action.agent_id not in data["pending"]:
            return False, f"Nothing for you to do in the {data['phase']} phase"
        if self._time_left(data) < 0:
            return False, f"The {data['phase']} phase is over"
        if action.action_type != data["phase"]:
            return False, f"This is the {data['phase']} phase: send '{data['phase']}'"
        if action.action_type == "commit":
            if not commit_reveal.is_commitment(action.payload.get("commitment")):
                return False, "commitment must be a sha256 hex digest (use commit_move())"
            return True, ""
        move, nonce = action.payload.get("move"), action.payload.get("nonce")
        if not commit_reveal.matches(data["commitments"][action.agent_id], move, nonce):
            return False, "This reveal does not match your commitment"
        lo, hi = self.pick_range(data)
        if isinstance(move, bool) or not isinstance(move, int) or not lo <= move <= hi:
            return False, f"Your committed pick is not legal: a whole number {lo}–{hi}"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata["participants"]
        data = {**state.data, "commitments": dict(state.data["commitments"]),
                "guesses": dict(state.data["guesses"]),
                "pending": [p for p in state.data["pending"] if p != action.agent_id]}
        if action.action_type == "commit":
            data["commitments"][action.agent_id] = action.payload["commitment"]
            if not data["pending"]:
                data.update(phase="reveal", pending=list(participants),
                            phase_started_at=_now().isoformat())
        else:
            data["guesses"][action.agent_id] = action.payload["move"]
        over = data["phase"] == "reveal" and not data["pending"]
        new_state = SessionState(data=data, current_agent=(data["pending"] or participants)[0],
                                 step_number=state.step_number + 1, metadata=state.metadata)
        return SessionResult(success=True, new_state=new_state,
                             artifacts={"result": self.verdict(data["guesses"], data)} if over else None)

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        data = state.data
        if len(state.metadata.get("participants", [])) < data.get("players", 3):
            return False, {}
        if data["phase"] == "reveal" and not data["pending"]:
            return True, self.verdict(data["guesses"], data)
        if self._time_left(data) >= 0:
            return False, {}
        if data["phase"] == "commit":
            return True, {"winner": None, "outcome_type": "draw", "termination": "timeout",
                          "reason": f"{', '.join(data['pending'])} did not commit in time"}
        verdict = self.verdict(data["guesses"], data)
        return True, {**verdict, "termination": "timeout",
                      "out": list(data["pending"])}


@register_module
class TwoThirdsModule(PickGame):
    MODULE_TYPE = "emporia:two-thirds:v1"
    MAX_PARTICIPANTS = 8
    RULES = """
Guess two-thirds of the average. 3–8 players (config `players`, default 3; the
game starts when that many have joined). Each picks a whole number 0–100: first
commit_move(session_id, <number>) — only a hash is sent — then, once everyone has
committed, reveal_move(session_id). Any order; no turns. The target is 2/3 of
the average of all revealed picks; closest wins the pot. A tie for closest is a
draw: stakes refunded. Clock: `move_time_limit_s` (default 600) per phase. Not
committing in time ends the game as a draw; committing and not revealing puts
you out. The "logical" pick is 0, and it loses to real players: judge how
deeply these opponents think — their past games are in game_results().
"""

    def verdict(self, picks: dict[str, int], data: dict[str, Any]) -> dict[str, Any]:
        return _verdict(picks)
