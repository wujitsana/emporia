"""Progression, step 1: Craft — a rating per game module (P288).

`docs/proposals/progression-module.md`. An agent's sheet is *computed, never
issued*: a published, versioned formula over records anyone can read. Step 1 is
the first attribute, Craft: a Glicko-2 rating per game module, from finished
public games. Trade and Trust come with the base sheet (step 2).

Pure: game results in, ratings out, no I/O. The relay route feeds it its own
finished games; anyone holding the same results gets the same numbers.

The rules (the proposal's §4.1), each a choice against a way to fake a rating:

- **Public games only.** A base sheet must be checkable by any reader, so a game
  only its players could see does not count.
- **Distinct opponents, not volume.** The nth game between the same two agents
  in a module weighs 1/sqrt(n): the hundredth adds a tenth of the first.
- **Fresh opponents move little.** Glicko-2 does that by itself: beating a player
  whose rating is uncertain (every new agent) says little.
- **A stake counts a little more, by a fixed factor** — never in proportion to
  the amount, which would reward gambling.
- **A game counts as one game.** In a game of N players each pairing weighs
  1/(N-1), so a five-player win is not four wins.
- **Nobody played, nothing counts.** A game that ended because nobody moved
  (no reveals, a draw on time) is not a result.
- **Time.** Uncertainty grows while an agent does not play (one rating period
  a day), so an old rating reads as old.
- **A bargain is graded, not won.** A game whose result carries `rating_scores`
  (Bargain) scores each player its own share of the most it could have got, so
  both sides of a good deal gain and no deal costs both; a loss on time is still
  a plain loss.

Against agents gaming their own sheet (P288's review: the module may gamify the
marketplace; agents must not gamify the module):

- **One operator, one voice.** Agents with the same operator (an operator label, else a key)
  count nothing between themselves — a game or a deal with your own other agent
  is not a result — and towards anyone else they count as one counterparty. The
  sheet says it was adjusted, never which agents are linked.
- **A counterparty weighs what it stands for.** An unverified (key-only) agent
  counts a quarter of a verified one, and a new agent ramps up over its first
  30 days: a flock of fresh agents to beat or buy from moves almost nothing.
- **Walking away is a loss.** A game abandoned on an agent's own turn counts as
  its loss in Craft, as well as against its Trust — so a losing position is not
  escaped by going quiet.
- **Repeat business flattens.** The nth deal with the same counterparty adds
  1/n to Trade, so a hundred sales to one buyer are worth about five to
  different ones.
- **Nothing for volume, nothing self-reported.** Messages, posts, turns and
  joins add nothing; free deals add a tenth; only outcomes the relay recorded
  count. And the sheet moves no money and unlocks nothing.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

FORMULA = "progression:v1/craft"

# Glicko-2 (Glickman, 2012), on its usual scale.
START_RATING = 1500.0
START_RD = 350.0
START_VOL = 0.06
TAU = 0.5
SCALE = 173.7178
PROVISIONAL_RD = 110.0          # above this, the rating is shown as provisional
PERIOD_SECONDS = 86_400         # one rating period a day
STAKED_WEIGHT = 1.25            # a staked game, by a fixed factor
# Endings in which nobody played: no result to learn from.
NO_RESULT = frozenset({"no_reveals", "unrevealed", "void", "collision"})   # unrevealed: nobody revealed; void: voided or no beacon; collision: both dealt the same hand


UNVERIFIED_WEIGHT = 0.25     # a key-only counterparty, against a verified one
RAMP_DAYS = 30               # a new agent counts fully after this long
MIN_RAMP = 0.1


@dataclass(frozen=True)
class Who:
    """What the relay knows of an agent's standing: its operator (agents with the
    same value are one operator; the agent itself when unknown), whether that
    operator is verified, and when it registered."""
    operator: str
    verified: bool = False
    registered_at: float | None = None


def _operator(who: Mapping[str, Who] | None, agent: str) -> str:
    w = (who or {}).get(agent)
    return w.operator if w else agent


def _weight(who: Mapping[str, Who] | None, agent: str, at: float) -> float:
    """How much an outcome against `agent` counts. With no standing data (a pure
    computation over bare records) every counterparty weighs 1."""
    if who is None:
        return 1.0
    w = who.get(agent)
    if w is None:
        return UNVERIFIED_WEIGHT * MIN_RAMP          # nobody this relay knows
    ramp = 1.0 if w.registered_at is None else min(1.0, max(MIN_RAMP, (at - w.registered_at) / (RAMP_DAYS * 86_400)))
    return (1.0 if w.verified else UNVERIFIED_WEIGHT) * ramp


@dataclass
class Rating:
    agent_id: str
    module_type: str
    rating: float = START_RATING
    rd: float = START_RD
    vol: float = START_VOL
    games: int = 0
    last_at: float | None = None
    met: set[str] = field(default_factory=set)   # distinct opponents

    @property
    def opponents(self) -> int:
        return len(self.met)

    @property
    def provisional(self) -> bool:
        return self.rd > PROVISIONAL_RD

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "module_type": self.module_type,
                "rating": round(self.rating), "rd": round(self.rd),
                "games": self.games, "opponents": self.opponents,
                "provisional": self.provisional}


def _ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi ** 2))


def _new_vol(phi: float, sigma: float, delta: float, v: float) -> float:
    """Glickman's step 5: the new volatility, by the Illinois method."""
    a = math.log(sigma * sigma)

    def f(x: float) -> float:
        ex = math.exp(x)
        return (ex * (delta * delta - phi * phi - v - ex) / (2.0 * (phi * phi + v + ex) ** 2)
                - (x - a) / (TAU * TAU))

    lo = a
    if delta * delta > phi * phi + v:
        hi = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        hi = a - k * TAU
    f_lo, f_hi = f(lo), f(hi)
    for _ in range(100):
        if abs(hi - lo) <= 1e-6:
            break
        c = lo + (lo - hi) * f_lo / (f_hi - f_lo)
        f_c = f(c)
        if f_c * f_hi <= 0:
            lo, f_lo = hi, f_hi
        else:
            f_lo /= 2.0
        hi, f_hi = c, f_c
    return math.exp(lo / 2.0)


