"""Common-value auction (`emporia:cv-auction:v1`), 3-8 players: one pot worth the same to everybody,
which nobody knows exactly. Bid for it blind and mind the winner's curse. Only imports: stdlib, the
SDK, `emporia.rand`, `emporia.rand_game` and `emporia.commit_reveal`.

The relay has no power in it and never knows a signal. Each player commits `sha256(s_i || nonce_i)`
with its create or join (`emporia:rand:v1`, naming a drand round). Once the latest round is published:

    base   B   = 50 + uint(rand("base", beacon)) mod 101              public, from the round alone
    part   a_i = uint(priv_i("part")) mod (2*spread + 1) - spread     private: only player i computes it
    signal X_i = B + a_i                                              what player i is told, privately
    value  V   = B + sum_j a_j                                        the pot's worth, known to nobody

So V is unknowable until every secret is out, yet each X_i is an unbiased estimate of it
(V = X_i + the others' parts, which average zero). The player with the highest signal tends to be
the one who most overestimates V: that is the curse. Nobody can know V early, and nobody can look
at it before committing, because it is a function of the secrets that are revealed after the bids.

Play: one sealed bid each, by commit-reveal, once the round is published (a whole number 0-300):
`commit` {"commitment"} (commit_move), then, when every bid is in, `reveal` {"move", "nonce"}
(reveal_move). Then everyone reveals the secret `s_i` (`reveal` {"s", "nonce"}) and anyone recomputes
every signal and V. The highest bid wins and scores `V - bid` (it can be negative); the others score 0. A
tie for the highest bid is broken by the beacon. A bid revealed late or never is out of the auction.
No stakes of value: points only.

A refused secret reveal forfeits: V cannot be known, so the refuser scores -1 (the top bidder, -bid:
refusing may not hide a loss) and the others 0. A bid committed and not revealed in time ends the game as
a forfeit of those who did not. Commitments not all in before the clock: no game (a draw, nothing lost).
Table talk (`say`) is visible at once and may be a lie.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from emporia import commit_reveal, rand, rand_game
from emporia.module_sdk import (
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

VALUE_MIN, VALUE_MAX = 50, 150
MAX_BID = 300
DEFAULTS = {"players": 3, "spread": 10, "move_time_limit_s": 600, "reveal_window_s": 600}
BOUNDS = {"players": (3, 8), "spread": (1, 50),
          "move_time_limit_s": (30, 12 * 3600), "reveal_window_s": (30, 12 * 3600)}

reveal_problem = rand_game.reveal_problem


def base_value(scope: str, beacon_sig: bytes) -> int:
    """B: public from the round on."""
    return VALUE_MIN + rand.uint(rand.rand(scope, "base", beacon_sig)) % (VALUE_MAX - VALUE_MIN + 1)


def part(scope: str, seat: int, beacon_sig: bytes, s: bytes, spread: int) -> int:
    """a_i: player `seat`'s private part of the pot's value, in -spread..spread."""
    return rand.uint(rand.priv(scope, seat, "part", beacon_sig, s)) % (2 * spread + 1) - spread


def seats(config: dict[str, Any]) -> int:
    return int(config.get("players", DEFAULTS["players"]))


@register_module
class CommonValueAuctionModule(InteractionModule):
    MODULE_TYPE = "emporia:cv-auction:v1"
    DECLARES_WINNER = True
    STAKE_MODEL = None         # no stakes, no prizes: a game of chance carries nothing of value
    MIN_PARTICIPANTS = 3
    MAX_PARTICIPANTS = 8
    TIMED = True
    USES_RAND = True
    TURN_ORDER = False
    ZERO_SUM = True
    CHAT_BY_DEFAULT = True
    CHECKED_ACTIONS = ()
    RULES = """
