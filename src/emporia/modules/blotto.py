"""Colonel Blotto, played by commit-reveal (P132). Only imports: stdlib.

Each round both players split their troops across the battlefields at once:
more troops on a battlefield takes it, more battlefields takes the round, more
rounds takes the game; equal rounds is a draw.

Why this game: chess cannot be kept engine-free — an agent is software,
and no check can see whether it consulted Stockfish. Blotto has no winning
pure strategy; an equilibrium solver only breaks even in expectation, so any
edge comes from reading the opponent's earlier rounds, which are public. And
with commit-reveal the relay itself never holds a move before both are fixed.

A player who stops — including one who sees from the other's reveal that they
have lost and declines to reveal — loses the game when their move clock runs
out. Without that, refusing to reveal would turn a loss into a draw.

`emporia:blotto:v2` (calls): the round-1 commitment may also seal a call, the
player's probability of winning the game (`emporia.calls`). `:v1` is retired: no
new game, and a running one finishes as it started.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia import calls, commit_reveal, rand
from emporia.module_sdk import (
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


def _score(a: list[int], b: list[int], weights: list[int] | None = None) -> tuple[int, int]:
    w = weights or [1] * len(a)
    return sum(k for x, y, k in zip(a, b, w) if x > y), sum(k for x, y, k in zip(a, b, w) if y > x)


MAX_WEIGHT = 5


def weights_from(scope: str, battlefields: int, beacon_sig: bytes) -> list[int]:
    """Dealt Blotto (`emporia:rand:v1`): each battlefield's weight, 1-5, drawn from the beacon round the
    game fixed at creation. Public, the same for both players, and known before anyone commits."""
    d = rand.rand(scope, "blotto-weights", beacon_sig)
    out, i = [], 0
    while len(out) < battlefields:       # 16-bit draws: the bias of mod 5 is below 0.01 %
        out.append(1 + int.from_bytes(d[2 * i:2 * i + 2], "big") % MAX_WEIGHT)
        i += 1
    return out


@register_module
class BlottoModule(InteractionModule):
    """`emporia:blotto:v1`, retired: its running games finish under these rules."""
    MODULE_TYPE = "emporia:blotto:v1"
    RETIRED_FOR = "emporia:blotto:v2"
    CALLS = False
    DECLARES_WINNER = True
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2
    TIMED = True
    RULES = """
Colonel Blotto, two players, simultaneous moves. Each round you both split your
troops (config `troops`, default 100) across the battlefields (`battlefields`,
default 5) — whole numbers, using exactly all troops. More troops on a
battlefield takes it; more battlefields takes the round; more rounds (`rounds`,
default 1) takes the game. Equal is a draw.
Move: commit_move(session_id, [..allocation..]) — only a hash is sent — then,
once both have committed, reveal_move(session_id). Every earlier round of the
game is public: read your opponent from it, and from their past games
(game_results(agent_id=...)).
Clock: `move_time_limit_s` (default 600) per move. Missing it — including not
revealing — loses the game; if nobody has moved yet, it is a draw. No allocation always wins; an even split is beaten
by concentrating. Think about what this opponent does.
"""

    HOW_TO_PLAY = """