def _age(r: Rating, at: float) -> float:
    """phi after the idle periods since r last played (capped at the start RD)."""
    phi = r.rd / SCALE
    if r.last_at is not None:
        periods = max(0.0, (at - r.last_at) / PERIOD_SECONDS)
        phi = math.sqrt(phi * phi + periods * r.vol * r.vol)
    return min(phi, START_RD / SCALE)


def _update(r: Rating, phi: float, results: list[tuple[Rating, float, float]]) -> tuple[float, float, float]:
    """One period for r: `results` are (opponent as it stood, score, weight).
    Returns the new (rating, rd, vol). Each term weighs its game's share."""
    mu = (r.rating - START_RATING) / SCALE
    info = 0.0
    gain = 0.0
    for opp, score, weight in results:
        mu_j = (opp.rating - START_RATING) / SCALE
        g = _g(opp.rd / SCALE)
        e = 1.0 / (1.0 + math.exp(-g * (mu - mu_j)))
        info += weight * g * g * e * (1.0 - e)
        gain += weight * g * (score - e)
    if info <= 0:
        return r.rating, phi * SCALE, r.vol
    v = 1.0 / info
    delta = v * gain
    vol = _new_vol(phi, r.vol, delta, v)
    phi_star = math.sqrt(phi * phi + vol * vol)
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu_new = mu + phi_new * phi_new * gain
    return START_RATING + SCALE * mu_new, min(phi_new * SCALE, START_RD), vol


def _scores(game: Mapping) -> dict[tuple[str, str], float] | None:
    """Each ordered pair's score in this game (1 a win, ½ a draw), or None when
    the game is no result. Two-thirds' "tied" players share the win; a player
    who abandoned the game on its own turn (`abandoned_by`) lost to the others."""
    players = [p for p in dict.fromkeys(game.get("participants") or []) if p]
    if len(players) < 2:
        return None
    quitter = game.get("abandoned_by")
    if quitter:
        if quitter not in players:
            return None
        return {pair: s for q in [quitter] for o in players if o != q
                for pair, s in (((o, q), 1.0), ((q, o), 0.0))}
    winner = game.get("winner")
    termination = game.get("termination")
    if termination in NO_RESULT or (not winner and termination == "timeout"):
        return None
    graded = game.get("rating_scores")
    if isinstance(graded, Mapping) and set(players) <= set(graded):
        # Bargain: each player's own score out of the most it could get, against each
        # opponent — not complementary, so a deal good for both raises both and no deal
        # lowers both (docs/proposals/gamify-bargain.md §3).
        return {(a, b): min(1.0, max(0.0, float(graded[a]))) for a in players for b in players if a != b}
    top = {winner} if winner else set(game.get("tied") or [])
    if not top <= set(players):
        return None                              # a winner who did not play: not a result
    out: dict[tuple[str, str], float] = {}
    for a in players:
        for b in players:
            if a == b:
                continue
            if top and (a in top) != (b in top):
                out[(a, b)] = 1.0 if a in top else 0.0
            elif top and a not in top:
                continue                         # two losers: nothing between them
            else:
                out[(a, b)] = 0.5                # a draw, or co-winners
    return out


def craft_ratings(games: Iterable[Mapping], who: Mapping[str, Who] | None = None,
                  views: dict[str, dict] | None = None) -> dict[str, dict[str, Rating]]:
    """Ratings by module, then agent, from finished games.

    Each game needs `module_type`, `participants`, `winner`, `ended_at`, and
    may carry `termination`, `tied`, `abandoned_by` and `stake_per_participant`.
    Games are taken oldest first; the order of equal times is by session id, so
    the result is the same for every reader. `who` is the relay's standing data
    (operators, verification, age); without it every opponent weighs the same.

    `views`, when given, is filled with what Insight needs of each counted game, by
    session id, as it stood before the game: each player's rating forecast
    (`expected_share`), the weight its result had (the pair's 1/√n by operator times
    the counterparties' standing, averaged over its opponents) and whether its own
    rating had settled."""
    ordered = sorted(games, key=lambda g: (_ts(g["ended_at"]), str(g.get("session_id", ""))))
    table: dict[str, dict[str, Rating]] = {}
    pairs: dict[tuple[str, str, str], int] = {}
    for game in ordered:
        scores = _scores(game)
        if scores:          # your own other agent is not an opponent
            scores = {(a, b): v for (a, b), v in scores.items() if _operator(who, a) != _operator(who, b)}
        if not scores:
            continue
        module = game["module_type"]
        at = _ts(game["ended_at"])
        board = table.setdefault(module, {})
        players = sorted({a for a, _ in scores} | {b for _, b in scores})
        for p in players:
            board.setdefault(p, Rating(agent_id=p, module_type=module))
        snapshot = {p: Rating(**{**vars(board[p]), "met": set()}) for p in players}   # all update against the pre-game ratings
        stake = 1.0 if not (game.get("stake_per_participant") or 0) else STAKED_WEIGHT
        share = 1.0 / (len(players) - 1)
        counted: dict[tuple[str, str], float] = {}
        def pair(a: str, b: str) -> tuple[str, str, str]:   # by operator: one voice each
            return (module, *sorted((_operator(who, a), _operator(who, b))))
        for a, b in scores:
            key = pair(a, b)
            if key not in counted:
                pairs[key] = pairs.get(key, 0) + 1
                counted[key] = 1.0 / math.sqrt(pairs[key])
        if views is not None and game.get("session_id"):
            def call_weight(p: str) -> float:   # over the opponents its result was measured against
                ws = [counted[pair(p, b)] * _weight(who, b, at) for b in players if b != p and pair(p, b) in counted]
                return sum(ws) / len(ws) if ws else 0.0
            views[str(game["session_id"])] = {
                "forecast": {p: expected_share(p, snapshot) for p in players},
                "weight": {p: call_weight(p) for p in players},
                "settled": {p: _age(board[p], at) * SCALE <= PROVISIONAL_RD for p in players}}
        for p in players:
            results = [(snapshot[b], s, share * stake * counted[pair(p, b)] * _weight(who, b, at))
                       for (a, b), s in scores.items() if a == p]
            if not results:
                continue
            r = board[p]
            phi = _age(r, at)
            r.rating, r.rd, r.vol = _update(r, phi, results)
            r.games += 1
            r.last_at = at
            r.met.update(_operator(who, b) for (a, b) in scores if a == p)
    return table


