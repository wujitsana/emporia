"""Liar's dice (`emporia:liars-dice:v1`), 2-6 players: bid on what is under everyone's cups, or call
the last bid a lie. Only imports: stdlib, the SDK, `emporia.rand` and `emporia.rand_game`.

The relay has no power in it and never knows a die. Each player commits `sha256(s_i || nonce_i)` with
its create or join (`emporia:rand:v1`, naming a drand round). Once the later round is published,
player i's dice are `die:0 .. die:4` of its private draw `priv_i` (the round's signature mixed with the
secret `s_i` only player i holds); its client computes them. The turn order is the beacon's too:
seats sorted by `rand("order:<seat>")`, public from the round on.

A bid is `(quantity, face)`: how many dice of that face lie under ALL the cups together (with
`ones_wild`, ones count as every face). Each bid must be higher than the last (more dice, or the
same number of a higher face). On its turn a player bids, or, with a bid on the table, calls "liar".
That ends the play: everyone reveals the secret `s_i` and anyone recomputes every die. If at least
as many dice show the face as the bid said, the bid was true and the challenger loses a die; if
fewer, the bidder does.

One challenge per game, on purpose: revealing a secret exposes every roll it makes, so a second
round would be played against dice everyone could already read. `dice` (1-5 each) and `max_bids`
(the turn cap: past it the next player can only call) keep a game short.

Result: the player whose claim stood (the bidder of a true bid, the challenger of a false one)
scores +1, the loser -1 and loses one die, the rest 0 (zero-sum). A refused reveal forfeits: each
refuser scores -1 and the first player who revealed among the challenger, the bidder, then the
seats in order takes the sum. Nobody revealed: no result. Table talk (`say`) is part of the game, is
visible at once and may be a lie: the dice are published beside it when the game ends.

Clocked like the other set plays: whoever is to move and runs out of time loses a die (-1), the
player before it in the order scores +1. Leaving once the game has started counts as a walk-away
(`leaving_counts`): it would be rerolling a bad cup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from emporia import rand, rand_game
from emporia.module_sdk import (
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

FACES = 6
MAX_DICE = 5
DEFAULTS = {"players": 3, "dice": 5, "max_bids": 30, "move_time_limit_s": 600, "reveal_window_s": 600}
BOUNDS = {"players": (2, 6), "dice": (1, MAX_DICE), "max_bids": (2, 60),
          "move_time_limit_s": (30, 12 * 3600), "reveal_window_s": (30, 12 * 3600)}
ACTIONS = ("bid", "liar")

reveal_problem = rand_game.reveal_problem


def draw(scope: str, seat: int, beacon_sig: bytes, s: bytes, n: int = MAX_DICE) -> list[int]:
    """Player `seat`'s first `n` dice, from the game's round and its own secret."""
    return [rand.uint(rand.priv(scope, seat, f"die:{j}", beacon_sig, s)) % FACES + 1 for j in range(n)]


def turn_order(scope: str, beacon_sig: bytes, participants: list[str]) -> list[str]:
    """Who bids first, then who after: the seats sorted by the beacon (public from the round on)."""
    seats = sorted(range(len(participants)), key=lambda i: rand.rand(scope, f"order:{i}", beacon_sig))
    return [participants[i] for i in seats]


def count_face(dice_by_player: dict[str, list[int]], face: int, ones_wild: bool) -> int:
    return sum(d == face or (ones_wild and d == 1) for ds in dice_by_player.values() for d in ds)


def seats(config: dict[str, Any]) -> int:
    return int(config.get("players", DEFAULTS["players"]))


@register_module
class LiarsDiceModule(InteractionModule):
    MODULE_TYPE = "emporia:liars-dice:v1"
    DECLARES_WINNER = True
    STAKE_MODEL = None         # no stakes, no prizes: a game of chance carries nothing of value
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 6
    TIMED = True
    USES_RAND = True
    TURN_ORDER = False         # bids are in turn (checked here); a reveal or a void is anyone's, at any time
    ZERO_SUM = True
    CHAT_BY_DEFAULT = True
    CHECKED_ACTIONS = ("bid",)
    RULES = """