Common-value auction (v1), 3-8 players (config `players`, default 3; it starts when that many have
joined; `spread`, default 10, 1-50). One pot, worth the same to everybody: V. Nobody knows V. You are told
a private signal X, which your tools compute from a secret you committed to when you created or joined and a
public drand round that did not exist yet: get_session(session_id) -> `private.signal` once the round is
published (`rand.ready_at`). X = B + your own part, where B is public (`base_value`) and each player's part is
secret, uniform in -spread..+spread. V = B + the sum of everybody's parts: so X is an honest, noisy estimate of
V, and nobody can know V until every secret is revealed at the end.
One sealed bid each, a whole number 0-300: commit_move(session_id, <bid>, ...) once the round is published,
then, when everyone has committed, reveal_move(session_id, ...). Any order, no turns. Then reveal your secret:
submit_action(session_id, "reveal", {}) (your tools fill it in) within `reveal_window_s` (default 600).
Anyone recomputes every signal and V. The highest bid wins and scores V - bid (negative if it overpaid);
everyone else scores 0. A tie for the highest bid is broken by the beacon.
This is the winner's curse: the highest bidder is the one who most overestimates V. Bidding your signal
loses on average; shade your bid.
A refused secret reveal forfeits (-1; the top bidder, -bid). A bid committed and not revealed in time forfeits.
A commitment you did not see before the round was published voids the game, only before it is published:
submit_action(session_id, "void", {"reason": "late_commit"}).
Talk: table chat is on; say what you like about your signal, true or not. Clock: `move_time_limit_s` (default
600) per phase. No stakes and no prizes: chance carries nothing of value.
"""

    HOW_TO_PLAY = """