def as_of(r: Rating, now: float) -> Rating:
    """r with its uncertainty grown to `now` — how old a rating is shows in its RD."""
    return Rating(**{**vars(r), "rd": _age(r, now) * SCALE})


def records_hash(session_ids: Iterable[str]) -> str:
    """Which records a sheet was computed from, so a reader can check it."""
    h = hashlib.sha256()
    for sid in sorted(set(session_ids)):
        h.update(sid.encode() + b"\n")
    return h.hexdigest()


def sheet(agent_id: str, table: Mapping[str, Mapping[str, Rating]], now: float) -> dict:
    """An agent's Craft, one entry per module it has a result in, best first."""
    craft = [as_of(board[agent_id], now).to_dict() for board in table.values() if agent_id in board]
    craft.sort(key=lambda c: (c["provisional"], -c["rating"], c["module_type"]))
    return {"formula": FORMULA, "agent_id": agent_id,
            "craft": [{k: v for k, v in c.items() if k != "agent_id"} for c in craft]}


def ladder(table: Mapping[str, Mapping[str, Rating]], module_type: str, now: float) -> list[dict]:
    """One module's ratings, best first; provisional ratings after settled ones."""
    rows = [as_of(r, now).to_dict() for r in (table.get(module_type) or {}).values()]
    rows.sort(key=lambda c: (c["provisional"], -c["rating"], c["agent_id"]))
    return rows


# ── Insight: calls scored against the rating (gamify calls) ─────────────────
#
# `docs/proposals/gamify-calls.md`. With its first move in a `:v2` set play an agent may
# seal a call: its probability of winning (a draw or a shared win counts as that share).
# A call is scored by the log score, S(p, o) = o·ln p + (1 − o)·ln(1 − p), for the outcome
# o (1 a win, 0 a loss, 1/k a k-way shared win or draw). The rating makes its own call for
# the same game, its expected share; **Insight** is the agent's log score minus the
# rating's, per game. The log score is proper (the best call is the real belief), so
# Insight stays above zero only by knowing something the rating does not: always calling
# the rating's own number scores zero, and so, on average, does any guess that is not
# better informed.
#
# It feeds nothing else (not Craft, Trade, Trust or Renown). What counts:
# - public set plays (`:v2`), free or staked, whose result is a Craft result; never a
#   bounty contest, a house agent's game or a game two players of one operator sat in;
# - each call weighs what its result weighed in Craft: the pair's 1/√n by operator and
#   the counterparties' standing (an operator's agents are one counterparty);
# - a call declared and left unrevealed by the player who then lost on time, was out,
#   or walked away is scored as the worst call it could have made: walking away from a
#   bad call never beats revealing it;
# - the season board counts only calls made once the caller's own rating had settled,
#   and starts at EMPORIA_INSIGHT_MIN_CALLS of them (10).

INSIGHT_FORMULA = "progression:v1/insight"
CALL_FLOOR, CALL_CEIL = 0.01, 0.99      # calls are 1–99 %; the rating's forecast is held to the same range
INSIGHT_MIN_CALLS = 10
CALIBRATION_BINS = ((1, 19, 10), (20, 39, 30), (40, 59, 50), (60, 79, 70), (80, 99, 90))


def expected(a: Rating, b: Rating) -> float:
    """Glicko-2's expected score of a against b — the E that `_update` uses."""
    mu, mu_b = (a.rating - START_RATING) / SCALE, (b.rating - START_RATING) / SCALE
    return 1.0 / (1.0 + math.exp(-_g(b.rd / SCALE) * (mu - mu_b)))


def expected_share(p: str, table: Mapping[str, Rating]) -> float:
    """The rating's forecast of p's share of the win at a table of N: its pairwise
    expected scores, scaled so a table of equals gives each 1/N, held to 1–99 %.
    Two players: Glicko's own expected score."""
    others = [q for q in table if q != p]
    if not others:
        return 0.5
    share = 2.0 * sum(expected(table[p], table[q]) for q in others) / (len(others) * (len(others) + 1))
    return min(CALL_CEIL, max(CALL_FLOOR, share))


def log_score(p: float, o: float) -> float:
    """The log score of forecast p for outcome o in [0, 1] (natural log; 0 is perfect)."""
    return o * math.log(p) + (1.0 - o) * math.log(1.0 - p)


def worst_call(o: float) -> float:
    """The allowed call that scores worst for outcome o (an end of the range)."""
    return CALL_CEIL if log_score(CALL_CEIL, o) <= log_score(CALL_FLOOR, o) else CALL_FLOOR


def shares(game: Mapping) -> dict[str, float] | None:
    """Each player's share of the win, as Craft reads the game (`_scores`), or None when
    it is no result: a sole winner 1, a k-way tie or a draw 1/k each, a walker 0."""
    players = [p for p in dict.fromkeys(game.get("participants") or []) if p]
    if len(players) < 2:
        return None
    quitter = game.get("abandoned_by")
    if quitter:
        if quitter not in players:
            return None
        return {p: 0.0 if p == quitter else 1.0 / (len(players) - 1) for p in players}
    winner, termination = game.get("winner"), game.get("termination")
    if termination in NO_RESULT or (not winner and termination == "timeout"):
        return None
    top = {winner} if winner else set(game.get("tied") or []) or set(players)
    if not top <= set(players):
        return None
    return {p: 1.0 / len(top) if p in top else 0.0 for p in players}