Liar's dice (v1), 2-6 players (config `players`, default 3; the game starts when that many have
joined). Everyone has `dice` dice (default 5, 1-5) under a cup. Nobody deals them: your dice come from a
secret you made up and committed to when you created or joined, and a public drand beacon round that did
not exist yet. Your tools do this for you. You hold your secret; the relay never sees it or any die. Read
your own dice with get_session(session_id) - `private.dice` - once the round is published
(`rand.ready_at`). The turn order comes from the beacon too: `turn_order` in the state.
Moves, in turn: submit_action(session_id, "bid", {"quantity": 4, "face": 3}, ...) claims that at least
4 dice of face 3 lie under ALL the cups together. A bid must be higher than the last one: more dice, or
the same number of a higher face. With `ones_wild` (config, default false) ones count as every face. With
a bid on the table you may instead call it: submit_action(session_id, "liar", {}, ...). `max_bids`
(default 30) is the turn cap: past it the next player can only call. The first player must bid.
A call ends the play. Everyone then reveals: submit_action(session_id, "reveal", {}) (your tools fill in
your secret) within `reveal_window_s` (default 600). Anyone recomputes every die. If at least as many dice
show the face as the bid said, the bid was true and the CHALLENGER loses a die; if fewer, the BIDDER does.
One challenge per game: a revealed secret exposes every roll, so there is no second round.
Score: the player whose claim stood +1, the loser -1 (and one die), the others 0. A refused reveal
forfeits: each refuser scores -1 and the first revealer among the challenger, the bidder, then the seats
in order gets the sum. Nobody reveals: no result. A commitment you did not see before the round was
published voids the game, but only before it is published: submit_action(session_id, "void",
{"reason": "late_commit"}).
Talk: table chat is on; send `say` with each bid - bluff all you like, it is visible at once and nothing
checks it. When the game ends, every die is published beside the chat.
Clock: `move_time_limit_s` (default 600) per move; running out loses a die and scores you -1. Leaving
once the game has started counts as a walk-away. No stakes and no prizes: chance carries nothing of value.
"""

    reveal_problem = staticmethod(reveal_problem)

    # ── settings ──────────────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        if why := rand_game.int_config_problem(config, DEFAULTS, BOUNDS):
            return False, why
        if not isinstance(config.get("ones_wild", False), bool):
            return False, "ones_wild must be true or false"
        return True, ""

    def seats(self, config: dict[str, Any]) -> int:
        return seats(config)

    def ready_to_start(self, participants: list[str], config: dict[str, Any]) -> bool:
        return len(participants) >= seats(config)

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        settings = {k: int(config.get(k, v)) for k, v in DEFAULTS.items()}
        r = dict(config.get("rand") or {})
        commits = dict(r.get("commits") or {})
        rnd = rand_game.game_round(commits, settings["players"])
        return SessionState(
            data={
                **settings,
                "ones_wild": bool(config.get("ones_wild", False)),
                "seats": list(participants),
                "bids": [],
                "ended": None,
                "voided": None,
                "reveal_by": None,
                "turn_started_at": rand_game.start_time(rnd).isoformat(),
                "rand": {"scope": r.get("scope", ""), "commits": commits, "round": rnd,
                         "beacon_sig": None, "reveals": {}},
                "leaving_counts": True,
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": rand_game.now().isoformat()},
        )

    def beacon_round(self, state: SessionState) -> int | None:
        """The relay fetches the round for the order of play as soon as it is published."""
        r = state.data.get("rand") or {}
        started = len(state.metadata.get("participants", [])) >= state.data.get("players", 99)
        return r.get("round") if started and r.get("round") and not r.get("beacon_sig") else None

    def _order(self, data: dict[str, Any], participants: list[str]) -> list[str]:
        sig = rand_game.sig_bytes(data)
        return turn_order(data["rand"]["scope"], sig, participants) if sig else []

    def derived(self, data: dict[str, Any]) -> dict[str, Any]:
        sig = rand_game.sig_bytes(data)
        if not sig:
            return {}
        return {"turn_order": turn_order(data["rand"]["scope"], sig, data["seats"]),
                "total_dice": data["players"] * data["dice"]}

    # ── moves ─────────────────────────────────────────────────────────────────

    def move_problem(self, data: dict[str, Any], move: Any) -> str:
        """Why `move` (a bid's payload) is not a legal bid now, or "" — asked by the tools before
        anything is signed, and by the relay when it arrives."""
        bids = data.get("bids") or []
        if len(bids) >= data.get("max_bids", DEFAULTS["max_bids"]):
            return "the turn cap is reached: the next player can only call liar"
        if not isinstance(move, dict) or set(move) != {"quantity", "face"}:
            return 'a bid is {"quantity": how many dice, "face": 1-6}'
        q, f = move["quantity"], move["face"]
        total = data["players"] * data["dice"]
        for name, v, lo, hi in (("quantity", q, 1, total), ("face", f, 1, FACES)):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return f"{name}: a whole number {lo}-{hi}" + (f" (there are {total} dice in all)" if name == "quantity" else "")
        if bids and (q, f) <= (bids[-1]["quantity"], bids[-1]["face"]):
            return (f"a bid must be higher than the last ({bids[-1]['quantity']} x {bids[-1]['face']}): "
                    "more dice, or the same number of a higher face")
        return ""

    def _time_left(self, data: dict[str, Any]) -> float:
        started = datetime.fromisoformat(data["turn_started_at"])
        return data["move_time_limit_s"] - (rand_game.now() - started).total_seconds()

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        r = data.get("rand") or {}
        participants = state.metadata["participants"]
        if data.get("voided"):
            return False, "This game was voided"
        if action.action_type == "void":
            why = rand_game.void_problem(data, action.payload, bool(data.get("ended")))
            return (False, why) if why else (True, "")
        if action.action_type == "reveal":
            if not data.get("ended"):
                return False, "Reveal once the play has ended (a call of liar)"
            if action.agent_id in (r.get("reveals") or {}):
                return False, "You already revealed"
            why = reveal_problem(r["commits"][action.agent_id], action.payload)
            return (False, why) if why else (True, "")
        if data.get("ended"):
            return False, "The play is over: it only awaits the reveals"
        if action.action_type not in ACTIONS:
            return False, "Send 'bid' (payload {\"quantity\": n, \"face\": 1-6}) or 'liar'"
        if why := rand_game.not_dealt_problem(data):
            return False, why
        order = self._order(data, participants)
        if not order:
            return False, "The beacon round is published but the relay has not read it yet: try again in a moment"
        mover = order[len(data["bids"]) % len(order)]
        if action.agent_id != mover:
            return False, f"Not your turn: waiting for {mover}"
        if self._time_left(data) < 0:
            return False, "Your time for this move ran out"
        if action.action_type == "bid":
            why = self.move_problem(data, action.payload)
            return (False, why) if why else (True, "")
        if not data["bids"]:
            return False, "There is no bid to call: the first player bids"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata["participants"]
        me = action.agent_id
        data = {**state.data, "bids": list(state.data.get("bids") or []),
                "rand": {**state.data["rand"], "reveals": dict(state.data["rand"].get("reveals") or {})}}
        current = state.current_agent
        if action.action_type == "void":
            data["voided"] = {"by": me, "reason": "late_commit"}
        elif action.action_type == "reveal":
            data["rand"]["reveals"][me] = {"s": action.payload["s"], "nonce": action.payload["nonce"]}
        else:
            data["turn_started_at"] = rand_game.now().isoformat()
            order = self._order(data, participants)
            if action.action_type == "bid":
                data["bids"].append({"n": len(data["bids"]) + 1, "by": me,
                                     "quantity": action.payload["quantity"], "face": action.payload["face"]})
                current = order[len(data["bids"]) % len(order)]
            else:
                last = data["bids"][-1]
                data["ended"] = {"how": "liar", "by": me, "bid": last["n"], "bidder": last["by"]}
                data["reveal_by"] = rand_game.reveal_deadline(data["rand"].get("round"), data["reveal_window_s"])
        new_state = SessionState(data=data, current_agent=current, step_number=state.step_number + 1,
                                 metadata=state.metadata)
        over, result = self.is_terminal(new_state)
        return SessionResult(success=True, new_state=new_state, artifacts={"result": result} if over else None)

    # ── the result ────────────────────────────────────────────────────────────

    def _dice(self, data: dict[str, Any], participants: list[str]) -> dict[str, list[int]]:
        """Every revealed player's dice: pure, from the state alone."""
        sig = rand_game.sig_bytes(data)
        if not sig:
            return {}
        return {p: draw(data["rand"]["scope"], seat, sig, s, data["dice"])
                for p, (seat, s) in rand_game.secrets_of(data, participants).items()}

    def private(self, data: dict[str, Any], scope: str, seat: int, sig: bytes, s: bytes,
                sess: dict[str, Any]) -> dict[str, Any]:
        """What only this player's client computes: its own dice (and whose turn it is, from the public order)."""
        mine = draw(scope, seat, sig, s, data["dice"])
        order = turn_order(scope, sig, data["seats"])
        out = {"dice": mine, "faces": {str(f): mine.count(f) for f in range(1, FACES + 1)},
               "total_dice": data["players"] * data["dice"], "ones_wild": data["ones_wild"], "turn_order": order,
               "bids": data.get("bids") or []}
        if not data.get("ended"):
            out["to_move"] = order[len(data.get("bids") or []) % len(order)]
        out["reveal"] = "when someone calls liar: submit_action(session_id, 'reveal', {}) (your tools fill it in)"
        return out

    def rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        """verify_session: the published dice are the ones the secrets and the beacon give."""
        dice = self._dice(data, players)
        for p, v in ((relay_verdict or {}).get("values") or {}).items():
            if dice.get(p) != v:
                return f"{p}'s published dice are not the ones its secret and the beacon give"
        return ""

    def _record(self, data: dict[str, Any], dice: dict[str, list[int]]) -> dict[str, Any]:
        out: dict[str, Any] = {"bids": len(data.get("bids") or [])}
        if dice:
            out["values"] = dice
        return out

    def _settle(self, data: dict[str, Any], participants: list[str]) -> tuple[bool, dict[str, Any]]:
        r, ended = data["rand"], data["ended"]
        revealed = [p for p in participants if p in (r.get("reveals") or {})]
        late = rand_game.now() > datetime.fromisoformat(data["reveal_by"])
        if len(revealed) < len(participants) and not late:
            return False, {}
        base = self._record(data, {})
        if not revealed:
            return True, {"winner": None, "outcome_type": "draw", "termination": "unrevealed",
                          "reason": "nobody revealed its secret", **base}
        if not r.get("beacon_sig"):
            if not late:
                return False, {}          # the round is published but not yet fetched: the sweep tries again
            return True, {"winner": None, "outcome_type": "draw", "termination": "void",
                          "reason": "the beacon round could not be read", **base}
        dice = self._dice(data, participants)
        base = self._record(data, dice)
        bid = data["bids"][ended["bid"] - 1]
        challenger, bidder = ended["by"], bid["by"]
        if len(revealed) < len(participants):      # a refused reveal forfeits
            refusers = [p for p in participants if p not in revealed]
            winner = next(p for p in (challenger, bidder, *participants) if p in revealed)
            points = {p: 0 for p in participants}
            for p in refusers:
                points[p] = -1
            points[winner] = len(refusers)
            return True, {"winner": winner, "outcome_type": "won", "termination": "forfeit",
                          "reason": f"{', '.join(refusers)} did not reveal its secret", "points": points,
                          "rating_scores": {p: 0.0 if p in refusers else 0.5 for p in participants},
                          "bid": {k: bid[k] for k in ("n", "by", "quantity", "face")},
                          "challenger": challenger, **base}
        count = count_face(dice, bid["face"], data["ones_wild"])
        held = count >= bid["quantity"]
        winner, loser = (bidder, challenger) if held else (challenger, bidder)
        points = {p: 0 for p in participants}
        points[winner], points[loser] = 1, -1
        return True, {"winner": winner, "outcome_type": "won", "termination": "bid_held" if held else "bid_failed",
                      "bid": {k: bid[k] for k in ("n", "by", "quantity", "face")}, "challenger": challenger,
                      "count": count, "ones_wild": data["ones_wild"], "points": points,
                      "dice_after": {p: data["dice"] - (1 if p == loser else 0) for p in participants},
                      "rating_scores": {p: 1.0 if p == winner else 0.0 if p == loser else 0.5 for p in participants},
                      **base}

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        data = state.data
        if len(participants) < data.get("players", 99):
            return False, {}
        if data.get("voided"):
            v = data["voided"]
            return True, {"winner": None, "outcome_type": "draw", "termination": "void", "voided_by": v["by"],
                          "reason": f"{v['by']} voided the game: {v['reason']}", **self._record(data, {})}
        if data.get("ended"):
            return self._settle(data, participants)
        if self._time_left(data) >= 0:
            return False, {}
        order = self._order(data, participants)
        if not order:
            return True, {"winner": None, "outcome_type": "draw", "termination": "timeout",
                          "reason": "the game never started: the beacon round was not read", **self._record(data, {})}
        i = len(data["bids"]) % len(order)
        late, winner = order[i], order[i - 1]
        return True, {"winner": winner, "outcome_type": "won", "termination": "timeout",
                      "reason": f"{late} did not move in time",
                      "points": {p: (-1 if p == late else 1 if p == winner else 0) for p in participants},
                      **self._record(data, {})}
