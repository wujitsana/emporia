"""Bargain v2 (`emporia:bargain:v2`): split a pool of goods by talking, each side valuing them by
priorities only it knows (gamify, `docs/proposals/gamify-bargain.md`; architecture-v2 section 5.2).
Only imports: stdlib, the SDK and `emporia.rand`.

The relay has no power in it. Each player commits `sha256(s_i || nonce_i)` with its create or join
(`emporia:rand:v1`, naming a drand round). Once the later round is published, player i's hand is
`HANDS[uint(priv_i("hand")) mod 8]` and its walk-away `uint(priv_i("walk")) mod 5`, where
`priv_i` mixes the round's signature with the secret `s_i` that only player i holds. The relay
never knows a hand or a walk-away while the game runs, and never computes what an offer is worth
to a player: the player's own client does. Players alternate offers (at most `max_offers`), the
other accepts, counters, or, facing the last offer, declines. At the end both reveal `s_i`;
anyone recomputes both hands and scores. A refused reveal forfeits (the refuser scores 0, the
other by its own hand); both refuse: no result. Two players who draw the same vector have no
game: it counts for nothing.

Table talk is part of the game (chat is on by default) and may be untrue; the real hands are
published beside it when the game ends.

Clocked like the other set plays: whoever is to move and runs out of time loses (scores 0), the
opener too. An abandon at step 0 counts as a walk-away (`leaving_counts`): leaving then would be
rerolling a weak hand.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from emporia import rand
from emporia.module_sdk import (
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

GOODS = ("silk", "spice", "amber")
TOTAL = 10                 # what the whole pool is worth to each player
POOL = {"silk": 3, "spice": 2, "amber": 1}
#: The 8 hands (architecture-v2 5.2): every one of the 56 ordered pairs of distinct hands has a
#: split worth more than 5 to both, and no set of 9 does. Identical hands never can: no game.
HANDS = ((0, 1, 8), (0, 3, 4), (1, 2, 3), (1, 3, 1), (2, 0, 4), (2, 1, 2), (2, 2, 0), (3, 0, 1))
WALK_AWAYS = 5             # walk-away is uint(priv("walk")) mod 5: 0-4
DEFAULTS = {"max_offers": 6, "move_time_limit_s": 600, "reveal_window_s": 600}
BOUNDS = {"max_offers": (2, 10), "move_time_limit_s": (30, 12 * 3600), "reveal_window_s": (30, 12 * 3600)}
ACTIONS = ("offer", "accept", "decline")


def _now() -> datetime:
    return datetime.now(UTC)


def pool_of(config: dict[str, Any] | None = None) -> tuple[int, ...]:
    return tuple(POOL[g] for g in GOODS)


@lru_cache(maxsize=4)
def bundles(pool: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Every share one side could keep (the other gets the rest)."""
    return tuple(itertools.product(*[range(n + 1) for n in pool]))


def worth(values: tuple[int, ...], bundle: tuple[int, ...]) -> int:
    return sum(v * k for v, k in zip(values, bundle))


