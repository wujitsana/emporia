"""Chess interaction module. Only imports: chess (optional), stdlib."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia.module_sdk import (
    InteractionModule,
    PaymentRules,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

_STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

try:
    import chess as _chess_lib
    _HAS_CHESS = True
except ImportError:
    _chess_lib = None
    _HAS_CHESS = False


def _next_player(participants: list[str], current: str) -> str:
    if not participants:
        return current
    idx = participants.index(current) if current in participants else 0
    return participants[(idx + 1) % len(participants)]


@register_module
class ChessModule(InteractionModule):
    MODULE_TYPE = "emporia:chess:v1"
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        return SessionState(
            data={
                "board_fen": _STARTING_FEN,
                "variant": config.get("variant", "standard"),
                "time_control": config.get("time_control", 300),
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": datetime.now(UTC).isoformat()},
        )

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        if action.action_type != "move":
            return False, "Only 'move' actions allowed"
        if action.agent_id != state.current_agent:
            return False, "Not your turn"
        # Accept either "move" (any format) or "uci" key
        uci = action.payload.get("uci") or action.payload.get("move", "")
        if not uci:
            return False, "move or uci required in payload"
        if not _HAS_CHESS:
            return True, ""
        try:
            board = _chess_lib.Board(state.data["board_fen"])
            move = _chess_lib.Move.from_uci(uci)
            if move not in board.legal_moves:
                return False, f"Illegal move: {uci}"
        except Exception as e:
            return False, f"Invalid move: {e}"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        uci = action.payload.get("uci") or action.payload.get("move", "")
        participants = state.metadata.get("participants", [])

        if _HAS_CHESS:
            board = _chess_lib.Board(state.data["board_fen"])
            move = _chess_lib.Move.from_uci(uci)
            board.push(move)
            outcome = board.outcome(claim_draw=True)
            new_fen = board.fen()
            terminal = board.is_game_over(claim_draw=True)
            result = (
                {"winner": action.agent_id, "termination": str(outcome.termination)}
                if outcome
                else {"winner": None}
            )
        else:
            # Without chess library: accept move, advance state, no legality check
            new_fen = state.data["board_fen"]
            terminal = False
            result = {"winner": None}

        next_agent = _next_player(participants, action.agent_id)
        new_state = SessionState(
            data={
                "board_fen": new_fen,
                "variant": state.data.get("variant", "standard"),
                "time_control": state.data.get("time_control", 300),
                "last_move": uci,
            },
            current_agent=next_agent,
            step_number=state.step_number + 1,
            metadata=state.metadata,
        )

        return SessionResult(
            success=True,
            new_state=new_state,
            artifacts={"result": result} if (terminal or result.get("winner")) else None,
        )

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        if not _HAS_CHESS:
            return False, {}
        board = _chess_lib.Board(state.data["board_fen"])
        if board.is_game_over(claim_draw=True):
            outcome = board.outcome(claim_draw=True)
            return True, {
                "winner": state.current_agent,
                "termination": str(outcome.termination) if outcome else None,
            }
        return False, {}
