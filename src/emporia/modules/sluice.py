"""Sluice (`emporia:sluice:v1`), 2 players: four stones in a line on a 6 x 5 board, with chance from drand.

Channels (columns) 1-6 left to right, rows 1-5 from the bottom. A stone dropped into a channel settles on
whatever is below it. Four of your stones in a line, any direction, wins at once. Only imports: stdlib, the
SDK, `emporia.rand` and `emporia.rand_game`.

Chance, and nothing hidden. The opening (2 silt blocks, then 3 stones for each side, dropped into channels the
beacon picks), who moves first, and the *gate* (the one open channel shut for a turn) all come from drand
rounds. Each player signs a commitment naming a round before it exists (`emporia:rand:v1`, as every dealt game);
the game's round `R0` is the latest of the two. The gate for move n is drawn from the round the previous move
named in its own signed payload (`{"channel": 3, "round": 5512345}`), which the relay takes only when it lies
3 to 40 rounds ahead of its own clock: no round can be chosen after it is seen, by a player or by the relay.
No secret is used or revealed. Everything here is a pure function of the stored beacons, so anyone re-derives it
(`rand_problem`) and replays the signed log (`REPLAY_WITH_RAND`).

`drops` stones each (4-10, default 10). Nobody with four when the drops run out (or the board fills): the player
with more *live threes* (a line of four cells holding three of your stones and one empty cell) wins; level goes
to the player who moved second. There is no draw. Result: winner +1, loser -1.

BALANCE (measured with `scripts/sim_sluice.py`, 3 seeds x 3000 games each, 9000 per cell, look-ahead bot v itself;
"first" = share of games the first mover wins, +- standard error of the pooled rate; look2 v greedy = look2's wins):

    level rule      drops   first mover wins (95 %)       look2 v greedy   ended by (four / threes / level or coin)
    coin flip         7     57.0 +- 0.5 (55.9 - 58.0)     85.6 %           1684 / 4818 / 2498
    coin flip         8     56.5 +- 0.5 (55.4 - 57.5)     85.3 %           2493 / 4412 / 2095
    coin flip         9     55.3 +- 0.5 (54.2 - 56.3)     84.2 %           3825 / 3435 / 1740
    second mover      7     43.4 +- 0.5 (42.4 - 44.4)     -                1684 / 4818 / 2498
    second mover     10     47.5 +- 0.5 (46.5 - 48.5)     83.0 %           6043 / 1683 / 1274

Target 45-55 % first-mover wins and look2 over greedy at least 80 %. A coin flip for a level game (7, 8 or 9 drops)
overshoots: threes favour the first mover, so handing half the level games to it puts it at 55-57 %. The rule in
force is therefore the original one (level goes to the second mover) with `drops` 10, which is inside the target.
`LEVEL_RULE = "coin"` keeps the measured variant, verified by `rand_problem` like the rest; it is not used.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia import rand, rand_game
from emporia.module_sdk import (
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

COLS, ROWS = 6, 5
SILT, EMPTY = "#", "."
SEAT_MARKS = ("A", "B")
DEFAULTS = {"drops": 10, "move_time_limit_s": 600}
BOUNDS = {"drops": (4, 10), "move_time_limit_s": (30, 12 * 3600)}
DEAL_ATTEMPTS = 64
SILT_BLOCKS = 2
STONES_EACH = 3
# What decides a game that ends with live threes level: "second" (the player who moved second; the rule in force) or
# "coin" (a drand flip, see `level_flip`: measured, not chosen, see BALANCE). The balance report swaps it to measure both.
LEVEL_RULE = "second"

Cell = tuple[int, int]          # (channel 1-6, row 1-5)
Grid = list[list[str]]          # grid[channel - 1][row - 1]


# ── the board: pure functions ───────────────────────────────────────────────────────────────

def lines(cols: int = COLS, rows: int = ROWS) -> list[tuple[Cell, ...]]:
    """Every line of four cells, in all four directions."""
    out: list[tuple[Cell, ...]] = []
    for ch in range(1, cols + 1):
        for r in range(1, rows + 1):
            for dc, dr in ((1, 0), (0, 1), (1, 1), (1, -1)):
                cells = tuple((ch + i * dc, r + i * dr) for i in range(4))
                if all(1 <= c <= cols and 1 <= rr <= rows for c, rr in cells):
                    out.append(cells)
    return out


LINES = lines()


def empty_grid() -> Grid:
    return [[EMPTY] * ROWS for _ in range(COLS)]


def _at(grid: Grid, cell: Cell) -> str:
    return grid[cell[0] - 1][cell[1] - 1]


def free_channels(grid: Grid) -> list[int]:
    """The channels that are not full, ascending."""
    return [c + 1 for c in range(COLS) if grid[c][ROWS - 1] == EMPTY]


def drop_row(grid: Grid, channel: int) -> int | None:
    """The row a stone dropped into `channel` settles on, or None when the channel is full."""
    col = grid[channel - 1]
    return col.index(EMPTY) + 1 if EMPTY in col else None


def place(grid: Grid, channel: int, mark: str) -> tuple[Grid, int]:
    """A copy of `grid` with a `mark` dropped into `channel`, and the row it took. Raises when the channel is full."""
    row = drop_row(grid, channel)
    if row is None:
        raise ValueError(f"channel {channel} is full")
    out = [list(c) for c in grid]
    out[channel - 1][row - 1] = mark
    return out, row


def four_line(grid: Grid, mark: str) -> list[list[int]] | None:
    """The first line of four `mark` stones, as [[channel, row], ...], or None."""
    for line in LINES:
        if all(_at(grid, c) == mark for c in line):
            return [list(c) for c in line]
    return None


def live_threes(grid: Grid, mark: str) -> int:
    """Lines of four cells that hold three of `mark` and one empty cell."""
    n = 0
    for line in LINES:
        cells = [_at(grid, c) for c in line]
        if cells.count(mark) == 3 and cells.count(EMPTY) == 1:
            n += 1
    return n


def can_win_in_one(grid: Grid, mark: str) -> bool:
    """Whether some drop by `mark` makes four in a line (a gate is not considered)."""
    for ch in free_channels(grid):
        g, _ = place(grid, ch, mark)
        if four_line(g, mark):
            return True
    return False


def board_rows(grid: Grid) -> list[str]:
    """Five strings, top row first: `.` empty, `#` silt, `A`, `B`."""
    return ["".join(grid[c][r - 1] for c in range(COLS)) for r in range(ROWS, 0, -1)]


# ── the draws: pure functions ───────────────────────────────────────────────────────────────

def deal(scope: str, sig: bytes) -> dict[str, Any]:
    """The opening from the game's round: `{"silt": [[ch, row], ...], "stones": [seat A's, seat B's], "attempt": a}`.
    Placement i of attempt a drops into `open[pick("deal:a:i")]` (the channels not full, ascending); the first
    attempt with no finished line and no one-drop win for either side is taken. After 64 refusals, two silt
    blocks alone (drawn as attempt 64)."""
    for attempt in range(DEAL_ATTEMPTS + 1):
        grid = empty_grid()
        silt: list[list[int]] = []
        stones: list[list[list[int]]] = [[], []]
        for i in range(SILT_BLOCKS + 2 * STONES_EACH):
            if attempt == DEAL_ATTEMPTS and i >= SILT_BLOCKS:
                break
            open_ = free_channels(grid)
            ch = open_[rand_game.pick(scope, f"deal:{attempt}:{i}", sig, len(open_))]
            mark = SILT if i < SILT_BLOCKS else SEAT_MARKS[(i - SILT_BLOCKS) % 2]
            grid, row = place(grid, ch, mark)
            if i < SILT_BLOCKS:
                silt.append([ch, row])
            else:
                stones[(i - SILT_BLOCKS) % 2].append([ch, row])
        if attempt == DEAL_ATTEMPTS or not (
                four_line(grid, "A") or four_line(grid, "B") or can_win_in_one(grid, "A") or can_win_in_one(grid, "B")):
            return {"silt": silt, "stones": stones, "attempt": attempt}
    raise AssertionError("unreachable")


def first(scope: str, sig: bytes, seats: list[str]) -> str:
    """Who moves first."""
    return seats[rand_game.pick(scope, "first", sig, 2)]


def gate(scope: str, n: int, sig: bytes, open_: list[int]) -> int | None:
    """The channel shut before move `n`, drawn from the round's signature `sig` out of `open_` (the channels not
    full). With one channel open, or none, there is no gate."""
    ch = sorted(open_)
    return ch[rand_game.pick(scope, f"gate:{n}", sig, len(ch))] if len(ch) > 1 else None


def level_flip(scope: str, sig: bytes, seats: list[str]) -> str:
    """Who wins a level game under the coin rule: `seats[pick(scope, "level", sig, 2)]`, from the beacon that drew
    the final move's gate (the round the move before it named; the game's own round for a one-move game)."""
    return seats[rand_game.pick(scope, "level", sig, 2)]


def _level_sig(data: dict[str, Any], n_moves: int) -> bytes:
    """The stored signature the coin is drawn from, for a game that ended after `n_moves` moves: the gate beacon of
    its last move. Falls back to the game's own round when that gate needed no beacon (one channel left)."""
    moves = data.get("moves") or []
    r = data.get("rand") or {}
    rnd = moves[n_moves - 2].get("round") if n_moves >= 2 else r.get("round")
    return _sig(data, rnd) or _sig(data, r.get("round"))


