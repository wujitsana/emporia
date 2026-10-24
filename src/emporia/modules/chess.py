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
#: P131: bounds on a per-move time limit. The floor leaves an LLM agent time to
#: think; the ceiling stays under the 24h idle sweep, which abandons instead.
MIN_MOVE_SECONDS = 30
MAX_MOVE_SECONDS = 12 * 3600

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


def _colours(data: dict[str, Any], participants: list[str]) -> tuple[str | None, str | None]:
    """(white, black). Stored at start since P131 (the creator may play
    black); older states mean participants[0] is white."""
    if "white" in data:
        return data.get("white"), data.get("black")
    return (participants[0] if participants else None,
            participants[1] if len(participants) > 1 else None)


def _parse_move(board, text: str):
    """A UCI move (e2e4) or, as documented, SAN (e4, Nf3, O-O)."""
    try:
        move = _chess_lib.Move.from_uci(text)
        if move in board.legal_moves:
            return move
    except ValueError:
        pass
    return board.parse_san(text)


def _seconds_left(data: dict[str, Any], now: datetime | None = None) -> float | None:
    limit = data.get("move_time_limit_s")
    started = data.get("turn_started_at")
    if not limit or not started:
        return None
    now = now or datetime.now(UTC)
    return float(limit) - (now - datetime.fromisoformat(started)).total_seconds()


def _result(board, participants: list[str], data: dict[str, Any] | None = None) -> dict[str, Any]:
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
    white, black = _colours(data or {}, participants)
    if outcome.winner is None or not (white and black):
        return {"winner": None, "outcome_type": "draw", "termination": termination}
    winner = white if outcome.winner == _chess_lib.WHITE else black
    return {"winner": winner, "outcome_type": "won", "termination": termination}


@register_module
class ChessModule(InteractionModule):
    MODULE_TYPE = "emporia:chess:v1"
    DECLARES_WINNER = True
    STAKE_MODEL = STAKE_WAGER
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2
    #: P131: is_terminal() can end a game by the clock alone (a per-move time
    #: limit), so the relay's sweep must ask it, not only submit_action.
    TIMED = True

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        """P131: game settings, checked before anyone stakes on them.

        start_fen        — a position to start from (FEN). Legal, not already
                           over. The side to move moves first.
        creator_plays    — "white" (default) or "black".
        move_time_limit_s — seconds each move may take; running out loses.
        """
        fen = config.get("start_fen")
        if fen is not None:
            if not isinstance(fen, str):
                return False, "start_fen must be a FEN string"
            try:
                board = _chess_lib.Board(fen)
            except ValueError as e:
                return False, f"start_fen is not a valid FEN: {e}"
            if not board.is_valid():
                return False, f"start_fen is not a legal position ({board.status()!r})"
            if board.is_game_over(claim_draw=True):
                return False, "start_fen is already decided — there would be nothing to play"
        if config.get("creator_plays", "white") not in ("white", "black"):
            return False, "creator_plays must be 'white' or 'black'"
        limit = config.get("move_time_limit_s")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not (
                    MIN_MOVE_SECONDS <= limit <= MAX_MOVE_SECONDS):
                return False, (f"move_time_limit_s must be whole seconds, "
                               f"{MIN_MOVE_SECONDS}–{MAX_MOVE_SECONDS}")
        return True, ""

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        fen = config.get("start_fen") or _STARTING_FEN
        creator, other = participants[0], (participants[1] if len(participants) > 1 else None)
        white, black = (other, creator) if config.get("creator_plays") == "black" else (creator, other)
        white_to_move = (not _HAS_CHESS) or _chess_lib.Board(fen).turn == _chess_lib.WHITE
        first = (white if white_to_move else black) or creator
        now = datetime.now(UTC).isoformat()
        data = {
            "board_fen": fen,
            "start_fen": fen,
            "white": white,
            "black": black,
            "variant": config.get("variant", "standard"),
            "time_control": config.get("time_control", 300),
            "turn_started_at": now,
        }
        if config.get("move_time_limit_s"):
            data["move_time_limit_s"] = int(config["move_time_limit_s"])
        return SessionState(
            data=data,
            current_agent=first,
            step_number=0,
            metadata={"participants": participants, "created_at": now},
        )

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        if action.action_type != "move":
            return False, "Only 'move' actions allowed"
        if action.agent_id != state.current_agent:
            return False, "Not your turn"
        left = _seconds_left(state.data)
        if left is not None and left < 0:
            return False, "Your time for this move ran out — the game is lost on time"
        # Accept either "move" (any format) or "uci" key
        uci = action.payload.get("uci") or action.payload.get("move", "")
        if not uci:
            return False, "move or uci required in payload"
        if not _HAS_CHESS:
            return True, ""
        try:
            board = _chess_lib.Board(state.data["board_fen"])
            _parse_move(board, uci)
        except ValueError:
            return False, f"Illegal move: {uci}"
        except Exception as e:
            return False, f"Invalid move: {e}"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        uci = action.payload.get("uci") or action.payload.get("move", "")
        participants = state.metadata.get("participants", [])

        if _HAS_CHESS:
            board = _chess_lib.Board(state.data["board_fen"])
            move = _parse_move(board, uci)
            san = board.san(move)
            uci = move.uci()
            board.push(move)
            new_fen = board.fen()
            terminal = board.is_game_over(claim_draw=True)
            result = _result(board, participants, state.data)
        else:
            # Without chess library: accept move, advance state, no legality check
            new_fen = state.data["board_fen"]
            san = uci
            terminal = False
            result = {"winner": None}

        next_agent = _next_player(participants, action.agent_id)
        new_state = SessionState(
            data={
                **state.data,
                "board_fen": new_fen,
                "last_move": uci,
                "last_san": san,
                "turn_started_at": datetime.now(UTC).isoformat(),
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
        participants = state.metadata.get("participants", [])
        board = _chess_lib.Board(state.data["board_fen"])
        if board.is_game_over(claim_draw=True):
            return True, _result(board, participants, state.data)
        left = _seconds_left(state.data)
        white, black = _colours(state.data, participants)
        if left is not None and left < 0 and white and black:
            # P131: the side to move ran out of time and loses, as agreed by
            # joining a game with a move limit.
            winner = black if board.turn == _chess_lib.WHITE else white
            return True, {"winner": winner, "outcome_type": "won", "termination": "timeout"}
        return False, {}