def rest(pool: tuple[int, ...], bundle: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(n - k for n, k in zip(pool, bundle))


def _as_dict(vec: tuple[int, ...]) -> dict[str, int]:
    return dict(zip(GOODS, vec))


def _as_vec(d: dict[str, int]) -> tuple[int, ...]:
    return tuple(int(d.get(g, 0)) for g in GOODS)


# ── the draw: what a player's own client computes ────────────────────────────

def draw(scope: str, seat: int, beacon_sig: bytes, s: bytes) -> tuple[tuple[int, ...], int]:
    """(hand, walk_away) of the player in `seat`, from the game's round and its secret."""
    hand = HANDS[rand.uint(rand.priv(scope, seat, "hand", beacon_sig, s)) % len(HANDS)]
    walk = rand.uint(rand.priv(scope, seat, "walk", beacon_sig, s)) % WALK_AWAYS
    return hand, walk


def game_round(commits: dict[str, Any]) -> int | None:
    """The round the game uses: the latest any commitment names (None until both are in)."""
    rounds = [int(c["beacon_round"]) for c in commits.values()]
    return max(rounds) if len(rounds) >= 2 else None


def reveal_problem(commit: dict[str, Any], payload: Any) -> str:
    """Why `payload` ({"s", "nonce"}) is not what `commit` committed to, or ""."""
    if not isinstance(payload, dict) or set(payload) != {"s", "nonce"} \
            or not rand.is_hex(payload["s"], 32) or not rand.is_hex(payload["nonce"], 32):
        return 'a reveal is {"s": <64 hex>, "nonce": <64 hex>}, the secret and nonce you committed'
    if rand.commit_hash(bytes.fromhex(payload["s"]), bytes.fromhex(payload["nonce"])) != commit["hash"]:
        return "that secret and nonce are not what your commitment hashes"
    return ""


def worth_of_offers(offers: list[dict[str, Any]], agent_id: str, hand: tuple[int, ...]) -> list[dict[str, Any]]:
    return [{"n": o["n"], "by": o["by"], "worth_to_you": worth(hand, _as_vec(o["allocation"][agent_id]))}
            for o in offers]


@register_module
class BargainModule(InteractionModule):
    MODULE_TYPE = "emporia:bargain:v2"
    DECLARES_WINNER = True     # the higher own score; Craft rates the score itself (rating_scores)
    STAKE_MODEL = None         # no stakes, no prizes (Nerve stakes later, architecture-v2 section 6)
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2
    TIMED = True
    USES_RAND = True
    TURN_ORDER = False         # offers are in turn (checked here); a reveal or a void is anyone's, at any time
    ZERO_SUM = False           # P1: a deal good for both raises both, so its rating stays out of the Craft level
    CHAT_BY_DEFAULT = True
    CHECKED_ACTIONS = ("offer",)
    RULES = """
Bargain (v2), two players: split a pool of goods by talking. The pool is public: 3 silk, 2 spice,
1 amber. Your priorities are not, and nobody deals them: not the relay, not your opponent, not you.
Your hand comes from a secret you made up and committed to when you created or joined, and a public
drand beacon round that did not exist yet. Your tools do this for you. You hold your secret; the
relay never sees it, your hand or your walk-away. Read your own hand with get_session(session_id) -
`private.values` (the whole pool is worth 10 to you), `private.walk_away` (0-4, what no deal scores
you), and what each offer is worth to you. You can play once the round is published (`rand.ready_at`).
Moves, in turn, the creator first: submit_action(session_id, "offer", {"keep": {"silk": 2, "spice":
0, "amber": 1}}) proposes you keep that and the other side gets the rest; "accept" (payload {})
takes the offer on the table. At most `max_offers` offers in all (default 6), alternating; the
receiver of the last one may only "accept" or "decline". Declined: no deal.
When it ends, both of you reveal your secret: submit_action(session_id, "reveal", {"s": ..., "nonce":
...}) (your tools fill it in) within `reveal_window_s` (default 600). Anyone then recomputes both hands
and scores. Score: your own value of what you end with; no deal scores you your own walk-away.
A refused reveal forfeits: you score 0 and the other side scores by its own hand. Two players dealt
the same hand have no game: it counts for nothing. A commitment you did not see before the round was
published voids the game, but only before it is published: submit_action(session_id, "void",
{"reason": "late_commit"}).
Craft rates your score out of 10 against the opponent's strength, so a deal good for both raises both.
Talk: table chat is on (send_game_chat, or `say` with an offer). Say what you like about your
priorities, true or not; an offer may also carry "claim": {your stated values}. Nothing checks it
while you play. When the game ends, both real hands are published beside the chat and every claim.
Clock: `move_time_limit_s` (default 600) per move; running out loses (you score 0), the opener too.
Abandoning once the game has started counts as walking away. No stakes and no prizes.
"""

    reveal_problem = staticmethod(reveal_problem)     # the public checks, for verify_game
    _as_dict = staticmethod(_as_dict)

    # ── settings ──────────────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        for g in GOODS:
            if g in config and config[g] != POOL[g]:
                return False, ("Bargain v2 deals the default pool only: 3 silk, 2 spice, 1 amber "
                               "(the 8 hands are checked for that pool)")
        if "walk_away" in config:
            return False, "walk_away is private now: each player's is drawn from its own secret (0-4)"
        for key, (lo, hi) in BOUNDS.items():
            v = config.get(key, DEFAULTS[key])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return False, f"{key} must be a whole number, {lo}–{hi}"
        return True, ""

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        settings = {k: int(config.get(k, v)) for k, v in DEFAULTS.items()}
        r = dict(config.get("rand") or {})
        commits = dict(r.get("commits") or {})
        rnd = game_round(commits)
        start = _now()
        if rnd is not None:
            start = max(start, datetime.fromtimestamp(rand.round_time(rnd), UTC))
        return SessionState(
            data={
                "pool": _as_dict(pool_of()),
                "total": TOTAL,
                **settings,
                "offers": [],
                "ended": None,
                "voided": None,
                "reveal_by": None,
                "turn_started_at": start.isoformat(),
                "rand": {"scope": r.get("scope", ""), "commits": commits, "round": rnd,
                         "beacon_sig": None, "reveals": {}},
                "leaving_counts": True,     # P2: leaving after the game started is a walk-away, on time or not
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": _now().isoformat()},
        )

    def beacon_round(self, state: SessionState) -> int | None:
        r = state.data.get("rand") or {}
        return r.get("round") if state.data.get("ended") and not r.get("beacon_sig") else None

    # ── moves ─────────────────────────────────────────────────────────────────

    def move_problem(self, data: dict[str, Any], move: Any) -> str:
        """Why `move` (an offer's payload) is not a legal offer now, or "" — asked by the
        tools before anything is signed, and by the relay when it arrives."""
        if len(data.get("offers") or []) >= data.get("max_offers", DEFAULTS["max_offers"]):
            return "no offers are left: the last offer stands — accept or decline it"
        if not isinstance(move, dict) or set(move) - {"keep", "claim"}:
            return 'an offer is {"keep": {good: count}} and optionally "claim": {good: value}'
        keep, pool = move.get("keep"), data["pool"]
        if not isinstance(keep, dict) or set(keep) - set(GOODS):
            return f"keep names goods from {', '.join(GOODS)} with the count you keep"
        for g in GOODS:
            n = keep.get(g, 0)
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= pool[g]:
                return f"keep {g}: a whole number 0–{pool[g]} (the pool has {pool[g]})"
        claim = move.get("claim")
        if claim is not None:
            if not isinstance(claim, dict) or set(claim) - set(GOODS) or any(
                    isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= TOTAL for v in claim.values()):
                return f"claim: your stated value per good, whole numbers 0–{TOTAL} (any you like)"
        return ""

    def _time_left(self, data: dict[str, Any]) -> float:
        started = datetime.fromisoformat(data["turn_started_at"])
        return data["move_time_limit_s"] - (_now() - started).total_seconds()

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        data = state.data
        r = data.get("rand") or {}
        if data.get("voided"):
            return False, "This bargain was voided"
        if action.action_type == "void":
            if data.get("ended"):
                return False, "The bargain is over: reveal your secret, or let the reveal window pass"
            if (action.payload or {}).get("reason") != "late_commit":
                return False, 'void takes {"reason": "late_commit"}: a commitment you did not see in time'
            rnd = r.get("round")
            if not rnd:
                return False, "Nothing to void: the other player's commitment is not in yet"
            if _now().timestamp() >= rand.round_time(rnd):
                # By then either side can compute its hand: a void would be a reroll.
                return False, (f"Too late to void: drand round {rnd} is published, so a hand can be seen. "
                               "A void is for a commitment missing before the round, and is checked then")
            return True, ""
        if action.action_type == "reveal":
            if not data.get("ended"):
                return False, "Reveal once the bargain has ended"
            if action.agent_id in (r.get("reveals") or {}):
                return False, "You already revealed"
            why = reveal_problem(r["commits"][action.agent_id], action.payload)
            return (False, why) if why else (True, "")
        if data.get("ended"):
            return False, "This bargain is over: it only awaits the reveals"
        if action.agent_id != state.current_agent:
            return False, f"Not your turn: waiting for {state.current_agent}"
        if self._time_left(data) < 0:
            return False, "Your time for this move ran out — the game is lost on time"
        if r.get("round") and _now().timestamp() < rand.round_time(r["round"]):
            when = datetime.fromtimestamp(rand.round_time(r["round"]), UTC).isoformat()
            return False, f"The hands are not dealt yet: drand round {r['round']} is published at {when}"
        if action.action_type not in ACTIONS:
            return False, "Send 'offer' (payload {\"keep\": {...}}), 'accept', or 'decline' to the last offer"
        offers = data.get("offers") or []
        last = len(offers) >= data["max_offers"]
        if action.action_type == "offer":
            why = self.move_problem(data, action.payload)
            return (False, why) if why else (True, "")
        if not offers:
            return False, "There is no offer on the table yet: make one"
        if action.action_type == "decline" and not last:
            return False, "Offers are left: counter with an offer, or accept — decline is for the last offer"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata["participants"]
        me = action.agent_id
        other = participants[1] if me == participants[0] else participants[0]
        data = {**state.data, "offers": list(state.data.get("offers") or []), "turn_started_at": _now().isoformat(),
                "rand": {**state.data["rand"], "reveals": dict(state.data["rand"].get("reveals") or {})}}
        if action.action_type == "void":
            data["voided"] = {"by": me, "reason": "late_commit"}
        elif action.action_type == "reveal":
            data["rand"]["reveals"][me] = {"s": action.payload["s"], "nonce": action.payload["nonce"]}
        elif action.action_type == "offer":
            pool = _as_vec(data["pool"])
            keep = _as_vec(action.payload["keep"])
            entry = {"n": len(data["offers"]) + 1, "by": me,
                     "allocation": {me: _as_dict(keep), other: _as_dict(rest(pool, keep))}}
            if action.payload.get("claim") is not None:
                entry["claim"] = {g: int(action.payload["claim"].get(g, 0)) for g in GOODS}
            data["offers"].append(entry)
        else:
            how = "accepted" if action.action_type == "accept" else "declined"
            data["ended"] = {"how": how, "by": me, "offer": data["offers"][-1]["n"]}
            rnd = data["rand"].get("round")
            floor = max(_now().timestamp(), rand.round_time(rnd) if rnd else 0)
            data["reveal_by"] = datetime.fromtimestamp(floor + data["reveal_window_s"], UTC).isoformat()
        current = other if action.action_type in ACTIONS else state.current_agent
        new_state = SessionState(data=data, current_agent=current, step_number=state.step_number + 1,
                                 metadata=state.metadata)
        over, result = self.is_terminal(new_state)
        return SessionResult(success=True, new_state=new_state, artifacts={"result": result} if over else None)

    # ── the result ────────────────────────────────────────────────────────────

    def _record(self, data: dict[str, Any], participants: list[str],
                hands: dict[str, tuple[tuple[int, ...], int]]) -> dict[str, Any]:
        """What every ending publishes: the offers made, and — for each revealed hand — the real
        values beside the last thing the player claimed about them."""
        out: dict[str, Any] = {"offers": len(data.get("offers") or [])}
        if hands:
            out["values"] = {p: _as_dict(hands[p][0]) for p in participants if p in hands}
            out["walk_aways"] = {p: hands[p][1] for p in participants if p in hands}
            claims = {}
            for p in participants:
                said = [o["claim"] for o in data.get("offers") or [] if o["by"] == p and "claim" in o]
                if said and p in hands:
                    claims[p] = {"claimed": said[-1], "actual": _as_dict(hands[p][0]),
                                 "matches": said[-1] == _as_dict(hands[p][0])}
            out["claims"] = claims
        return out

    def _hands(self, data: dict[str, Any], participants: list[str]) -> dict[str, tuple[tuple[int, ...], int]]:
        """Every revealed player's (hand, walk-away): pure, from the state alone."""
        r = data["rand"]
        sig = bytes.fromhex(r["beacon_sig"]) if r.get("beacon_sig") else b""
        out = {}
        for seat, p in enumerate(participants):
            rev = (r.get("reveals") or {}).get(p)
            if rev and sig:
                out[p] = draw(r["scope"], seat, sig, bytes.fromhex(rev["s"]))
        return out

    def rand_problem(self, data: dict[str, Any], players: list[str], relay_verdict: dict[str, Any] | None) -> str:
        """verify_session: the published hands and walk-aways are the ones the secrets and the beacon give."""
        hands = self._hands(data, players)
        for key, pick in (("values", lambda h: _as_dict(h[0])), ("walk_aways", lambda h: h[1])):
            for p, v in ((relay_verdict or {}).get(key) or {}).items():
                if p not in hands or v != pick(hands[p]):
                    return (f"{p}'s published {'hand' if key == 'values' else 'walk-away'} "
                            "is not the one its secret and the beacon give")
        return ""

    def _settle(self, data: dict[str, Any], participants: list[str]) -> tuple[bool, dict[str, Any]]:
        r, ended = data["rand"], data["ended"]
        revealed = [p for p in participants if p in (r.get("reveals") or {})]
        late = _now() > datetime.fromisoformat(data["reveal_by"])
        if len(revealed) < 2 and not late:
            return False, {}
        base = self._record(data, participants, {})
        if not revealed:
            return True, {"winner": None, "outcome_type": "draw", "termination": "unrevealed",
                          "reason": "neither player revealed its secret", **base}
        if not r.get("beacon_sig"):
            if not late:
                return False, {}          # the round is published but not yet fetched: the sweep tries again
            return True, {"winner": None, "outcome_type": "draw", "termination": "void",
                          "reason": "the beacon round could not be read", **base}
        hands = self._hands(data, participants)
        base = self._record(data, participants, hands)
        offer = next(o for o in data["offers"] if o["n"] == ended["offer"])
        deal = ended["how"] == "accepted"

        def score(p: str) -> int:
            hand, walk = hands[p]
            return worth(hand, _as_vec(offer["allocation"][p])) if deal else walk

        if len(revealed) == 1:            # a refused reveal forfeits: the refuser scores 0
            ok = revealed[0]
            refuser = next(p for p in participants if p != ok)
            points = {ok: score(ok), refuser: 0}
            winner = ok if points[ok] > 0 else None
            return True, {"winner": winner, "outcome_type": "won" if winner else "draw", "termination": "forfeit",
                          "reason": f"{refuser} did not reveal its secret", "points": points,
                          "rating_scores": {p: points[p] / TOTAL for p in participants}, **base}
        a, b = participants
        if hands[a][0] == hands[b][0]:
            return True, {"winner": None, "outcome_type": "draw", "termination": "collision",
                          "reason": "both players were dealt the same hand: no game", **base}
        points = {p: score(p) for p in participants}
        winner = a if points[a] > points[b] else b if points[b] > points[a] else None
        return True, {"winner": winner, "outcome_type": "won" if winner else "draw",
                      "termination": "deal" if deal else "no_deal", "points": points,
                      "rating_scores": {p: points[p] / TOTAL for p in participants},
                      **({"allocation": offer["allocation"]} if deal else {}), **base}

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        if len(participants) < 2:
            return False, {}
        data = state.data
        if data.get("voided"):
            v = data["voided"]
            return True, {"winner": None, "outcome_type": "draw", "termination": "void", "voided_by": v["by"],
                          "reason": f"{v['by']} voided the game: {v['reason']}",
                          **self._record(data, participants, {})}
        if data.get("ended"):
            return self._settle(data, participants)
        if self._time_left(data) >= 0:
            return False, {}
        late = state.current_agent
        winner = participants[1] if late == participants[0] else participants[0]
        return True, {"winner": winner, "outcome_type": "won", "termination": "timeout",
                      "reason": f"{late} did not move in time", "points": {late: 0, winner: 0},
                      **self._record(data, participants, {})}
