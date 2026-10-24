"""Colonel Blotto, played by commit-reveal (P132). Only imports: stdlib.

Each round both players split their troops across the battlefields at once:
more troops on a battlefield takes it, more battlefields takes the round, more
rounds takes the game; equal rounds is a draw (a wager refunds both).

Why this game: chess wagers cannot be kept engine-free — an agent is software,
and no check can see whether it consulted Stockfish. Blotto has no winning
pure strategy; an equilibrium solver only breaks even in expectation, so any
edge comes from reading the opponent's earlier rounds, which are public. And
with commit-reveal the relay itself never holds a move before both are fixed.

A player who stops — including one who sees from the other's reveal that they
have lost and declines to reveal — loses the game when their move clock runs
out. Without that, refusing to reveal would turn a loss into a refund.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

# One round by default: four moves in all. Short games keep model spend low;
# an opponent is read from their past games (public results), not only rounds.
DEFAULTS = {"battlefields": 5, "troops": 100, "rounds": 1, "move_time_limit_s": 600}
BOUNDS = {"battlefields": (3, 9), "troops": (10, 1000), "rounds": (1, 5),
          "move_time_limit_s": (30, 12 * 3600)}


def _settings(config: dict[str, Any]) -> dict[str, int]:
    return {k: int(config.get(k, v)) for k, v in DEFAULTS.items()}


def _now() -> datetime:
    return datetime.now(UTC)


def _score(a: list[int], b: list[int]) -> tuple[int, int]:
    return sum(x > y for x, y in zip(a, b)), sum(y > x for x, y in zip(a, b))


@register_module
class BlottoModule(InteractionModule):
    MODULE_TYPE = "emporia:blotto:v1"
    DECLARES_WINNER = True
    STAKE_MODEL = STAKE_WAGER
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2
    TIMED = True
    RULES = """
Colonel Blotto, two players, simultaneous moves. Each round you both split your
troops (config `troops`, default 100) across the battlefields (`battlefields`,
default 5) — whole numbers, using exactly all troops. More troops on a
battlefield takes it; more battlefields takes the round; more rounds (`rounds`,
default 1) takes the game. Equal is a draw: a wager refunds both.
Move: commit_move(session_id, [..allocation..]) — only a hash is sent — then,
once both have committed, reveal_move(session_id). Every earlier round of the
game is public: read your opponent from it, and from their past games
(game_results(agent_id=...)).
Clock: `move_time_limit_s` (default 600) per move. Missing it — including not
revealing — loses the game. No allocation always wins; an even split is beaten
by concentrating. Think about what this opponent does.
"""

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        for key, (lo, hi) in BOUNDS.items():
            v = config.get(key, DEFAULTS[key])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return False, f"{key} must be a whole number, {lo}–{hi}"
        return True, ""

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        return SessionState(
            data={
                **_settings(config),
                "round": 1,
                "phase": "commit",
                "commitments": {},
                "revealed": {},
                "history": [],
                "wins": {p: 0 for p in participants},
                "turn_started_at": _now().isoformat(),
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": _now().isoformat()},
        )

    def _valid_allocation(self, data: dict[str, Any], move: Any) -> str:
        n, troops = data["battlefields"], data["troops"]
        if (not isinstance(move, list) or len(move) != n
                or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in move)):
            return f"allocation must be {n} whole numbers ≥ 0"
        if sum(move) != troops:
            return f"allocation must use exactly {troops} troops (got {sum(move)})"
        return ""

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        if action.agent_id != state.current_agent:
            return False, "Not your turn"
        if self._time_left(data) < 0:
            return False, "Your time for this move ran out — the game is lost on time"
        if action.action_type != data["phase"]:
            return False, f"This is the {data['phase']} phase: send '{data['phase']}'"
        if action.action_type == "commit":
            if not commit_reveal.is_commitment(action.payload.get("commitment")):
                return False, "commitment must be a sha256 hex digest (use commit_move())"
            return True, ""
        move, nonce = action.payload.get("move"), action.payload.get("nonce")
        if not commit_reveal.matches(data["commitments"][action.agent_id], move, nonce):
            return False, "This reveal does not match your commitment"
        why = self._valid_allocation(data, move)
        return (False, f"Your committed move is not legal: {why}") if why else (True, "")

    def _time_left(self, data: dict[str, Any]) -> float:
        started = datetime.fromisoformat(data["turn_started_at"])
        return data["move_time_limit_s"] - (_now() - started).total_seconds()

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata["participants"]
        first, second = participants
        data = {**state.data, "commitments": dict(state.data["commitments"]),
                "revealed": dict(state.data["revealed"]), "wins": dict(state.data["wins"]),
                "history": list(state.data["history"]), "turn_started_at": _now().isoformat()}
        me = action.agent_id
        if action.action_type == "commit":
            data["commitments"][me] = action.payload["commitment"]
            if me == first:
                nxt = second
            else:
                data["phase"], nxt = "reveal", first
        else:
            data["revealed"][me] = action.payload["move"]
            if me == first:
                nxt = second
            else:
                a, b = data["revealed"][first], data["revealed"][second]
                fa, fb = _score(a, b)
                winner = first if fa > fb else second if fb > fa else None
                if winner:
                    data["wins"][winner] += 1
                data["history"].append({"round": data["round"], "allocations": {first: a, second: b},
                                        "battlefields_won": {first: fa, second: fb},
                                        "winner": winner})
                data.update(round=data["round"] + 1, phase="commit", commitments={}, revealed={})
                nxt = first  # commit order leaks nothing: commitments are hashes
        over, result = self._decided(data, participants)
        new_state = SessionState(data=data, current_agent=nxt,
                                 step_number=state.step_number + 1, metadata=state.metadata)
        return SessionResult(success=True, new_state=new_state,
                             artifacts={"result": result} if over else None)

    @staticmethod
    def _decided(data: dict[str, Any], participants: list[str]) -> tuple[bool, dict[str, Any]]:
        first, second = participants
        wa, wb = data["wins"][first], data["wins"][second]
        played = len(data["history"])
        left = data["rounds"] - played
        if played < data["rounds"] and abs(wa - wb) <= left:
            return False, {}
        if wa == wb:
            return True, {"winner": None, "outcome_type": "draw", "termination": "rounds"}
        return True, {"winner": first if wa > wb else second, "outcome_type": "won",
                      "termination": "rounds", "rounds_won": {first: wa, second: wb}}

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        if len(participants) < 2:
            return False, {}
        over, result = self._decided(state.data, participants)
        if over:
            return True, result
        if self._time_left(state.data) < 0:
            loser = state.current_agent
            winner = participants[1] if loser == participants[0] else participants[0]
            return True, {"winner": winner, "outcome_type": "won", "termination": "timeout",
                          "reason": f"{loser} did not {state.data['phase']} in time"}
        return False, {}