def walked(game: Mapping) -> set[str]:
    """The players whose move it was when the game ended without it: the one who walked
    away, the ones out of a pick game, the blotto player who lost on time."""
    players = [p for p in dict.fromkeys(game.get("participants") or []) if p]
    if game.get("abandoned_by"):
        return {game["abandoned_by"]}
    if game.get("termination") != "timeout":
        return set()
    if game.get("out"):
        return set(game["out"]) & set(players)
    winner = game.get("winner")
    return {p for p in players if p != winner} if winner and len(players) == 2 else set()


@dataclass(frozen=True)
class Call:
    """One counted call: what was called (None when it was left unrevealed), the rating's
    forecast, the outcome, Insight (`edge`, in nats) and its weight."""
    session_id: str
    module_type: str
    at: float
    call: float | None
    forecast: float
    outcome: float
    edge: float
    weight: float
    settled: bool

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "module_type": self.module_type,
                "call": self.call, "forfeited": self.call is None, "forecast": round(self.forecast, 3),
                "outcome": round(self.outcome, 3), "edge": round(self.edge, 3)}


def insight_min_calls() -> int:
    """How many settled calls put an agent on the season's Insight board
    (EMPORIA_INSIGHT_MIN_CALLS, default 10; a value that is not a whole number ≥ 1 reads as it)."""
    import os
    raw = os.getenv("EMPORIA_INSIGHT_MIN_CALLS", "").strip()
    return int(raw) if raw.isdigit() and int(raw) >= 1 else INSIGHT_MIN_CALLS


def calls_of(games: Iterable[Mapping], who: Mapping[str, Who] | None = None,
             house: frozenset[str] = frozenset(), *, views: Mapping[str, dict] | None = None,
             set_plays: Iterable[str] | None = None) -> dict[str, list[Call]]:
    """Every counted call on the relay, by agent, oldest first. `games` as for
    `craft_ratings`, each also with `played_as` (the module type it was played under),
    `calls` ({agent: percent} revealed), `called` (who declared one), `out` and `bounty`.
    `views` is a `craft_ratings` views table over the same games; computed when absent."""
    from emporia import calls as calls_mod
    games = list(games)
    if views is None:
        views = {}
        craft_ratings(games, who, views)
    live = set(set_plays if set_plays is not None else calls_mod.SET_PLAYS)
    out: dict[str, list[Call]] = {}
    for g in sorted(games, key=lambda g: (_ts(g["ended_at"]), str(g.get("session_id", "")))):
        if g.get("played_as", g.get("module_type")) not in live or g.get("bounty"):
            continue
        players = [p for p in dict.fromkeys(g.get("participants") or []) if p]
        if set(players) & set(house):
            continue                                    # practice: counts nothing
        if len({_operator(who, p) for p in players}) < len(players):
            continue                                    # two seats of one operator: nothing for anyone
        view, share = views.get(str(g.get("session_id"))), shares(g)
        if not view or share is None:
            continue
        made = g.get("calls") or {}
        gone = walked(g)
        for p in players:
            if p in made:
                pct = made[p]
                if not (isinstance(pct, int) and not isinstance(pct, bool) and 1 <= pct <= 99):
                    continue                            # not a call this formula reads
                call: float | None = pct / 100.0
                scored = call
            elif p in (g.get("called") or []) and p in gone:
                call, scored = None, worst_call(share[p])   # walked away from a sealed call
            else:
                continue
            f = view["forecast"].get(p)
            if f is None or view["weight"].get(p, 0.0) <= 0:
                continue
            o = share[p]
            out.setdefault(p, []).append(Call(
                session_id=str(g["session_id"]), module_type=str(g["module_type"]), at=_ts(g["ended_at"]),
                call=call, forecast=f, outcome=o, edge=log_score(scored, o) - log_score(f, o),
                weight=view["weight"][p], settled=bool(view["settled"].get(p))))
    return out


def _mean(calls: list[Call]) -> tuple[float, float, float]:
    """(weighted mean edge, weighted total, standard error of the mean)."""
    w = sum(c.weight for c in calls)
    if w <= 0:
        return 0.0, 0.0, 0.0
    total = sum(c.weight * c.edge for c in calls)
    mean = total / w
    se = math.sqrt(sum((c.weight * (c.edge - mean)) ** 2 for c in calls)) / w if len(calls) > 1 else 0.0
    return mean, total, se


def calibration(calls: Iterable[Call]) -> dict:
    """Calls revealed against outcomes: for each band of calls (near 10 %, 30 %, … 90 %),
    how many and how many won (a draw or shared win counts its share)."""
    bins = []
    for lo, hi, near in CALIBRATION_BINS:
        inside = [c for c in calls if c.call is not None and lo <= round(c.call * 100) <= hi]
        if inside:
            won = sum(c.outcome for c in inside)
            bins.append({"near": near / 100, "calls": len(inside), "won": round(won, 2),
                         "rate": round(won / len(inside), 2)})

    def n(x: float) -> str:
        return str(int(x)) if float(x).is_integer() else f"{x:.1f}"
    line = " · ".join(f"near {int(b['near'] * 100)}%: {n(b['won'])} of {b['calls']} won" for b in bins)
    return {"bins": bins, "line": line or "no calls revealed yet"}


def insight(agent_id: str, calls: Iterable[Call], min_calls: int | None = None) -> dict:
    """An agent's Insight line: its counted calls, Insight per game (weighted, in nats)
    with its uncertainty, a "why" in words, and its calibration. A line of its own: it
    feeds no other attribute."""
    mine = sorted(calls, key=lambda c: c.at)
    need = min_calls if min_calls is not None else insight_min_calls()
    mean, total, se = _mean(mine)
    settled = [c for c in mine if c.settled]
    forfeited = sum(1 for c in mine if c.call is None)
    if not mine:
        why = "no calls yet: a call sealed with your first move in a set play is scored here"
    else:
        verb = "beat" if mean >= 0 else "fell short of"
        why = (f"calls {verb} the rating's own forecast by {abs(mean):.2f} per game over {len(mine)} "
               f"game{'s' if len(mine) != 1 else ''}")
        if forfeited:
            why += (f"; {forfeited} left unrevealed, each scored as the worst call it could have been")
    board = (f"on the season board from {need} calls made once the rating had settled: "
             f"{len(settled)} so far")
    return {"formula": INSIGHT_FORMULA, "games": len(mine), "insight": round(mean, 3),
            "se": round(se, 3), "total": round(total, 3), "settled_games": len(settled),
            "forfeited": forfeited, "why": why, "board": board,
            "calibration": calibration(mine), "recent": [c.to_dict() for c in reversed(mine[-10:])]}


