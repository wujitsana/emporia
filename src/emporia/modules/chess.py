"""Chess interaction module. Only imports: chess (optional), stdlib."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia.module_sdk import (
    STAKE_WAGER,
    InteractionModule,
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


def _result(board, participants: list[str]) -> dict[str, Any]:
    """The game's result, read from the board — the one answer both
    `apply_action` and `is_terminal` give.

    `is_terminal` used to return `state.current_agent`, which after the final
    move is the side *to move*: the player just checkmated. Settlement reads
    `is_terminal`, so every decided chess game paid the loser, and every draw
    paid whoever happened to be on move. The colour comes from the board;
    white is `participants[0]` (see `initial_state`).

    A draw names no winner and says so with `outcome_type="draw"`, which the
    session route settles as a refund — nobody won, so nobody is paid.
    """
    outcome = board.outcome(claim_draw=True)
    if outcome is None:
        return {"winner": None}
    termination = str(outcome.termination)
    if outcome.winner is None or len(participants) < 2:
        return {"winner": None, "outcome_type": "draw", "termination": termination}
    winner = participants[0] if outcome.winner == _chess_lib.WHITE else participants[1]
    return {"winner": winner, "outcome_type": "won", "termination": termination}


@register_module
class ChessModule(InteractionModule):
    MODULE_TYPE = "emporia:chess:v1"
    DECLARES_WINNER = True
    STAKE_MODEL = STAKE_WAGER
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
            try:
                san = board.san(move)
            except Exception:
                san = uci
            board.push(move)
            new_fen = board.fen()
            terminal = board.is_game_over(claim_draw=True)
            result = _result(board, participants)
        else:
            # Without chess library: accept move, advance state, no legality check
            new_fen = state.data["board_fen"]
            san = uci
            terminal = False
            result = {"winner": None}

        next_agent = _next_player(participants, action.agent_id)
        new_state = SessionState(
            data={
                "board_fen": new_fen,
                "variant": state.data.get("variant", "standard"),
                "time_control": state.data.get("time_control", 300),
                "last_move": uci,
                "last_san": san,
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
            return True, _result(board, state.metadata.get("participants", []))
        return False, {}