def _grid_of(data: dict[str, Any], opening: dict[str, Any]) -> Grid:
    """The board at the opening: silt and each seat's stones at their cells."""
    grid = empty_grid()
    for ch, row in opening.get("silt") or []:
        grid[ch - 1][row - 1] = SILT
    for mark, agent in zip(SEAT_MARKS, data["seats"], strict=True):
        for ch, row in (opening.get("stones") or {}).get(agent) or []:
            grid[ch - 1][row - 1] = mark
    return grid


def _opening_for(data: dict[str, Any], sig: bytes) -> dict[str, Any]:
    """The opening as the state stores it: stones keyed by agent, and who moves first."""
    scope, seats = data["rand"]["scope"], data["seats"]
    d = deal(scope, sig)
    return {"silt": d["silt"], "stones": {seats[0]: d["stones"][0], seats[1]: d["stones"][1]},
            "first": first(scope, sig, seats), "attempt": d["attempt"]}


def _order(data: dict[str, Any], opening: dict[str, Any]) -> list[str]:
    a, b = data["seats"]
    return [a, b] if opening["first"] == a else [b, a]


def _sig(data: dict[str, Any], round_no: Any) -> bytes:
    """The stored signature of `round_no`: under `beacons`, or the game's own round in `beacon_sig`."""
    sig = rand_game.beacon_of(data, round_no)
    r = data.get("rand") or {}
    if not sig and round_no is not None and r.get("round") == round_no:
        sig = rand_game.sig_bytes(data)
    return sig


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def verdict(data: dict[str, Any]) -> dict[str, Any] | None:
    """The result the rules give from the stored opening and moves alone, or None while the game goes on (the
    clock is not here). A pure function: `is_terminal` and `rand_problem` both ask it."""
    opening = data.get("opening")
    if not opening:
        return None
    seats = data["seats"]
    marks = dict(zip(seats, SEAT_MARKS, strict=True))
    order = _order(data, opening)
    grid = _grid_of(data, opening)
    moves = data.get("moves") or []
    for i, m in enumerate(moves):
        who = m["by"]
        grid, _ = place(grid, m["channel"], marks[who])
        if line := four_line(grid, marks[who]):
            loser = seats[1] if who == seats[0] else seats[0]
            return _result(data, who, loser, "four", len(moves), line=line, grid=grid)
    if len(moves) < 2 * data["drops"] and free_channels(grid):
        return None
    threes = {p: live_threes(grid, marks[p]) for p in seats}
    a, b = seats
    if threes[a] != threes[b]:
        winner, how = (a, "threes") if threes[a] > threes[b] else (b, "threes")
    elif LEVEL_RULE == "coin":
        winner, how = level_flip((data.get("rand") or {}).get("scope", ""), _level_sig(data, len(moves)), seats), "coin"
    else:
        winner, how = order[1], "level"           # level goes to the player who moved second
    loser = b if winner == a else a
    return _result(data, winner, loser, how, len(moves), threes=threes, grid=grid)


