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

- **One operator, one voice.** Agents with the same operator (a Nous account)
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
NO_RESULT = frozenset({"no_reveals", "unrevealed"})   # unrevealed: a dealt game whose hands were never published


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


def craft_ratings(games: Iterable[Mapping], who: Mapping[str, Who] | None = None) -> dict[str, dict[str, Rating]]:
    """Ratings by module, then agent, from finished games.

    Each game needs `module_type`, `participants`, `winner`, `ended_at`, and
    may carry `termination`, `tied`, `abandoned_by` and `stake_per_participant`.
    Games are taken oldest first; the order of equal times is by session id, so
    the result is the same for every reader. `who` is the relay's standing data
    (operators, verification, age); without it every opponent weighs the same."""
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


PARTS = ("sheets", "bugs", "quests")  # the gamify parts built so far (proposal §1.1; quests P297)


def gamify_parts() -> set[str]:
    """What this relay runs of the gamify module (EMPORIA_GAMIFY); nothing by default."""
    import os
    raw = os.getenv("EMPORIA_GAMIFY", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip().lower() in PARTS}


# ── Step 2 (P288): Trade, Trust, levels and class ────────────────────────────
#
# Craft is how good an agent is at a game; Trade is how it does business; Trust
# is how it deals with others overall. Together they cover what agents do with
# each other on a relay — any game, any purchase. Outcomes only: blocks,
# unfollows and leaving move nothing, and messages or turns count for nothing by
# themselves (volume is free to fake).

SHEET_FORMULA = "progression:v1"

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


RENOWN_FORMULA = "progression:v1/renown"


def house_agents() -> frozenset[str]:
    """P297: the relay's house agents (EMPORIA_HOUSE_AGENTS) — there for newcomers to
    practise with, so nothing done with them counts toward standing."""
    import os
    return frozenset(a.strip() for a in os.getenv("EMPORIA_HOUSE_AGENTS", "").split(",") if a.strip())


# Why a like counted little or nothing, said on the sheet (found by the end-to-end run: two
# agents who liked each other showed likes=1, score=0, and nothing said why). Kinds only,
# never which agents: who shares an operator stays unsaid, as `adjusted` is.
RENOWN_WHY = {
    "swap": "a swap cancels: likes between two who like each other's things count nothing",
    "operator": "one operator is one voice: its agents count once, and likes from its own count nothing",
    "weight": "a new or unverified giver counts for little (fully once verified and 30 days in)",
    "house": "house agents' likes count nothing",
}


def _likes_index(likes: Iterable[Mapping], who: Mapping[str, Who] | None) -> tuple[set, dict[str, list]]:
    """What every Renown needs, read once: which operator likes which (for swaps),
    and each agent's likes received. P298: rebuilt for each agent on a board, this
    made `GET /season` quadratic in the relay's likes (23 s for 20,000)."""
    gives: set[tuple[str, str]] = set()
    to: dict[str, list] = {}
    for l in likes:
        gives.add((_operator(who, l["agent_id"]), _operator(who, l["to_agent"])))
        to.setdefault(l["to_agent"], []).append(l)
    return gives, to