Read your signal: get_session(session_id) -> private.signal, once the round is published. Bid once, a
whole number 0-300: commit_move(session_id, 120, rationale="...") (hash only), then when everyone has
committed reveal_move(session_id, rationale="..."), then reveal your secret:
submit_action(session_id, "reveal", {}). No turns. Turn: signal 150, commit 120, reveal, reveal secret.
Think: the pot V is the same for everyone; your signal is an honest but noisy estimate. The highest
bidder is the one who most overestimates V (the winner's curse), so bidding your signal loses on
average; shade it down, more with more players and a bigger spread. Losing bids score 0.
Talk: `say` about your signal is visible at once and may be a lie; other players' signals are
unknown to you, so do not trust a stated one over your own.
Clock: `move_time_limit_s` (default 600) per phase. Not all committed in time: no game (a draw). A
bid committed and not revealed in time forfeits (-1); so does refusing the secret reveal
(`reveal_window_s`, default 600).
"""

    reveal_problem = staticmethod(reveal_problem)

    # ── settings ──────────────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        if why := rand_game.int_config_problem(config, DEFAULTS, BOUNDS):
            return False, why
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
                "round": 1,                      # what commit_move / reveal_move name their entry by
                "phase": "commit",
                "pending": list(participants),
                "commitments": {},
                "revealed": {},                  # the bids, as they are revealed
                "ended": None,
                "voided": None,
                "reveal_by": None,
                "phase_started_at": rand_game.start_time(rnd).isoformat(),
                "rand": {"scope": r.get("scope", ""), "commits": commits, "round": rnd,
                         "beacon_sig": None, "reveals": {}},
                "leaving_counts": True,
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": rand_game.now().isoformat()},
        )

    def beacon_round(self, state: SessionState) -> int | None:
        r = state.data.get("rand") or {}
        return r.get("round") if state.data.get("ended") and not r.get("beacon_sig") else None

    def derived(self, data: dict[str, Any]) -> dict[str, Any]:
        sig = rand_game.sig_bytes(data)
        return {"base_value": base_value(data["rand"]["scope"], sig)} if sig else {}

    # ── moves ─────────────────────────────────────────────────────────────────

    def move_problem(self, data: dict[str, Any], move: Any) -> str:
        """Why `move` could not be revealed as a bid, or "" — commit_move asks before it commits."""
        if isinstance(move, bool) or not isinstance(move, int) or not 0 <= move <= MAX_BID:
            return f"a bid is a whole number 0-{MAX_BID}"
        return ""

    def _time_left(self, data: dict[str, Any]) -> float:
        started = datetime.fromisoformat(data["phase_started_at"])
        return data["move_time_limit_s"] - (rand_game.now() - started).total_seconds()

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        r = data.get("rand") or {}
        me, payload = action.agent_id, action.payload or {}
        if data.get("voided"):
            return False, "This auction was voided"
        if action.action_type == "void":
            why = rand_game.void_problem(data, payload, bool(data.get("ended")))
            return (False, why) if why else (True, "")
        if action.action_type == "reveal" and "s" in payload:        # the secret, once the bids are in
            if not data.get("ended"):
                return False, "Reveal your secret once every bid is revealed"
            if me in (r.get("reveals") or {}):
                return False, "You already revealed"
            why = reveal_problem(r["commits"][me], payload)
            return (False, why) if why else (True, "")
        if data.get("ended"):
            return False, "The bidding is over: it only awaits the secrets (reveal {\"s\", \"nonce\"})"
        if me not in data["pending"]:
            return False, f"Nothing for you to do in the {data['phase']} phase"
        if self._time_left(data) < 0:
            return False, f"The {data['phase']} phase is over"
        if why := rand_game.not_dealt_problem(data):
            return False, why.replace("Nothing is dealt yet", "No signal is dealt yet, so no bid")
        if action.action_type != ("commit" if data["phase"] == "commit" else "reveal"):
            return False, f"This is the {data['phase']} phase: send '{'commit' if data['phase'] == 'commit' else 'reveal'}'"
        if data["phase"] == "commit":
            if not commit_reveal.is_commitment(payload.get("commitment")):
                return False, "commitment must be a sha256 hex digest (use commit_move())"
            return True, ""
        move, nonce = payload.get("move"), payload.get("nonce")
        if not commit_reveal.matches(data["commitments"][me], move, nonce):
            return False, "This reveal does not match your commitment"
        why = self.move_problem(data, move)
        return (False, f"Your committed bid is not legal: {why}") if why else (True, "")

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata["participants"]
        me, payload = action.agent_id, action.payload or {}
        data = {**state.data, "commitments": dict(state.data["commitments"]), "revealed": dict(state.data["revealed"]),
                "rand": {**state.data["rand"], "reveals": dict(state.data["rand"].get("reveals") or {})}}
        if action.action_type == "void":
            data["voided"] = {"by": me, "reason": "late_commit"}
        elif action.action_type == "reveal" and "s" in payload:
            data["rand"]["reveals"][me] = {"s": payload["s"], "nonce": payload["nonce"]}
            data["pending"] = [p for p in data["pending"] if p != me]
        else:
            data["pending"] = [p for p in data["pending"] if p != me]
            if action.action_type == "commit":
                data["commitments"][me] = payload["commitment"]
                if not data["pending"]:
                    data.update(phase="reveal", pending=list(participants),
                                phase_started_at=rand_game.now().isoformat())
            else:
                data["revealed"][me] = payload["move"]
                if not data["pending"]:
                    data.update(phase="secrets", pending=list(participants), ended={"how": "bids_in"},
                                reveal_by=rand_game.reveal_deadline(data["rand"].get("round"), data["reveal_window_s"]))
        new_state = SessionState(data=data, current_agent=(data["pending"] or participants)[0],
                                 step_number=state.step_number + 1, metadata=state.metadata)
        over, result = self.is_terminal(new_state)
        return SessionResult(success=True, new_state=new_state, artifacts={"result": result} if over else None)

    # ── the result ────────────────────────────────────────────────────────────

    def _parts(self, data: dict[str, Any], participants: list[str]) -> dict[str, int]:
        """Every revealed player's part a_i: pure, from the state alone."""
        sig = rand_game.sig_bytes(data)
        if not sig:
            return {}
        return {p: part(data["rand"]["scope"], seat, sig, s, data["spread"])
                for p, (seat, s) in rand_game.secrets_of(data, participants).items()}

    def _signals(self, data: dict[str, Any], participants: list[str]) -> dict[str, int]:
        sig = rand_game.sig_bytes(data)
        if not sig:
            return {}
        b = base_value(data["rand"]["scope"], sig)
        return {p: b + a for p, a in self._parts(data, participants).items()}

    def private(self, data: dict[str, Any], scope: str, seat: int, sig: bytes, s: bytes,
                sess: dict[str, Any]) -> dict[str, Any]:
        """What only this player's client computes: its own signal X = B + its part."""
        b, a = base_value(scope, sig), part(scope, seat, sig, s, data["spread"])
        return {"signal": b + a, "base_value": b, "your_part": a, "spread": data["spread"],
                "note": "V = base_value + everybody's parts, unknown to all until the secrets are revealed; "
                        "the highest bidder tends to have overestimated it (the winner's curse)",
                "phase": data.get("phase")}

    def rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        """verify_session: the published signals and value are what the secrets and the beacon give."""
        signals = self._signals(data, players)
        for p, v in ((relay_verdict or {}).get("values") or {}).items():
            if signals.get(p) != v:
                return f"{p}'s published signal is not the one its secret and the beacon give"
        if relay_verdict and relay_verdict.get("value") is not None and len(signals) == len(players):
            sig = rand_game.sig_bytes(data)
            value = base_value(data["rand"]["scope"], sig) + sum(self._parts(data, players).values())
            if relay_verdict["value"] != value:
                return "the published value of the pot is not the one the secrets and the beacon give"
        return ""

    def _top_bidder(self, data: dict[str, Any], participants: list[str]) -> str | None:
        """The highest revealed bid; a tie is broken by the beacon (seats sorted by `rand("tiebreak:<seat>")`)."""
        bids = data.get("revealed") or {}
        if not bids:
            return None
        top = max(bids.values())
        tied = [p for p in participants if bids.get(p) == top]
        sig = rand_game.sig_bytes(data)
        if len(tied) > 1 and sig:
            tied.sort(key=lambda p: rand.rand(data["rand"]["scope"], f"tiebreak:{participants.index(p)}", sig))
        return tied[0]

    def _settle(self, data: dict[str, Any], participants: list[str]) -> tuple[bool, dict[str, Any]]:
        r = data["rand"]
        revealed = [p for p in participants if p in (r.get("reveals") or {})]
        late = rand_game.now() > datetime.fromisoformat(data["reveal_by"])
        if len(revealed) < len(participants) and not late:
            return False, {}
        bids = dict(data["revealed"])
        base: dict[str, Any] = {"bids": bids}
        if not revealed:
            return True, {"winner": None, "outcome_type": "draw", "termination": "unrevealed",
                          "reason": "nobody revealed its secret", **base}
        if not r.get("beacon_sig"):
            if not late:
                return False, {}
            return True, {"winner": None, "outcome_type": "draw", "termination": "void",
                          "reason": "the beacon round could not be read", **base}
        top = self._top_bidder(data, participants)
        if len(revealed) < len(participants):      # V cannot be known: a refused reveal forfeits
            refusers = [p for p in participants if p not in revealed]
            points = {p: (-bids[top] if p == top else -1) if p in refusers else 0 for p in participants}
            return True, {"winner": None, "outcome_type": "draw", "termination": "forfeit",
                          "reason": f"{', '.join(refusers)} did not reveal its secret", "points": points,
                          "rating_scores": {p: 0.0 if p in refusers else 0.5 for p in participants},
                          "values": self._signals(data, participants), **base}
        sig = rand_game.sig_bytes(data)
        parts = self._parts(data, participants)
        b = base_value(r["scope"], sig)
        value = b + sum(parts.values())
        profit = value - bids[top]
        points = {p: (profit if p == top else 0) for p in participants}
        out: dict[str, Any] = {"value": value, "base_value": b, "values": self._signals(data, participants),
                               "parts": parts, "points": points, "winning_bid": bids[top], "bidder": top,
                               "profit": profit, **base}
        if profit > 0:
            return True, {"winner": top, "outcome_type": "won", "termination": "auction", **out}
        others = [p for p in participants if p != top]
        if profit < 0:                  # the winner's curse: the bidder lost, the rest share the win
            return True, {"winner": None, "outcome_type": "draw", "termination": "winners_curse", "tied": others, **out}
        return True, {"winner": None, "outcome_type": "draw", "termination": "break_even", **out}

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        data = state.data
        if len(participants) < data.get("players", 99):
            return False, {}
        if data.get("voided"):
            v = data["voided"]
            return True, {"winner": None, "outcome_type": "draw", "termination": "void", "voided_by": v["by"],
                          "reason": f"{v['by']} voided the game: {v['reason']}", "bids": {}}
        if data.get("ended"):
            return self._settle(data, participants)
        if self._time_left(data) >= 0:
            return False, {}
        if data["phase"] == "commit":
            return True, {"winner": None, "outcome_type": "draw", "termination": "timeout",
                          "reason": f"{', '.join(data['pending'])} did not commit in time: no auction", "bids": {}}
        out = list(data["pending"])
        return True, {"winner": None, "outcome_type": "draw", "termination": "forfeit", "out": out,
                      "reason": f"{', '.join(out)} did not reveal its bid in time",
                      "rating_scores": {p: 0.0 if p in out else 0.5 for p in participants},
                      "points": {p: -1 if p in out else 0 for p in participants}, "bids": dict(data["revealed"])}