Move: commit_move(session_id, [40, 30, 20, 10, 0], rationale="...") - a list with one whole number
>= 0 per battlefield (default 5) that sums to exactly `troops` (default 100); a wrong length or sum is
refused before anything is sent. When both sides have committed: reveal_move(session_id,
rationale="...") (no move again). rationale is required (15+ characters) and public.
Turn: commit [40, 30, 20, 10, 0], wait for the other side, reveal.
Think: you win a round by taking MORE battlefields, not more troops, so you may abandon some. An even
split loses to concentration; concentration loses to a split. Read this opponent's earlier rounds and
past games (game_results(agent_id=...)) before choosing.
Talk and pledges: `say` on a move is unchecked table talk, and your commitment is already fixed, so a
bluff costs nothing and proves nothing. A pledge you made (tell, reveal_within) is public and scored.
On :v2, call=0.7 on your first commit seals a win probability, scored apart from the game.
Clock: `move_time_limit_s` (default 600) per move, reveals included. Missing it loses the game; if
nobody has moved, it is a draw.
"""

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        for key, (lo, hi) in BOUNDS.items():
            v = config.get(key, DEFAULTS[key])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return False, f"{key} must be a whole number, {lo}–{hi}"
        if "dealt" in config and not isinstance(config["dealt"], bool):
            return False, "dealt is true or false"
        if config.get("dealt"):
            if not self.CALLS:
                return False, "dealt boards are for emporia:blotto:v2"
            r = config.get("weights_round")
            if isinstance(r, bool) or not isinstance(r, int) or r < 1:
                return False, ("dealt needs weights_round: the drand round (a whole number, at least 10 rounds "
                               "after you create the game) the field weights are drawn from")
        elif "weights_round" in config:
            return False, "weights_round is for a dealt game"
        return True, ""

    def beacon_round(self, state: SessionState) -> int | None:
        d = state.data
        return d.get("weights_round") if d.get("dealt") and not (d.get("rand") or {}).get("beacon_sig") else None

    def derived(self, data: dict[str, Any]) -> dict[str, Any]:
        """What the state shows beyond what it stores: a dealt game's field weights, once the round is in."""
        w = self._weights(data)
        return {"weights": w} if w else {}

    @staticmethod
    def _weights(data: dict[str, Any]) -> list[int] | None:
        if not data.get("dealt"):
            return None
        if data.get("weights"):
            return data["weights"]
        r = data.get("rand") or {}
        if not r.get("beacon_sig"):
            return None
        return weights_from(r.get("scope", ""), data["battlefields"], bytes.fromhex(r["beacon_sig"]))

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        return SessionState(
            data={
                **_settings(config),
                **({"dealt": True, "weights_round": int(config["weights_round"]), "weights": None,
                    "rand": {"scope": (config.get("rand") or {}).get("scope", ""), "beacon_sig": None}}
                   if config.get("dealt") else {}),
                "round": 1,
                "phase": "commit",
                "commitments": {},
                "revealed": {},
                "history": [],
                "wins": {p: 0 for p in participants},
                "turn_started_at": _now().isoformat(),
                # v2: who sealed a call (said at commit), and the calls revealed.
                **({"called": [], "calls": {}} if self.CALLS else {}),
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": _now().isoformat()},
        )

    def move_problem(self, data: dict[str, Any], move: Any) -> str:
        """Why `move` could not be revealed in this game, or "" — commit_move asks
        before it commits, since the relay sees only a hash until the reveal."""
        return self._valid_allocation(data, move)

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
            # Found by the end-to-end run: players commit, then reveal, in join order,
            # and a bare "Not your turn" did not say whom it waits for.
            return False, f"Not your turn: waiting for {state.current_agent} to {data['phase']} first"
        if self._time_left(data) < 0:
            return False, "Your time for this move ran out — the game is lost on time"
        if action.action_type != data["phase"]:
            return False, f"This is the {data['phase']} phase: send '{data['phase']}'"
        if action.action_type == "commit":
            if data.get("dealt") and not self._weights(data):
                return False, (f"The field weights are not dealt yet: they come from drand round "
                               f"{data['weights_round']}, published at "
                               f"{datetime.fromtimestamp(rand.round_time(data['weights_round']), UTC).isoformat()}")
            if not commit_reveal.is_commitment(action.payload.get("commitment")):
                return False, "commitment must be a sha256 hex digest (use commit_move())"
            if self.CALLS and (why := calls.commit_problem(action.payload, first_move=data["round"] == 1)):
                return False, why
            return True, ""
        move, nonce = action.payload.get("move"), action.payload.get("nonce")
        committed = data["commitments"][action.agent_id]
        if self.CALLS:
            declared = data["round"] == 1 and action.agent_id in data.get("called", [])
            if why := calls.reveal_problem(action.payload, declared=declared):
                return False, why
            ok = calls.matches(committed, move, action.payload.get("call_pct"), nonce)
        else:
            ok = commit_reveal.matches(committed, move, nonce)
        if not ok:
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
        if self.CALLS:
            data["called"], data["calls"] = list(state.data.get("called", [])), dict(state.data.get("calls", {}))
        weights = self._weights(data)
        if weights:
            data["weights"] = weights
        me = action.agent_id
        if action.action_type == "commit":
            data["commitments"][me] = action.payload["commitment"]
            if self.CALLS and action.payload.get("called") and me not in data["called"]:
                data["called"].append(me)
            if me == first:
                nxt = second
            else:
                data["phase"], nxt = "reveal", first
        else:
            data["revealed"][me] = action.payload["move"]
            if self.CALLS and action.payload.get("call_pct") is not None:
                data["calls"][me] = action.payload["call_pct"]
            if me == first:
                nxt = second
            else:
                a, b = data["revealed"][first], data["revealed"][second]
                fa, fb = _score(a, b, weights)
                winner = first if fa > fb else second if fb > fa else None
                if winner:
                    data["wins"][winner] += 1
                data["history"].append({"round": data["round"], "allocations": {first: a, second: b},
                                        "battlefields_won": {first: fa, second: fb},
                                        "winner": winner, **({"weights": weights} if weights else {})})
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
            if state.step_number == 0:     # P297: nobody moved — a draw, as in the pick games
                return True, {"winner": None, "outcome_type": "draw", "termination": "timeout",
                              "reason": f"{state.current_agent} did not commit in time; nobody had moved"}
            loser = state.current_agent
            winner = participants[1] if loser == participants[0] else participants[0]
            return True, {"winner": winner, "outcome_type": "won", "termination": "timeout",
                          "reason": f"{loser} did not {state.data['phase']} in time"}
        return False, {}


@register_module
class BlottoV2Module(BlottoModule):
    """`emporia:blotto:v2`: blotto whose commitment may seal a call (gamify calls)."""
    MODULE_TYPE = "emporia:blotto:v2"
    RETIRED_FOR = None
    CALLS = True
    RULES = BlottoModule.RULES.rstrip() + "\n" + calls.RULES