def _result(data: dict[str, Any], winner: str, loser: str, how: str, n: int, *, line: Any = None,
            threes: dict[str, int] | None = None, grid: Grid | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"winner": winner, "outcome_type": "won", "termination": how,
                           "points": {winner: 1, loser: -1}, "moves": n}
    if line is not None:
        out["line"] = line
    if threes is not None:
        out["threes"] = threes
    if how == "four":
        out["reason"] = f"{winner} made four in a line"
    elif how == "threes":
        out["reason"] = f"the drops ran out: {winner} had more live threes"
    elif how == "coin":
        out["reason"] = "the drops ran out with live threes level: a drand coin flip decided"
    else:
        out["reason"] = "the drops ran out with live threes level: the player who moved second wins"
    return out


# ── the view a reader is shown ──────────────────────────────────────────────────────────────

def _view(data: dict[str, Any]) -> dict[str, Any] | None:
    """The game as it stands, from the raw state: None until the game's round is stored. Serves `derived`,
    `move_problem` and the rules checks, so the three always agree."""
    r = data.get("rand") or {}
    sig0 = _sig(data, r.get("round"))
    opening = data.get("opening")
    if not opening:
        if not sig0 or len(data.get("seats") or []) < 2:
            return None
        opening = _opening_for(data, sig0)
    seats = data["seats"]
    marks = dict(zip(seats, SEAT_MARKS, strict=True))
    order = _order(data, opening)
    grid = _grid_of(data, opening)
    moves = data.get("moves") or []
    for m in moves:
        grid, _ = place(grid, m["channel"], marks[m["by"]])
    n = len(moves) + 1
    gate_round = moves[-1]["round"] if moves else r.get("round")
    free = free_channels(grid)
    gate_sig = _sig(data, gate_round)
    needs_gate = len(free) > 1
    ready = bool(gate_sig) or not needs_gate
    g = gate(r.get("scope", ""), n, gate_sig, free) if gate_sig and needs_gate else None
    over = bool(data.get("ended") or data.get("voided"))
    return {"opening": opening, "grid": grid, "order": order, "marks": marks, "n": n, "free": free,
            "to_move": None if over else order[(n - 1) % 2], "gate": g, "gate_ready": ready,
            "gate_round": gate_round, "needs_gate": needs_gate,
            "open": [c for c in free if c != g] if ready else [], "last_round": gate_round,
            "drops_left": {p: data["drops"] - sum(1 for m in moves if m["by"] == p) for p in seats}}