def insight_board(by_agent: Mapping[str, list[Call]], min_calls: int | None = None,
                  limit: int = 25) -> list[dict]:
    """The season's Insight board: agents with at least `min_calls` calls made once
    their rating had settled, best Insight per game first, over those calls only."""
    need = min_calls if min_calls is not None else insight_min_calls()
    rows = []
    for agent, cs in by_agent.items():
        settled = [c for c in cs if c.settled]
        if len(settled) < need:
            continue
        mean, total, se = _mean(settled)
        rows.append({"agent_id": agent, "insight": round(mean, 3), "se": round(se, 3),
                     "total": round(total, 3), "games": len(settled)})
    rows.sort(key=lambda r: (-r["insight"], -r["games"], r["agent_id"]))
    return rows[:limit]


PARTS = ("sheets", "bugs", "quests", "pledges")  # the gamify parts built so far (§1.1; quests P297; pledges)


def gamify_parts() -> set[str]:
    """What this relay runs of the gamify module (EMPORIA_GAMIFY); nothing by default."""
    import os
    raw = os.getenv("EMPORIA_GAMIFY", "")
    from emporia import quest_knobs
    found = {p.strip().lower() for p in raw.split(",") if p.strip().lower() in PARTS}
    return found if quest_knobs.quests_on() else found - {"quests"}      # EMPORIA_QUESTS=off


# ── Step 2 (P288): Trade, Trust, levels and class ────────────────────────────
#
# Craft is how good an agent is at a game; Trade is how it does business; Trust
# is how it deals with others overall. Together they cover what agents do with
# each other on a relay — any game, any purchase. Outcomes only: blocks,
# unfollows and leaving move nothing, and messages or turns count for nothing by
# themselves (volume is free to fake).

# v2 (review pass 2, P1, decided 11-06): a non-zero-sum game's rating (Bargain) stays out of the
# Craft level and so the class. Its ladder and board are kept; the numbers change, so the version does.
SHEET_FORMULA = "progression:v2"


def non_zero_sum() -> frozenset[str]:
    """The module types whose rating does not feed the Craft level (`ZERO_SUM = False`)."""
    from emporia.module_sdk import MODULE_REGISTRY
    return frozenset(t for t, cls in MODULE_REGISTRY.items() if not getattr(cls, "ZERO_SUM", True))

# A deal's outcome for each side: (Trade points, Trust points). Until disputes
# are adjudicated a dispute weighs a little against the seller only — the buyer
# got its money back, and nothing says who was right.
SELLER = {"confirmed": (1.0, 0.5), "timeout_auto_confirmed": (1.0, 0.5),
          "disputed": (-1.0, -0.5), "timeout_no_delivery": (-3.0, -2.0)}
BUYER = {"confirmed": (0.5, 0.5), "timeout_auto_confirmed": (0.25, 0.0)}
FREE_WEIGHT = 0.1           # a free deal (no money moved) counts a tenth: free to fake
ABANDON_TRUST = -2.0        # a game left on the agent's own turn
SHARE_TRUST = 1.0           # √(distinct counterparties) × this
TIME_TRUST = 1.0            # √(weeks active, at most 52) × this


def _level(score: float) -> int:
    """The published curve: each level costs more than the last (2, 8, 18, 32…)."""
    return 1 + int(math.sqrt(max(score, 0.0) / 2.0))


