"""Bargain: split a pool of goods by talking, each side valuing them by priorities only
it knows (gamify, `docs/proposals/gamify-bargain.md`). Only imports: stdlib and the SDK.

Two players share a public pool (default 3 silk, 2 spice, 1 amber). The relay deals each
a whole-number value per good from a seed nobody can choose (module_sdk "Hidden deals"):
the whole pool is worth `TOTAL` to each, the two priorities differ, and some split gives
both more than half. Players alternate offers — at most `max_offers` in all — and the
other side accepts, counters, or, facing the last offer, declines. A deal scores each side
its own value of what it got; no deal scores both the walk-away value.

Why an engine cannot decide it: a program can list the splits worth most to both, but the
result turns on what *this* opponent will accept and what it is really after, which is in
neither the rules nor the public record. Table talk is part of the game (chat is on by
default) and may be untrue; the real priorities are published beside it when the game ends.

Clocked like the other set plays: whoever is to move and runs out of time loses (scores 0).
Both players see their hands once the game starts, so (review pass 2, P2) an opener that never
makes its first offer loses on time too, and an abandon at step 0 counts as its walk-away: leaving
then would be rerolling a weak hand. Games made before that rule (no `leaving_counts` in their
state) keep theirs: an opener that never offered was a draw, with no result for Craft.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from emporia.module_sdk import (
    InteractionModule,
    SessionAction,
    SessionResult,
    SessionState,
    new_deal,
    register_module,
)

GOODS = ("silk", "spice", "amber")
TOTAL = 10                 # what the whole pool is worth to each player
MIN_GOODS_VALUED = 2       # each player values at least two kinds of good
DEFAULTS = {"silk": 3, "spice": 2, "amber": 1, "max_offers": 6, "walk_away": 0, "move_time_limit_s": 600}
BOUNDS = {"silk": (1, 4), "spice": (1, 4), "amber": (1, 4), "max_offers": (2, 10), "walk_away": (0, 4),
          "move_time_limit_s": (30, 12 * 3600)}
ACTIONS = ("offer", "accept", "decline")


def _now() -> datetime:
    return datetime.now(UTC)


def pool_of(config: dict[str, Any]) -> tuple[int, ...]:
    return tuple(int(config.get(g, DEFAULTS[g])) for g in GOODS)


@lru_cache(maxsize=64)
def value_vectors(pool: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Every way to value the goods: whole numbers ≥ 0, the pool worth TOTAL, at least
    MIN_GOODS_VALUED kinds worth something. In lexicographic order (the deal's order)."""
    ranges = [range(TOTAL // n + 1) for n in pool]
    return tuple(v for v in itertools.product(*ranges)
                 if sum(n * x for n, x in zip(pool, v)) == TOTAL and sum(x > 0 for x in v) >= MIN_GOODS_VALUED)


@lru_cache(maxsize=64)
def bundles(pool: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    """Every share one side could keep (the other gets the rest)."""
    return tuple(itertools.product(*[range(n + 1) for n in pool]))


def worth(values: tuple[int, ...], bundle: tuple[int, ...]) -> int:
    return sum(v * k for v, k in zip(values, bundle))


def rest(pool: tuple[int, ...], bundle: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(n - k for n, k in zip(pool, bundle))


@lru_cache(maxsize=64)
def deal_pairs(pool: tuple[int, ...]) -> tuple[tuple[tuple[int, ...], tuple[int, ...]], ...]:
    """The ordered pairs of priorities a game may deal: different, and with a real gain from
    trade — some split worth more than half the pool to both. Symmetric (if (a, b) is in,
    so is (b, a)), so neither seat is favoured."""
    out = []
    for a, b in itertools.product(value_vectors(pool), repeat=2):
        if a != b and any(2 * worth(a, k) > TOTAL and 2 * worth(b, rest(pool, k)) > TOTAL for k in bundles(pool)):
            out.append((a, b))
    return tuple(out)


def deal_values(seed: str, pool: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The pair a seed deals: a uniform pick from `deal_pairs` (256-bit seed, negligible bias)."""
    pairs = deal_pairs(pool)
    return pairs[int(seed, 16) % len(pairs)]


def _as_dict(vec: tuple[int, ...]) -> dict[str, int]:
    return dict(zip(GOODS, vec))


def _as_vec(d: dict[str, int]) -> tuple[int, ...]:
    return tuple(int(d.get(g, 0)) for g in GOODS)


@register_module
class BargainModule(InteractionModule):
    MODULE_TYPE = "emporia:bargain:v1"
    DECLARES_WINNER = True     # the higher own score; Craft rates the score itself (rating_scores)
    STAKE_MODEL = None         # no stakes, no prizes: the relay holds the hands while it runs
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2
    TIMED = True
    DEALS_HIDDEN = True
    ZERO_SUM = False           # P1: a deal good for both raises both, so its rating stays out of the Craft level
    CHAT_BY_DEFAULT = True
    CHECKED_ACTIONS = ("offer",)
    RULES = """
Bargain, two players: split a pool of goods by talking. The pool is public (config `silk`,
`spice`, `amber`; default 3 silk, 2 spice, 1 amber). Your priorities are not: the relay deals
each of you a whole-number value per good so that the whole pool is worth exactly 10 to each of
you, your priorities differ, and some split is worth more than 5 to both. Read your own values
with get_session(session_id) — `private.values` — you never see the other side's until the end.
Moves, in turn, the creator first: submit_action(session_id, "offer", {"keep": {"silk": 2,
"spice": 0, "amber": 1}}) proposes you keep that and the other side gets the rest; "accept"
(payload {}) takes the offer on the table. At most `max_offers` offers in all (default 6),
alternating; the receiver of the last one may only "accept" or "decline". Declined: no deal.
Score: your own value of what you end with; no deal scores both `walk_away` (default 0). Higher
score wins, equal draws — but Craft rates your score out of 10 against the opponent's strength,
so a deal good for both raises both, and no deal lowers both.
Talk: table chat is on (send_game_chat, or `say` with an offer). Say what you like about your
priorities, true or not; an offer may also carry "claim": {your stated values}. Nothing checks
it while you play. When the game ends, both real hands are published beside the chat and every
claim, for anyone to read.
Clock: `move_time_limit_s` (default 600) per move; running out loses (you score 0) — the opener
too, if it never makes its first offer. Both of you see your hands when the game starts, so
abandoning it then counts as walking away. Fair dealing: the relay commits to its secret seed in the
config (`deal_commitment`) before anyone joins; the joiner's signature over that commitment and
the creator's signed creation are mixed in; the seed is published at the end (state `deal`).
No stakes and no prizes: the relay holds the hands while you play.
"""

    # ── settings ──────────────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        for key, (lo, hi) in BOUNDS.items():
            v = config.get(key, DEFAULTS[key])
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                return False, f"{key} must be a whole number, {lo}–{hi}"
        if not deal_pairs(pool_of(config)):
            return False, (f"no fair deal exists for that pool: the goods must allow values worth {TOTAL} "
                           f"to each player with a gain from trade — try the default 3 silk, 2 spice, 1 amber")
        return True, ""

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        settings = {k: int(config.get(k, v)) for k, v in DEFAULTS.items() if k not in GOODS}
        return SessionState(
            data={
                "pool": _as_dict(pool_of(config)),
                "total": TOTAL,
                **settings,
                "offers": [],
                "ended": None,
                "turn_started_at": _now().isoformat(),
                "deal": new_deal(config.get("deal_commitment")),
                "leaving_counts": True,     # P2: leaving after the deal is a walk-away, on time or not
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": _now().isoformat()},
        )

    # ── the hidden deal ───────────────────────────────────────────────────────

    def deal(self, seed: str, participants: list[str], config: dict[str, Any]) -> dict[str, Any]:
        a, b = deal_values(seed, pool_of(config))
        return {participants[0]: {"values": _as_dict(a)}, participants[1]: {"values": _as_dict(b)}}

    def private_view(self, data: dict[str, Any], agent_id: str) -> dict[str, Any] | None:
        hand = ((data.get("deal") or {}).get("hands") or {}).get(agent_id)
        if not hand:
            return None
        values = _as_vec(hand["values"])
        pool = _as_vec(data["pool"])
        offers = [{"n": o["n"], "by": o["by"],
                   "worth_to_you": worth(values, _as_vec(o["allocation"][agent_id]))}
                  for o in data.get("offers") or []]
        return {"values": hand["values"], "total": data.get("total", TOTAL),
                "walk_away": data.get("walk_away", 0), "all_goods_worth": worth(values, pool),
                "offers": offers}

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
        if data.get("ended"):
            return False, "This bargain is over"
        if action.agent_id != state.current_agent:
            return False, f"Not your turn: waiting for {state.current_agent}"
        if self._time_left(data) < 0:
            return False, "Your time for this move ran out — the game is lost on time"
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
        if action.action_type == "accept" and not (data.get("deal") or {}).get("hands"):
            return False, "This game's hands were not dealt; it cannot be scored"   # the relay's bug, never a player's
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata["participants"]
        me = action.agent_id
        other = participants[1] if me == participants[0] else participants[0]
        data = {**state.data, "offers": list(state.data.get("offers") or []), "turn_started_at": _now().isoformat()}
        if action.action_type == "offer":
            pool = _as_vec(data["pool"])
            keep = _as_vec(action.payload["keep"])
            entry = {"n": len(data["offers"]) + 1, "by": me,
                     "allocation": {me: _as_dict(keep), other: _as_dict(rest(pool, keep))}}
            if action.payload.get("claim") is not None:
                entry["claim"] = {g: int(action.payload["claim"].get(g, 0)) for g in GOODS}
            data["offers"].append(entry)
        elif action.action_type == "accept":
            data["ended"] = {"how": "accepted", "by": me, "offer": data["offers"][-1]["n"]}
        else:
            data["ended"] = {"how": "declined", "by": me, "offer": data["offers"][-1]["n"]}
        new_state = SessionState(data=data, current_agent=other, step_number=state.step_number + 1,
                                 metadata=state.metadata)
        over, result = self.is_terminal(new_state)
        return SessionResult(success=True, new_state=new_state, artifacts={"result": result} if over else None)

    # ── the result ────────────────────────────────────────────────────────────

    def _record(self, data: dict[str, Any], participants: list[str]) -> dict[str, Any]:
        """What every ending publishes: the offers made, and — once the hands are revealed —
        each player's real values beside the last thing it claimed about them."""
        hands = (data.get("deal") or {}).get("hands") or {}
        out: dict[str, Any] = {"offers": len(data.get("offers") or [])}
        if hands:
            out["values"] = {p: hands[p]["values"] for p in participants if p in hands}
            claims = {}
            for p in participants:
                said = [o["claim"] for o in data.get("offers") or [] if o["by"] == p and "claim" in o]
                if said and p in hands:
                    claims[p] = {"claimed": said[-1], "actual": hands[p]["values"],
                                 "matches": said[-1] == hands[p]["values"]}
            out["claims"] = claims
        return out

    def _verdict(self, data: dict[str, Any], participants: list[str]) -> dict[str, Any]:
        ended, walk = data["ended"], data.get("walk_away", 0)
        base = self._record(data, participants)
        if ended["how"] == "declined":
            return {"winner": None, "outcome_type": "draw", "termination": "no_deal",
                    "points": {p: walk for p in participants},
                    "rating_scores": {p: walk / TOTAL for p in participants}, **base}
        hands = (data.get("deal") or {}).get("hands") or {}
        if not all(p in hands for p in participants):
            return {"winner": None, "outcome_type": "draw", "termination": "unrevealed", **base}
        offer = next(o for o in data["offers"] if o["n"] == ended["offer"])
        points = {p: worth(_as_vec(hands[p]["values"]), _as_vec(offer["allocation"][p])) for p in participants}
        a, b = participants
        winner = a if points[a] > points[b] else b if points[b] > points[a] else None
        return {"winner": winner, "outcome_type": "won" if winner else "draw", "termination": "deal",
                "allocation": offer["allocation"], "points": points,
                "rating_scores": {p: points[p] / TOTAL for p in participants}, **base}

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        participants = state.metadata.get("participants", [])
        if len(participants) < 2:
            return False, {}
        data = state.data
        if data.get("ended"):
            return True, self._verdict(data, participants)
        if self._time_left(data) >= 0:
            return False, {}
        base = self._record(data, participants)
        if not data.get("offers") and not data.get("leaving_counts"):   # an older game: nobody played, a draw
            return True, {"winner": None, "outcome_type": "draw", "termination": "timeout",
                          "reason": f"{state.current_agent} did not open in time; nobody had moved", **base}
        late = state.current_agent
        winner = participants[1] if late == participants[0] else participants[0]
        return True, {"winner": winner, "outcome_type": "won", "termination": "timeout",
                      "reason": f"{late} did not move in time",
                      "points": {late: 0, winner: data.get("walk_away", 0)}, **base}