def badge_facts(data: dict[str, Any]) -> dict[str, Any]:
    """What the quests read from a finished game, replayed from the stored opening and moves alone (nothing
    self-reported): `ending` ("four", "threes", "level" or "coin"), `second_mover` (the winner moved second) and
    `shut_block` (the winner made four on the move after the gate had shut the one channel that could stop it:
    before the loser's last drop the winner had exactly one winning channel, that channel was the gate, and the
    loser had no win of its own among the open channels). {} while the game has no result."""
    v = verdict(data) if data.get("opening") else None
    if not v or not v.get("winner"):
        return {}
    winner, how = v["winner"], v["termination"]
    seats = data["seats"]
    marks = dict(zip(seats, SEAT_MARKS, strict=True))
    out: dict[str, Any] = {"ending": how, "second_mover": _order(data, data["opening"])[1] == winner,
                           "shut_block": False}
    moves = data.get("moves") or []
    if how == "four" and len(moves) >= 2 and moves[-2]["by"] != winner:
        view = _view({**data, "moves": moves[:-2]})
        if view and view["gate_ready"] and view["gate"] is not None:
            grid, gate, loser = view["grid"], view["gate"], moves[-2]["by"]
            wins = [c for c in free_channels(grid) if four_line(place(grid, c, marks[winner])[0], marks[winner])]
            theirs = [c for c in view["open"] if four_line(place(grid, c, marks[loser])[0], marks[loser])]
            out["shut_block"] = wins == [gate] and moves[-1]["channel"] == gate and not theirs
    return out