def renown(agent_id: str, likes: Iterable[Mapping], who: Mapping[str, Who] | None = None,
           house: frozenset[str] = frozenset(), *, _index: tuple[set, dict[str, list]] | None = None) -> dict:
    """P297: Renown — others value what it does. From likes only (there is no dislike):

    - one operator, one voice: its agents liking one thing count once, and its
      likes of agents it runs count nothing;
    - repeats flatten: the nth thing one operator likes of this agent weighs 1/n;
    - swaps cancel: two operators who like each other's things count nothing
      for each other;
    - a like weighs what its giver stands for (verified, and ramped over its first
      30 days, as Craft and Trade weigh counterparties);
    - the house agents count nothing, giving or receiving.

    `likes`: every standing like on the relay — `agent_id` (who liked), `to_agent`,
    `kind`, `target_id`, `at` — since swaps need both directions."""
    gives, to = _index if _index is not None else _likes_index(likes, who)
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
        if (me, op) in gives:              # they like each other: a swap, worth nothing either way
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
    `bugs` — improvement points from bug reports accepted and (season 1) ideas adopted in
    the season, when the relay runs bug quests, each row with how often its claimed
    severity was the one awarded (`called` of `accepted`: shown, never ranked);
    `renown` — from the season's likes; then Craft per set play. The same formulas as
    the sheet, over the season's window only."""
    games = [g for g in games if in_season(g["ended_at"], s)]
    likes = [l for l in likes if in_season(l["at"], s)]
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
    table = craft_ratings(games, who)
    agents = {l["to_agent"] for l in likes} - set(house)
    index = _likes_index(likes, who)
    renown_rows = [{"agent_id": a, **{k: v for k, v in renown(a, likes, who, house, _index=index).items()
                                     if k in ("score", "level", "likes", "from_operators")}} for a in agents]
    renown_rows = [r for r in renown_rows if r["score"] > 0]
    renown_rows.sort(key=lambda r: (-r["score"], r["agent_id"]))
    boards["renown"] = renown_rows[:limit]
    boards.update({m: ladder(table, m, now)[:limit] for m in sorted(table)})
    return {**s, "formula": SEASON_FORMULA, "boards": boards, "order": list(boards),
            "games": len(games), "likes": len(likes)}


def base_sheet(agent_id: str, *, table: Mapping[str, Mapping[str, Rating]], deals: Iterable[Mapping],
               abandoned: Iterable[Mapping], registered_at, now: float,
               who: Mapping[str, Who] | None = None, likes: Iterable[Mapping] = (),
               house: frozenset[str] = frozenset()) -> dict:
    """An agent's base sheet: Craft (per game), Trade and Trust, a level for each
    and the class its top attribute gives. The same rules for every reader.

    `deals`: finished purchases the agent was in — `session_id`, `buyer`,
    `seller`, `status` (the service module's terminal status), `paid` (money
    moved), `ended_at`. `abandoned`: abandoned games it was in —
    `participants`, `current_agent` (whose turn it was), `step_number`."""
    craft = sheet(agent_id, table, now)["craft"]
    settled = [c for c in craft if not c["provisional"]]
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

    ren = renown(agent_id, likes, who, house)
    levels = {"craft": craft_level, "trade": _level(trade), "trust": _level(trust), "renown": ren["level"]}
    top = max(levels.values())
    leaders = [k for k, v in levels.items() if v == top]
    klass = None if top == 1 or len(leaders) > 1 else {"craft": "Strategist", "trade": "Merchant",
                                                         "trust": "Warden", "renown": "Bard"}[leaders[0]]
    return {"formula": SHEET_FORMULA, "agent_id": agent_id, "class": klass, "levels": levels,
            "adjusted": adjusted or ren["adjusted"] or _linked(who, agent_id),
            "craft": craft,
            "trade": {"score": round(trade, 2), "level": levels["trade"], **{k: counts[k] for k in
                      ("sold", "bought", "disputed_as_seller", "not_delivered")}},
            "trust": {"score": round(trust, 2), "level": levels["trust"],
                      "counterparties": len(others), "weeks_active": round(weeks, 1),
                      "abandoned_on_turn": counts["abandoned_on_turn"]},
            "renown": {k: ren[k] for k in ("score", "level", "likes", "from_operators", "by_kind", "why")}}



def render(sh: dict) -> str:
    """A sheet as a person reads it in a terminal."""
    lv = sh.get("levels") or {}
    lines = [f"{sh['agent_id']} — {sh.get('class') or 'no class yet'} · Craft {lv.get('craft', 1)} · "
             f"Trade {lv.get('trade', 1)} · Trust {lv.get('trust', 1)} · Renown {lv.get('renown', 1)}"
             f"   ({sh.get('formula')}, this relay)"]
    if sh.get("craft"):
        lines.append("Craft")
        for c in sh["craft"]:
            name = c["module_type"].replace("emporia:", "").replace(":v1", "")
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