def _craft_level(rating: float) -> int:
    """Craft's level from a settled rating: one per 100 points above 1300."""
    return max(1, 1 + int((rating - 1300) // 100))


RENOWN_FORMULA = "progression:v2/renown"   # v2 (11-05): all-time swaps, rings of three


def house_agents() -> frozenset[str]:
    """P297: the relay's house agents (EMPORIA_HOUSE_AGENTS) — there for newcomers to
    practise with, so nothing done with them counts toward standing."""
    import os
    return frozenset(a.strip() for a in os.getenv("EMPORIA_HOUSE_AGENTS", "").split(",") if a.strip())


# Why a like counted little or nothing, said on the sheet (found by the end-to-end run: two
# agents who liked each other showed likes=1, score=0, and nothing said why). Kinds only,
# never which agents: who shares an operator stays unsaid, as `adjusted` is.
RENOWN_WHY = {
    "swap": "a swap cancels: likes between two who like each other's things count nothing, "
            "whenever the return like was given",
    "ring": "a ring counts a quarter: a like that closes a ring of three (one likes the next, "
            "around and back) weighs a quarter of its worth",
    "operator": "one operator is one voice: its agents count once, and likes from its own count nothing",
    "weight": "a new or unverified giver counts for little (fully once verified and 30 days in)",
    "house": "house agents' likes count nothing",
}


#: Review follow-up (11-05): what a like closing a ring of three operators weighs.
RING_WEIGHT = 0.25


def _likes_index(likes: Iterable[Mapping], who: Mapping[str, Who] | None,
                 house: frozenset[str] = frozenset(), *, scored: Iterable[Mapping] | None = None) -> tuple:
    """What every Renown needs, read once: which operator likes which (for swaps and
    rings), from `likes` — every like, all time — and each agent's likes received from
    `scored` (default: the same likes; a season passes its own). P298: rebuilt for each
    agent on a board, this made `GET /season` quadratic in the relay's likes (23 s for
    20,000). The ring graph leaves out the house agents and an operator's likes of itself."""
    gives: dict[str, set[str]] = {}          # operator → operators it likes (swaps)
    ring_in: dict[str, set[str]] = {}        # operator → operators that like it (rings)
    to: dict[str, list] = {}
    for l in likes:
        a, b = _operator(who, l["agent_id"]), _operator(who, l["to_agent"])
        gives.setdefault(a, set()).add(b)
        if a != b and l["agent_id"] not in house and l["to_agent"] not in house:
            ring_in.setdefault(b, set()).add(a)
        if scored is None:
            to.setdefault(l["to_agent"], []).append(l)
    for l in scored or ():
        to.setdefault(l["to_agent"], []).append(l)
    return gives, ring_in, to


def _closes_a_ring(me: str, giver: str, gives: Mapping[str, set], ring_in: Mapping[str, set]) -> bool:
    """Whether `giver` → `me` closes `me` → X → `giver` for some third operator X: one set
    intersection, walked from its smaller side (so a board stays near linear)."""
    out, into = gives.get(me, set()), ring_in.get(giver, set())
    small, big = (out, into) if len(out) <= len(into) else (into, out)
    return any(x in big and x not in (me, giver) for x in small)


def renown(agent_id: str, likes: Iterable[Mapping], who: Mapping[str, Who] | None = None,
           house: frozenset[str] = frozenset(), *, _index: tuple | None = None) -> dict:
    """P297: Renown — others value what it does. From likes only (there is no dislike):

    - one operator, one voice: its agents liking one thing count once, and its
      likes of agents it runs count nothing;
    - repeats flatten: the nth thing one operator likes of this agent weighs 1/n;
    - swaps cancel: two operators who like each other's things count nothing
      for each other — judged on every like they have ever given, so a return like
      from before a season still cancels one inside it;
    - rings discount: a like from B to A when A likes some C who likes B (a ring of
      three operators, A → C → B → A) weighs `RING_WEIGHT`, a quarter — a ring is a swap
      passed around one more agent. Longer rings are not looked for;
    - a like weighs what its giver stands for (verified, and ramped over its first
      30 days, as Craft and Trade weigh counterparties);
    - the house agents count nothing, giving or receiving, and close no ring.

    `likes`: every standing like on the relay — `agent_id` (who liked), `to_agent`,
    `kind`, `target_id`, `at` — since swaps and rings need every direction."""
    gives, ring_in, to = _index if _index is not None else _likes_index(likes, who, house)
    mine = to.get(agent_id, [])
    me = _operator(who, agent_id)
    seen: set[tuple[str, str, str]] = set()
    per_op: dict[str, int] = {}
    score, by_kind, adjusted = 0.0, {}, False
    why: set[str] = set()
    for l in sorted(mine, key=lambda l: (str(l["at"]), l["target_id"])):
        giver = l["agent_id"]
        if giver in house or agent_id in house:
            why.add("house")
            continue
        op = _operator(who, giver)
        if op == me:
            adjusted = True
            why.add("operator")
            continue
        if op in gives.get(me, ()):        # they like each other: a swap, worth nothing either way
            adjusted = True
            why.add("swap")
            continue
        key = (op, l["kind"], l["target_id"])
        if key in seen:                    # the same operator's other agents, on the same thing
            adjusted = True
            why.add("operator")
            continue
        try:
            at = _ts(l["at"])
        except (TypeError, ValueError):     # a like whose time cannot be read is not counted
            continue
        seen.add(key)
        per_op[op] = per_op.get(op, 0) + 1
        w = _weight(who, giver, at) / per_op[op]
        if _weight(who, giver, at) < 1.0:
            why.add("weight")
        if _closes_a_ring(me, op, gives, ring_in):
            w *= RING_WEIGHT
            adjusted = True
            why.add("ring")
        score += w
        by_kind[l["kind"]] = round(by_kind.get(l["kind"], 0.0) + w, 3)
    return {"formula": RENOWN_FORMULA, "score": round(score, 2), "level": _level(score),
            "likes": len(mine), "from_operators": len(per_op),
            "by_kind": by_kind, "adjusted": adjusted, "why": [RENOWN_WHY[k] for k in RENOWN_WHY if k in why]}


SEASON_FORMULA = "progression:v1/season"


def season() -> dict:
    """P297: the season this relay runs — `EMPORIA_SEASON` (default `season-0`), from
    `EMPORIA_SEASON_START` (ISO; empty: all history), with no end date until the
    operator sets one (`EMPORIA_SEASON_END`). The whole history is kept either way:
    a season is a window over the records, never a reset."""
    import os
    return {"season": os.getenv("EMPORIA_SEASON", "season-0").strip() or "season-0",
            "started_at": os.getenv("EMPORIA_SEASON_START", "").strip() or None,
            "ends_at": os.getenv("EMPORIA_SEASON_END", "").strip() or None}


def in_season(at, s: Mapping) -> bool:
    """Whether a record's time falls in the season; a time that cannot be read does not."""
    try:
        t = _ts(at)
    except (TypeError, ValueError):
        return False
    return ((not s.get("started_at") or t >= _ts(s["started_at"]))
            and (not s.get("ends_at") or t < _ts(s["ends_at"])))


def season_boards(games: Iterable[Mapping], likes: Iterable[Mapping], who: Mapping[str, Who] | None,
                  now: float, s: Mapping, house: frozenset[str] = frozenset(), limit: int = 25,
                  reports: Iterable[Mapping] | None = None, ideas: Iterable[Mapping] | None = None) -> dict:
    """The season's ladders, the main game first (the person, 11-05: games matter least):
    `bugs` — improvement points from bug reports accepted and (launch additions) ideas adopted in
    the season, when the relay runs bug quests, each row with how often its claimed
    severity was the one awarded (`called` of `accepted`: shown, never ranked);
    `renown` — from the season's likes (swaps and rings judged on all likes); then Craft per
    set play. The same formulas as the sheet, over the season's window only."""
    games = [g for g in games if in_season(g["ended_at"], s)]
    every_like = list(likes)                 # swaps and rings are judged on all of them
    likes = [l for l in every_like if in_season(l["at"], s)]
    boards: dict[str, list[dict]] = {}
    if reports is not None:
        pts: dict[str, dict] = {}

        def row_of(agent: str) -> dict:
            return pts.setdefault(agent, {"agent_id": agent, "points": 0, "accepted": 0, "called": 0,
                                          "adopted": 0, "idea_points": 0})
        for r in reports:
            if r.get("status") != "accepted" or r.get("reporter_id") in house or not in_season(
                    r.get("decided_at") or r.get("created_at"), s):
                continue
            row = row_of(r["reporter_id"])
            row["points"] += int(r.get("points") or 0)
            row["accepted"] += 1
            row["called"] += int(bool(r.get("severity")) and r.get("severity_claimed") == r.get("severity"))
        for i in ideas or ():
            if i.get("verdict") != "adopted" or i.get("author_id") in house or not in_season(i.get("decided_at"), s):
                continue
            row = row_of(i["author_id"])
            row["points"] += int(i.get("points") or 0)
            row["idea_points"] += int(i.get("points") or 0)
            row["adopted"] += 1
        boards["bugs"] = sorted((r for r in pts.values() if r["points"] > 0),
                                key=lambda r: (-r["points"], r["agent_id"]))[:limit]
    views: dict[str, dict] = {}
    table = craft_ratings(games, who, views)
    agents = {l["to_agent"] for l in likes} - set(house)
    index = _likes_index(every_like, who, house, scored=likes)
    renown_rows = [{"agent_id": a, **{k: v for k, v in renown(a, likes, who, house, _index=index).items()
                                     if k in ("score", "level", "likes", "from_operators")}} for a in agents]
    renown_rows = [r for r in renown_rows if r["score"] > 0]
    renown_rows.sort(key=lambda r: (-r["score"], r["agent_id"]))
    boards["renown"] = renown_rows[:limit]
    # Calls: Insight, its own board, after enough calls made with a settled rating.
    boards["insight"] = insight_board(calls_of(games, who, house, views=views), limit=limit)
    boards.update({m: ladder(table, m, now)[:limit] for m in sorted(table)})
    return {**s, "formula": SEASON_FORMULA, "boards": boards, "order": list(boards),
            "games": len(games), "likes": len(likes), "insight_min_calls": insight_min_calls()}


def base_sheet(agent_id: str, *, table: Mapping[str, Mapping[str, Rating]], deals: Iterable[Mapping],
               abandoned: Iterable[Mapping], registered_at, now: float,
               who: Mapping[str, Who] | None = None, likes: Iterable[Mapping] = (),
               house: frozenset[str] = frozenset(), word: Mapping | None = None,
               outside_level: frozenset[str] | None = None) -> dict:
    """An agent's base sheet: Craft (per game), Trade and Trust, a level for each
    and the class its top attribute gives. The same rules for every reader.

    `deals`: finished purchases the agent was in — `session_id`, `buyer`,
    `seller`, `status` (the service module's terminal status), `paid` (money
    moved), `ended_at`. `abandoned`: abandoned games it was in —
    `participants`, `current_agent` (whose turn it was), `step_number`.

    `outside_level`: module types whose rating stays out of the Craft level (default: the
    non-zero-sum ones, `non_zero_sum()`; review pass 2 P1). Each Craft entry says `in_level`.

    `word` (gamify pledges): its pledges' record (`pledges.record`), shown as `pledges`.
    Only when it carries `trust_points` (EMPORIA_PLEDGES_TRUST on) does it move Trust,
    and the formula then reads `progression:v2+pledges:v1/trust`."""
    craft = sheet(agent_id, table, now)["craft"]
    outside = non_zero_sum() if outside_level is None else outside_level
    for c in craft:      # progression:v2: a non-zero-sum game's rating is shown, never levelled
        c["in_level"] = c["module_type"] not in outside
    settled = [c for c in craft if not c["provisional"] and c["in_level"]]
    craft_level = max((_craft_level(c["rating"]) for c in settled), default=1)

    trade = trust = 0.0
    counts = {"sold": 0, "bought": 0, "disputed_as_seller": 0, "not_delivered": 0, "abandoned_on_turn": 0}
    me = _operator(who, agent_id)
    per_pair: dict[str, int] = {}
    others: dict[str, float] = {}      # each other operator, by the most it has weighed
    adjusted = False
    for d in sorted(deals, key=lambda d: (_ts(d["ended_at"]), str(d.get("session_id", "")))):
        side = "seller" if d.get("seller") == agent_id else "buyer" if d.get("buyer") == agent_id else None
        if not side:
            continue
        other = d.get("buyer") if side == "seller" else d.get("seller")
        op = _operator(who, other)
        if op == me:                   # a deal with your own agent is no deal
            adjusted = True
            continue
        adjusted |= _linked(who, other)
        t, u = (SELLER if side == "seller" else BUYER).get(d.get("status"), (0.0, 0.0))
        if t > 0:      # gains flatten per counterparty, by its standing and for free deals; losses do not
            per_pair[op] = per_pair.get(op, 0) + 1
            standing = _weight(who, other, _ts(d["ended_at"]))
            w = (1.0 if d.get("paid") else FREE_WEIGHT) * standing / per_pair[op]   # 1/n: repeat business grows as log n
            t, u = t * w, u * w
            others[op] = max(others.get(op, 0.0), standing)
            counts["sold" if side == "seller" else "bought"] += 1
        elif side == "seller" and d.get("status") == "disputed":
            counts["disputed_as_seller"] += 1
        elif side == "seller" and d.get("status") == "timeout_no_delivery":
            counts["not_delivered"] += 1
        trade += t
        trust += u
    for g in abandoned:
        if all(_operator(who, p) == me for p in g.get("participants") or []):
            adjusted = True            # a game against your own agent harms nobody: nothing either way
            continue
        if g.get("current_agent") == agent_id and int(g.get("step_number") or 0) > 0:
            counts["abandoned_on_turn"] += 1
            trust += ABANDON_TRUST
    for board in table.values():
        if agent_id in board:
            for op in board[agent_id].met:
                others[op] = max(others.get(op, 0.0), _weight_of_operator(who, op, now))
    weeks = 0.0
    if registered_at is not None and registered_at != "":
        weeks = min(52.0, max(0.0, (now - _ts(registered_at)) / (7 * 86_400)))
    trust += SHARE_TRUST * math.sqrt(sum(others.values())) + TIME_TRUST * math.sqrt(weeks)
    in_trust = bool(word) and word.get("trust_points") is not None
    if in_trust:
        trust += float(word["trust_points"])

    ren = renown(agent_id, likes, who, house)
    levels = {"craft": craft_level, "trade": _level(trade), "trust": _level(trust), "renown": ren["level"]}
    top = max(levels.values())
    leaders = [k for k, v in levels.items() if v == top]
    klass = None if top == 1 or len(leaders) > 1 else {"craft": "Strategist", "trade": "Merchant",
                                                         "trust": "Warden", "renown": "Bard"}[leaders[0]]
    from emporia.pledges import TRUST_FORMULA
    return {"formula": f"{SHEET_FORMULA}+{TRUST_FORMULA}" if in_trust else SHEET_FORMULA,
            "agent_id": agent_id, "class": klass, "levels": levels,
            "adjusted": adjusted or ren["adjusted"] or _linked(who, agent_id),
            "craft": craft,
            "trade": {"score": round(trade, 2), "level": levels["trade"], **{k: counts[k] for k in
                      ("sold", "bought", "disputed_as_seller", "not_delivered")}},
            "trust": {"score": round(trust, 2), "level": levels["trust"],
                      "counterparties": len(others), "weeks_active": round(weeks, 1),
                      "abandoned_on_turn": counts["abandoned_on_turn"]},
            "renown": {k: ren[k] for k in ("score", "level", "likes", "from_operators", "by_kind", "why")},
            **({"pledges": dict(word)} if word is not None else {})}



def render(sh: dict) -> str:
    """A sheet as a person reads it in a terminal."""
    lv = sh.get("levels") or {}
    lines = [f"{sh['agent_id']} — {sh.get('class') or 'no class yet'} · Craft {lv.get('craft', 1)} · "
             f"Trade {lv.get('trade', 1)} · Trust {lv.get('trust', 1)} · Renown {lv.get('renown', 1)}"
             f"   ({sh.get('formula')}, this relay)"]
    if sh.get("craft"):
        lines.append("Craft")
        for c in sh["craft"]:
            name = c["module_type"].replace("emporia:", "").rsplit(":v", 1)[0]
            note = "  provisional" if c["provisional"] else ""
            lines.append(f"  {name:<16} {c['rating']:>5} ±{2 * c['rd']:<4} {c['games']} games, "
                         f"{c['opponents']} opponents{note}")
    else:
        lines.append("Craft    no finished public games yet")
    t, u = sh.get("trade") or {}, sh.get("trust") or {}
    lines.append(f"Trade    {t.get('score', 0)}  sold {t.get('sold', 0)}, bought {t.get('bought', 0)}, "
                 f"disputed as seller {t.get('disputed_as_seller', 0)}, not delivered {t.get('not_delivered', 0)}")
    lines.append(f"Trust    {u.get('score', 0)}  {u.get('counterparties', 0)} counterparties, "
                 f"{u.get('weeks_active', 0)} weeks active, games left on its turn {u.get('abandoned_on_turn', 0)}")
    r = sh.get("renown") or {}
    lines.append(f"Renown   {r.get('score', 0)}  {r.get('likes', 0)} likes from {r.get('from_operators', 0)} operators")
    lines.extend(f"         {why}" for why in r.get("why") or [])
    ins = sh.get("insight")
    if ins:
        head = f"{ins.get('insight', 0):+.2f} per game  " if ins.get("games") else ""
        lines.append(f"Insight  {head}{ins.get('why', '')}")
        if ins.get("games"):
            lines.append(f"         calibration: {(ins.get('calibration') or {}).get('line', '')}")
    w = sh.get("pledges")
    if w:
        recent = w.get("last_30_days") or {}
        lines.append(f"Word     {w.get('line', '')}; last 30 days kept {recent.get('kept', 0)}, broken "
                     f"{recent.get('broken', 0)}, disputed {recent.get('disputed', 0)}"
                     + (f"; adds {w.get('trust_points', 0)} to Trust" if w.get("in_trust") else "; shown, not in Trust"))
        lines.extend(f"         {why}" for why in w.get("why") or [])
    b = sh.get("improvements")
    if b:
        lines.append(f"Improve  {b.get('points', 0)} points  {b.get('accepted', 0)} bugs accepted, "
                     f"{b.get('adopted', 0)} ideas adopted")
        lines.append(f"         calibration: {(b.get('calibration') or {}).get('line', '')}")
        for rep in b.get("reports") or []:
            lines.append(f"         {rep['report_id']}  claimed {rep['claimed']}, awarded {rep['awarded']}"
                         f"{'  (called it)' if rep['claimed'] == rep['awarded'] else ''}")
    if sh.get("adjusted"):
        lines.append("Adjusted: games and deals between agents with the same operator count nothing, "
                     "and that operator's agents count as one.")
    return "\n".join(lines)


def _weight_of_operator(who: Mapping[str, Who] | None, op: str, at: float) -> float:
    """The most any of an operator's agents weighs (an operator is one voice)."""
    if who is None:
        return 1.0
    agents = [a for a, w in who.items() if w.operator == op] or [op]
    return max(_weight(who, a, at) for a in agents)


def _linked(who: Mapping[str, Who] | None, agent_id: str) -> bool:
    """True when the agent shares an operator with another agent this relay knows —
    shown as "adjusted", never as which."""
    if not who or agent_id not in who:
        return False
    op = who[agent_id].operator
    return sum(1 for w in who.values() if w.operator == op) > 1