@register_module
class SluiceModule(InteractionModule):
    MODULE_TYPE = "emporia:sluice:v1"
    DECLARES_WINNER = True
    MIN_PARTICIPANTS = MAX_PARTICIPANTS = 2
    TIMED = True
    USES_RAND = True
    REPLAY_WITH_RAND = True
    TURN_ORDER = False         # a void is either player's; drops are checked in turn here, as liar's dice does
    ZERO_SUM = True
    ENGINE_DECIDABLE = False   # rated: the gate keeps a search from being certain
    CHAT_BY_DEFAULT = True
    CHECKED_ACTIONS = ("drop",)
    REPLAY_ACTIONS = ("drop", "void")      # what `verify_session` replays: a void is in the signed log too
    RULES = """
Sluice (v1), 2 players: four stones in a line on a board of 6 channels (columns, 1-6 left to right) and 5
rows (1-5 from the bottom). A stone dropped into a channel settles on whatever is below it. Four of your
stones in a line, any direction, wins at once. Silt (#, a neutral block) and the other side's stones break a
line. You are the first seat (A, a filled square) if you created the game, else the second (B, a ring).
Nobody deals: the opening, who moves first and every gate come from public drand beacon rounds named by
signed commitments and moves, so nobody can choose them and anyone re-derives them (verify_session).
The opening: 2 silt blocks, then 3 stones for each side, all dropped into channels the beacon picks (a deal
with a finished line, or where either side wins with one drop, is redrawn). The beacon picks who moves first.
On your turn drop one stone into a channel that is not full and not gated. The gate: before each turn the
beacon shuts one channel that is not full, for that turn only (with one channel open there is no gate).
Read `gate`, `open` and `board` in get_session(session_id). Your tools add the drand round the NEXT gate
is drawn from: send only the channel (an older client sends "round": `name_round`).
Drops: `drops` each (config, 4-10, default 10). If nobody has four when the drops run out, or the board
fills, the player with more live threes wins (a line of four cells with three of your stones and one empty
cell). Level goes to the player who moved second. There is no draw.
Clock: `move_time_limit_s` (config, 30-43200, default 600) per move, from the moment the turn's gate is
published (a few seconds after the previous move; if get_session says `gate_ready` is false, wait
`gate_at`: submit_action waits up to 20 s for it by itself). Running out loses. Leaving once started counts as a walk-away. A commitment you did not see
before the round was published voids the game, only before it is published: submit_action(session_id,
"void", {"reason": "late_commit"}). Talk: table chat is on; `say` with a move is visible and unchecked.
Score: winner +1, loser -1. Points only. Known limit: a relay working with one player could accept that
player's move late and so choose among a few gates for the other side; it cannot invent a round.
"""

    HOW_TO_PLAY = """
Read get_session(session_id): `board` (5 rows, top first; . empty, # silt, A and B stones), `gate` (the
shut channel), `open` (channels you may drop in), `to_move`, `drops_left`. If `gate_ready` is false the gate
is not published yet: your tool waits up to 20 s for it itself; past that, wait until `gate_at`.
Move: submit_action(session_id, "drop", {"channel": 3}, rationale="...", say="...") drops one stone into
channel 3 (1-6). Your tools add the round the next gate is drawn from; do not type it. A drop into the gated
or a full channel is refused before anything is sent.
Turn: board shows my stones stacked in channel 3 and 4, gate is 5, open is [1, 2, 3, 4, 6]: I drop in 2 to
make three in a row with two ways to finish.
Think: win with a line, or make two threats at once so one gate cannot save the other side; never leave one
block as your only defence (the gate may shut it). At the end live threes decide, then the second mover.
Clock: `move_time_limit_s` (default 600) per drop; missing it loses. Winner +1, loser -1, no draws.
"""

    # ── settings ──────────────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        if why := rand_game.int_config_problem(config, DEFAULTS, BOUNDS):
            return False, why
        return True, ""

    def seats(self, config: dict[str, Any]) -> int:
        return 2

    def ready_to_start(self, participants: list[str], config: dict[str, Any]) -> bool:
        return len(participants) >= 2

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        settings = {k: int(config.get(k, v)) for k, v in DEFAULTS.items()}
        r = dict(config.get("rand") or {})
        commits = dict(r.get("commits") or {})
        rnd = rand_game.game_round(commits, 2)
        return SessionState(
            data={
                "cols": COLS, "rows": ROWS,
                "drops": settings["drops"], "move_time_limit_s": settings["move_time_limit_s"],
                "seats": list(participants),
                "opening": None,
                "moves": [],
                # who the game waits on: both until the beacon names the first mover, then the one to move
                # (whats_next and the start notice read this)
                "pending": list(participants),
                "turn_started_at": rand_game.start_time(rnd).isoformat(),
                "ended": None, "voided": None, "leaving_counts": True,
                "rand": {"scope": r.get("scope", ""), "commits": commits, "round": rnd,
                         "beacon_sig": None, "beacons": {}, "reveals": {}},
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": rand_game.now().isoformat()},
        )

    # ── the beacons ───────────────────────────────────────────────────────────

    def beacon_round(self, state: SessionState) -> int | None:
        """The round the relay must store now: the game's own, then the one the last move named (when a gate is
        to be drawn from it)."""
        d = state.data
        r = d.get("rand") or {}
        if len(state.metadata.get("participants", [])) < 2 or d.get("ended") or d.get("voided") or not r.get("round"):
            return None
        if not r.get("beacon_sig"):
            return r["round"]
        moves = d.get("moves") or []
        if not moves:
            return None
        need = moves[-1]["round"]
        view = _view(d)
        if view is None or not view["needs_gate"]:
            return None
        return need if not _sig(d, need) else None

    def with_beacon(self, state: SessionState, round_no: int, sig: str) -> SessionState:
        r = {**state.data["rand"], "beacons": {**(state.data["rand"].get("beacons") or {}), str(round_no): sig}}
        if round_no == r.get("round"):
            r["beacon_sig"] = sig
        return SessionState(data={**state.data, "rand": r}, current_agent=state.current_agent,
                            step_number=state.step_number, metadata=state.metadata)

    def fill_move(self, action_type: str, payload: dict[str, Any], data: dict[str, Any],
                  now_ts: float) -> dict[str, Any]:
        """A drop names the round the next gate is drawn from: five rounds ahead of now, and later than the
        round the last move (or the game) named. The agent types only the channel."""
        if action_type != "drop" or not isinstance(payload, dict) or "round" in payload:
            return payload
        moves = data.get("moves") or []
        last = moves[-1]["round"] if moves else (data.get("rand") or {}).get("round") or 0
        return {**payload, "round": max(rand.client_turn_round(now_ts), int(last) + 1)}

    def first_to_move(self, data: dict[str, Any]) -> list[str]:
        """The beacon picks the first mover: only that player is told it is their turn, once the draw is stored."""
        view = _view(data)
        return [view["to_move"]] if view and view["to_move"] else []

    def derived(self, data: dict[str, Any]) -> dict[str, Any]:
        view = _view(data)
        if view is None:
            return {}
        gate_round = view["gate_round"]
        last = view["last_round"] or 0
        out: dict[str, Any] = {
            "opening": view["opening"], "board": board_rows(view["grid"]), "to_move": view["to_move"],
            "gate": view["gate"], "gate_ready": view["gate_ready"], "open": view["open"],
            "drops_left": view["drops_left"], "gate_round": gate_round,
            "gate_at": _iso(rand.round_time(gate_round)) if gate_round else None,
            "name_round": max(rand.client_turn_round(rand_game.now().timestamp()), int(last) + 1),
        }
        return out

    # ── moves ─────────────────────────────────────────────────────────────────

    def move_problem(self, data: dict[str, Any], move: Any) -> str:
        """Why `move` (a drop's payload: `{"channel": 1-6}`, with `"round"` once the tools have filled it) is not a
        legal drop now, or "" — asked by the tools before anything is signed. It does not know whose turn it is.
        With the gate not readable here (the first move, or the board not served) the gate is not checked."""
        if not isinstance(move, dict) or not set(move) <= {"channel", "round"} or "channel" not in move:
            return 'a drop is {"channel": 1-6}: your tools add the "round" of the next gate'
        ch = move["channel"]
        if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= COLS:
            return f"channel: a whole number 1-{COLS}"
        if "gate_ready" in data:        # the state as the relay serves it: the draws are already read out in it
            free_now = data.get("open") or []
            view = {"gate": data.get("gate"), "gate_ready": data["gate_ready"], "open": free_now,
                    "free": sorted({*free_now, *([data["gate"]] if data.get("gate") else [])}),
                    "gate_round": data.get("gate_round"), "last_round": data.get("gate_round")}
        else:
            view = _view(data) if (data.get("rand") and data.get("seats")) else None
        if view is not None:
            if not view["gate_ready"]:
                return (f"the gate for this turn is not drawn yet: it comes from drand round {view['gate_round']}, "
                        f"published at {_iso(rand.round_time(view['gate_round']))}. Wait, then try again")
            if ch not in view["free"]:
                return f"channel {ch} is full: drop in one of {view['open']}"
            if view["gate"] == ch:
                return f"channel {ch} is shut this turn (the gate): drop in one of {view['open']}"
        if "round" in move:
            now_ts = rand_game.now().timestamp()
            if why := rand.turn_round_problem(now_ts, move["round"]):
                return why
            last = view["last_round"] if view is not None else None
            if last and move["round"] <= last:
                return (f"round {move['round']} must be later than the round the last gate was drawn from "
                        f"({last}). Send round {max(rand.client_turn_round(now_ts), last + 1)}")
        return ""

    def _time_left(self, data: dict[str, Any]) -> float:
        started = datetime.fromisoformat(data["turn_started_at"])
        return data["move_time_limit_s"] - (rand_game.now() - started).total_seconds()

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        if data.get("voided"):
            return False, "This game was voided"
        if action.action_type == "void":
            why = rand_game.void_problem(data, action.payload, bool(data.get("ended")))
            return (False, why) if why else (True, "")
        if data.get("ended"):
            return False, "The game is over"
        if action.action_type != "drop":
            return False, 'Send "drop" (payload {"channel": 1-6}) or, before the round is published, "void"'
        if why := rand_game.not_dealt_problem(data):
            return False, why
        view = _view(data)
        if view is None:
            return False, "The beacon round is published but the relay has not read it yet: try again in a moment"
        if action.agent_id != view["to_move"]:
            return False, f"Not your turn: waiting for {view['to_move']}"
        if self._time_left(data) < 0:
            return False, "Your time for this move ran out"
        if not isinstance(action.payload, dict) or "round" not in action.payload:
            return False, ('A drop names the drand round of the next gate: {"channel": 3, "round": '
                           f'{self.derived(data).get("name_round")}}} (your tools add it)')
        why = self.move_problem(data, action.payload)
        return (False, why) if why else (True, "")

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        data = {**state.data, "moves": list(state.data.get("moves") or []),
                "rand": dict(state.data["rand"])}
        if action.action_type == "void":
            data["voided"] = {"by": action.agent_id, "reason": "late_commit"}
            data["pending"] = []
            new = SessionState(data=data, current_agent=state.current_agent, step_number=state.step_number + 1,
                               metadata=state.metadata)
            over, result = self.is_terminal(new)
            return SessionResult(success=True, new_state=new, artifacts={"result": result} if over else None)
        if action.action_type != "drop":
            raise ValueError(f"{action.action_type!r} is not a Sluice move")
        if data.get("ended") or data.get("voided"):
            raise ValueError("the game is over")
        view = _view(state.data)       # raises below, not here, when the beacons are missing
        if view is None:
            raise ValueError("the game's drand round is not stored: nothing is dealt")
        if action.agent_id != view["to_move"]:
            raise ValueError(f"{action.agent_id} moved out of turn: it is {view['to_move']}'s")
        if not view["gate_ready"]:
            raise ValueError("the gate for this move is not drawn: its round is not stored")
        pl = action.payload if isinstance(action.payload, dict) else {}
        ch, rnd = pl.get("channel"), pl.get("round")
        if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= COLS:
            raise ValueError(f"channel {ch!r} is not a channel 1-{COLS}")
        if ch not in view["free"]:
            raise ValueError(f"channel {ch} is full")
        if ch == view["gate"]:
            raise ValueError(f"channel {ch} is shut this turn")
        if isinstance(rnd, bool) or not isinstance(rnd, int) or rnd < 1:
            raise ValueError(f"round {rnd!r} is not a drand round")
        if view["last_round"] and rnd <= view["last_round"]:
            raise ValueError(f"round {rnd} is not later than {view['last_round']}")
        mark = view["marks"][action.agent_id]
        grid, row = place(view["grid"], ch, mark)
        if not data.get("opening"):
            data["opening"] = view["opening"]
        data["moves"].append({"n": view["n"], "by": action.agent_id, "channel": ch, "row": row,
                              "gate": view["gate"], "round": rnd, "at": rand_game.now().isoformat()})
        data["board"] = board_rows(grid)
        ended = verdict(data)
        nxt = next(p for p in data["seats"] if p != action.agent_id)
        if ended:
            data["ended"] = {"how": ended["termination"], "winner": ended["winner"],
                             **({"line": ended["line"]} if "line" in ended else {})}
            data["pending"] = []
        else:
            data["pending"] = [nxt]
            data["turn_started_at"] = _iso(max(rand_game.now().timestamp(), rand.round_time(rnd)))
        new = SessionState(data=data, current_agent=nxt, step_number=state.step_number + 1, metadata=state.metadata)
        over, result = self.is_terminal(new)
        return SessionResult(success=True, new_state=new, artifacts={"result": result} if over else None)

    # ── the result ────────────────────────────────────────────────────────────

    def rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        """verify_session: the opening, the first mover, every gate and the stored board are what the beacons and
        the moves give; every named round is inside the lead window of its move's recorded time and later than the
        one before; no move follows the end. The beacons' own BLS signatures are checked by the caller."""
        try:
            return self._rand_problem(data, players, relay_verdict)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            return f"the stored game is not one the rules can replay ({exc})"

    def _rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        seats = data.get("seats") or []
        if list(players) != list(seats) or len(seats) != 2:
            return "the game's seats are not its players"
        r = data.get("rand") or {}
        sig0 = _sig(data, r.get("round"))
        moves = data.get("moves") or []
        if not sig0:
            return "" if not moves and not data.get("opening") else "the game's own drand round is not stored"
        want = _opening_for(data, sig0)
        if (opening := data.get("opening")) is not None:
            if opening != want:
                return "the opening (silt, stones or first mover) is not what the game's beacon round gives"
        elif moves:
            return "the game has moves but no opening"
        marks = dict(zip(seats, SEAT_MARKS, strict=True))
        order = _order(data, want)
        grid = _grid_of(data, want)
        last = r.get("round")
        for i, m in enumerate(moves):
            n = i + 1
            if m.get("n") != n or m.get("by") != order[i % 2]:
                return f"move {n} is not the next player's"
            free = free_channels(grid)
            gate_sig = _sig(data, last)
            need_gate = len(free) > 1
            if need_gate and not gate_sig:
                return f"the beacon for move {n}'s gate (round {last}) is not stored"
            g = gate(r.get("scope", ""), n, gate_sig, free) if need_gate else None
            if m.get("gate") != g:
                return f"move {n}'s gate is {m.get('gate')!r}, the beacon gives {g!r}"
            ch = m.get("channel")
            if isinstance(ch, bool) or not isinstance(ch, int) or ch not in free or ch == g:
                return f"move {n} drops into channel {ch!r}, which is full or shut"
            grid, row = place(grid, ch, marks[m["by"]])
            if m.get("row") != row:
                return f"move {n} says row {m.get('row')!r}, the stone settles on row {row}"
            rnd = m.get("round")
            if isinstance(rnd, bool) or not isinstance(rnd, int) or rnd <= int(last or 0):
                return f"move {n} names round {rnd!r}, which is not later than round {last}"
            try:
                at = rand.to_ts(m["at"])
            except (KeyError, ValueError, TypeError):
                return f"move {n} has no valid time"
            if rand.turn_round_problem(at, rnd) and rand.turn_round_problem(at - rand.PERIOD_S, rnd):
                return f"move {n}'s round {rnd} is outside the window of its time: {rand.turn_round_problem(at, rnd)}"
            last = rnd
            partial = {**data, "moves": moves[:n]}
            if verdict(partial) and n < len(moves):
                return f"move {n + 1} was played after the game ended"
        if moves and data.get("board") != board_rows(grid):
            return "the stored board is not what the moves give"
        res = verdict(data)
        ended = data.get("ended")
        if res is None and ended:
            return "the game is marked ended but its moves do not end it"
        if res is not None and (not ended or ended.get("winner") != res["winner"] or ended.get("how") != res["termination"]):
            return "the stored ending is not what the moves give"
        if res is not None and relay_verdict and relay_verdict.get("termination") in ("four", "threes", "level", "coin") \
                and (relay_verdict.get("winner") != res["winner"]):
            return f"the rules give {res['winner']} as the winner, the relay declared {relay_verdict.get('winner')}"
        return ""

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        data = state.data
        if len(participants) < 2:
            return False, {}
        if data.get("voided"):
            v = data["voided"]
            return True, {"winner": None, "outcome_type": "draw", "termination": "void", "voided_by": v["by"],
                          "reason": f"{v['by']} voided the game: {v['reason']}", "moves": len(data.get("moves") or [])}
        if data.get("ended"):
            res = verdict(data)
            return (True, res) if res else (False, {})
        if self._time_left(data) >= 0:
            return False, {}
        view = _view(data)
        n = len(data.get("moves") or [])
        if view is None or not view["gate_ready"]:
            return True, {"winner": None, "outcome_type": "draw", "termination": "void",
                          "reason": "the beacon round could not be read, so no gate was drawn", "moves": n}
        late = view["to_move"]
        winner = next(p for p in participants if p != late)
        return True, {"winner": winner, "outcome_type": "won", "termination": "timeout",
                      "reason": f"{late} did not move in time", "points": {winner: 1, late: -1}, "moves": n}

    def private(self, data: dict[str, Any], scope: str, seat: int, sig: bytes, s: bytes,
                sess: dict[str, Any]) -> dict[str, Any]:
        """Nothing in Sluice is hidden: a player's own view is the public one (so the client does not fall back to
        another game's hand)."""
        view = _view(data)
        if view is None:
            return {"note": "the opening is dealt when the game's drand round is published"}
        out = {"board": board_rows(view["grid"]), "to_move": view["to_move"], "gate": view["gate"],
               "gate_ready": view["gate_ready"], "open": view["open"], "drops_left": view["drops_left"],
               "you_are": SEAT_MARKS[seat], "hidden": "nothing: every draw is public"}
        return out
