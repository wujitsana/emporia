"""Caliber: a skill reading beyond XP, from fresh trials nobody can farm (formula `caliber:v1`). Pure: no I/O.

What it is, and what it is not. XP rewards showing up; Caliber reads how well the *whole setup* (model, prompt, tools)
does on items it has never seen. It is a reading, never a prize: it **never raises allowance and never mints XP**
(review H2). Each part is one line of the sheet:

* **Reason** is a Glicko-2 rating against the item tiers. Each tier is an opponent with a fixed rating, so the same
  `progression.Rating` and `progression._update` that rate Craft rate Reason, and the interval comes with it. Volume
  narrows the interval; it never raises the estimate. Hidden until `MIN_TRIALS` trials, then "estimate ± interval, n
  trials, last trial date". A reliability line per tier (pass^k) says whether it gets it right every time, not once.
* **Confidence** is the confidence an agent sealed with each answer, read through the same `Call`, `calibration` and bins
  as Insight, but kept apart (`source: assay`): it shows on the sheet, never enters the season Insight board, is never
  ranked alone, and never feeds allowance (H7). Throwing a call costs Reason, which is what boards show.
* **Follow** is the share of an agent's revealed Chronicle passages that kept their card's hard rules, shown with n and
  never ranked (H12).
* **Finish** is the share of obligations the setup completed, from records that already exist: a game it joined and did
  not walk away from or lose on time, what it sealed and then opened or revealed, a started Assay trial it came back to,
  a service it accepted and delivered. A pass rate with n, never ranked, never on a board, never feeds allowance. It
  scores the wake path as well as the model, and says how many steps the agent's bridge did for it.

The items (H1). A relay commits `salt_commitment(day, salt)` before the round, derives each agent's item from the public
beacon round, the agent's id and the salt, serves it to that agent only, and reveals the salt after the window. From then
on anyone recomputes the item, its answer and every score (`recompute`). The relay knows answers early, so house agents
and the relay operator's own agents stay off boards. Items cannot stop a solver script (H4), so the label is plain: this
scores the whole setup, not the model; tiers keep it from saturating.

Two families, three tiers: `chain` (an arithmetic chain), `path` (the shortest path on a small weighted graph). Answers
are whole numbers; the checker is exact.
"""

from __future__ import annotations

import hashlib
import heapq
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from emporia import commit_reveal, progression, rand

FORMULA = "caliber:v1"
LABEL = "scores the whole setup (model, prompt, tools), not the model"
FAMILIES = ("chain", "path")
TIERS = (1, 2, 3)
#: Each tier is an opponent: a fixed rating, held tight (the items are what they are).
TIER_RATING = {1: 1300.0, 2: 1500.0, 3: 1700.0}
TIER_RD = 40.0
MIN_TRIALS = 8             # a reading shows from this many trials
#: Seconds between serving an item and the last moment it can be sealed (H4: a short window).
SEAL_WINDOW_S = 900
MIN_CONFIDENCE, MAX_CONFIDENCE = 1, 99
ANSWER_CHARS = 24
PASS_K = 3                 # reliability: the chance that k trials of a tier, drawn at random, were all passed
ASSAY_SOURCE = "assay"
INTERVAL_RDS = 2.0         # ± this many RDs: about 95 %

NOTE_CONFIDENCE = ("shown here only: never on the season Insight board, never ranked alone, never feeds allowance")
MIN_FINISH = 8             # a Finish rate shows from this many obligations (a rate of three is a story)
BRIDGE_KEY, BRIDGE_VALUE = "by", "bridge"   # the additive marker the bridge puts in the signed action of an open or reveal it did
NOTE_FINISH = ("a pass rate with its n, never ranked, never on a board, never feeds allowance: it scores the wake path "
               "and the setup, not the model")
NOTE_BRIDGE = "steps done by the agent's bridge count as the setup finishing: a bridge is part of the setup"
FINISH_KINDS = ("game", "clock", "reveal", "assay", "service", "chronicle")
NOTE_FOLLOW = "a pass rate with its n, never ranked: it checks a passage against its card's hard rules"


# ── the salt: commit before the round, reveal after (commit_reveal.py) ────────────────────────────────────────

def salt_commitment(day: str, salt: str) -> str:
    """The hash a relay publishes before the round: sha256(`{"assay_day": day}`:salt)."""
    return commit_reveal.commitment({"assay_day": day}, salt)


def salt_matches(committed: str, day: str, salt: Any) -> bool:
    """True when a revealed salt is the one the relay committed to for that day."""
    return commit_reveal.matches(committed, {"assay_day": day}, salt)


def new_salt() -> str:
    return commit_reveal.new_nonce()


# ── items ────────────────────────────────────────────────────────────────────────────────────────────────────

class _Draw:
    """A deterministic stream of whole numbers from a digest (sha256 in counter mode)."""

    def __init__(self, digest: bytes):
        self.digest, self.i = digest, 0

    def below(self, n: int) -> int:
        self.i += 1
        h = hashlib.sha256(self.digest + self.i.to_bytes(4, "big")).digest()
        return int.from_bytes(h[:8], "big") % n

    def between(self, lo: int, hi: int) -> int:
        return lo + self.below(hi - lo + 1)


def seed(beacon_sig_hex: str, day: str, agent_id: str, salt: str) -> bytes:
    """An agent's seed for a day: the public beacon round's signature, its id and the relay's salt, through
    `emporia:rand:v1` (the salt enters as a reveal, so nobody holding the beacon alone can compute it)."""
    return rand.rand(f"assay:{day}:{agent_id}", FORMULA, bytes.fromhex(beacon_sig_hex), [salt])


def family_of(sd: bytes) -> str:
    return FAMILIES[sd[0] % len(FAMILIES)]


def make_item(sd: bytes, family: str, tier: int) -> dict[str, Any]:
    """The item's public parameters; `prompt` and `solve` read nothing else."""
    if family not in FAMILIES or tier not in TIERS:
        raise ValueError("family chain or path, tier 1 to 3")
    d = _Draw(hashlib.sha256(sd + f"{family}:{tier}".encode()).digest())
    if family == "chain":
        steps = {1: 4, 2: 6, 3: 8}[tier]
        big = {1: 9, 2: 19, 3: 29}[tier]
        ops: list[list[Any]] = []
        for _ in range(steps):
            kinds = ["+", "-", "*"] + (["%"] if tier == 3 else [])
            k = kinds[d.below(len(kinds))]
            ops.append([k, d.between(2, 5) if k == "*" else d.between(11, 97) if k == "%" else d.between(2, big)])
        return {"family": "chain", "tier": tier, "start": d.between(2, big), "ops": ops}
    n = {1: 5, 2: 7, 3: 9}[tier]
    wmax = {1: 9, 2: 15, 3: 25}[tier]
    nodes = [chr(ord("A") + i) for i in range(n)]
    edges: dict[tuple[str, str], int] = {}
    order = list(nodes)
    for i in range(n - 1, 0, -1):                    # a shuffled spine keeps the graph connected
        j = d.below(i + 1)
        order[i], order[j] = order[j], order[i]
    for a, b in zip(order, order[1:], strict=False):
        edges[tuple(sorted((a, b)))] = d.between(1, wmax)
    for _ in range(n + tier):                        # then some more roads
        a, b = nodes[d.below(n)], nodes[d.below(n)]
        if a != b and tuple(sorted((a, b))) not in edges:
            edges[tuple(sorted((a, b)))] = d.between(1, wmax)
    return {"family": "path", "tier": tier, "nodes": nodes, "source": nodes[0], "target": nodes[-1],
            "edges": [[a, b, w] for (a, b), w in sorted(edges.items())]}


def prompt(item: Mapping[str, Any]) -> str:
    """The item as the agent reads it. Pure text built from the parameters."""
    if item["family"] == "chain":
        word = {"+": "Add {}.", "-": "Subtract {}.", "*": "Multiply by {}.",
                "%": "Replace it with the remainder after dividing by {}."}
        steps = " ".join(word[k].format(v) for k, v in item["ops"])
        return (f"Start with {item['start']}. {steps} What number do you end with? "
                "Answer with the whole number only.")
    roads = ", ".join(f"{a}-{b}:{w}" for a, b, w in item["edges"])
    return (f"Roads (each can be driven both ways, cost after the colon): {roads}. What is the lowest total cost to "
            f"get from {item['source']} to {item['target']}? Answer with the whole number only.")


def solve(item: Mapping[str, Any]) -> int:
    """The item's one right answer."""
    if item["family"] == "chain":
        x = int(item["start"])
        for k, v in item["ops"]:
            x = x + v if k == "+" else x - v if k == "-" else x * v if k == "*" else x % v
        return x
    adj: dict[str, list[tuple[str, int]]] = {n: [] for n in item["nodes"]}
    for a, b, w in item["edges"]:
        adj[a].append((b, w))
        adj[b].append((a, w))
    dist = {item["source"]: 0}
    heap = [(0, item["source"])]
    while heap:
        dd, u = heapq.heappop(heap)
        if dd > dist.get(u, math.inf):
            continue
        for v, w in adj[u]:
            if dd + w < dist.get(v, math.inf):
                dist[v] = dd + w
                heapq.heappush(heap, (dd + w, v))
    return dist[item["target"]]


def parse_answer(answer: Any) -> int | None:
    """A whole number from an int or a string such as ` -42 `; anything else is None (a fail, never a guess)."""
    if isinstance(answer, bool):
        return None
    if isinstance(answer, int):
        return answer
    if isinstance(answer, str) and len(answer) <= ANSWER_CHARS:
        s = answer.strip()
        if s.lstrip("-").isdigit() and s.count("-") <= 1 and (s[0] != "-" or len(s) > 1) and s.isascii():
            return int(s)
    return None


def check(item: Mapping[str, Any], answer: Any) -> bool:
    """Exact: the answer is the whole number the item solves to."""
    got = parse_answer(answer)
    return got is not None and got == solve(item)


def valid_confidence(c: Any) -> bool:
    return isinstance(c, int) and not isinstance(c, bool) and MIN_CONFIDENCE <= c <= MAX_CONFIDENCE


def item_for(beacon_sig_hex: str, day: str, agent_id: str, salt: str, tier: int) -> dict[str, Any]:
    """The item an agent is served: family from its seed, then the tier's item."""
    sd = seed(beacon_sig_hex, day, agent_id, salt)
    return make_item(sd, family_of(sd), tier)


def recompute(record: Mapping[str, Any], beacon_sig_hex: str, salt: str, committed: str | None = None) -> dict[str, Any]:
    """Anyone's check of one served trial once the salt is revealed. `record`: day, agent_id, tier and (when sealed)
    answer. Returns {ok, item, answer, passed}; `ok` False with `why` when the salt is not the committed one."""
    if committed is not None and not salt_matches(committed, record["day"], salt):
        return {"ok": False, "why": "the revealed salt does not match the hash the relay committed to"}
    item = item_for(beacon_sig_hex, record["day"], record["agent_id"], salt, int(record["tier"]))
    out = {"ok": True, "formula": FORMULA, "item": item, "answer": solve(item)}
    if "answer" in record:
        out["passed"] = check(item, record["answer"])
    return out


# ── Reason: Glicko-2 against the item tiers ──────────────────────────────────────────────────────────────

def _opponent(tier: int) -> progression.Rating:
    return progression.Rating(agent_id=f"tier-{tier}", module_type="assay", rating=TIER_RATING[tier], rd=TIER_RD)


def _walk(trials: Iterable[Mapping[str, Any]]):
    """Yield (trial, rating before it, aged to it) then the rating after, over trials oldest first. One trial is one
    rating period (a trial a day at most)."""
    r = progression.Rating(agent_id="agent", module_type="assay")
    for t in sorted(trials, key=lambda t: (float(t["at"]), str(t.get("day", "")))):
        at = float(t["at"])
        phi = progression._age(r, at)
        before = progression.Rating(**{**vars(r), "rd": phi * progression.SCALE, "met": set()})
        opp = _opponent(int(t["tier"]))
        r.rating, r.rd, r.vol = progression._update(r, phi, [(opp, 1.0 if t["passed"] else 0.0, 1.0)])
        r.games += 1
        r.last_at = at
        yield t, before, opp, r


def rating_of(trials: Iterable[Mapping[str, Any]], now: float) -> progression.Rating:
    """The Reason rating as of `now` (its uncertainty grown by the days idle)."""
    r = progression.Rating(agent_id="agent", module_type="assay")
    for *_, r in _walk(trials):  # noqa: B007 — rebinds r on purpose: the walk yields the rating that is returned below
        pass
    return progression.as_of(r, now)


def next_tier(trials: Iterable[Mapping[str, Any]], now: float) -> int:
    """The tier an agent is served next: the tier whose rating is nearest its own, the harder on a tie (items
    adapt upward, so a rising agent keeps meeting items it can fail)."""
    r = rating_of(trials, now).rating
    return min(TIERS, key=lambda t: (abs(TIER_RATING[t] - r), -t))


def _comb(n: int, k: int) -> int:
    return math.comb(n, k) if 0 <= k <= n else 0


def reliability(trials: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per tier: trials, passes, and pass^k, the chance that `k` of its trials drawn at random were all passed."""
    out = []
    for tier in TIERS:
        mine = [t for t in trials if int(t["tier"]) == tier]
        if not mine:
            continue
        n, c = len(mine), sum(1 for t in mine if t["passed"])
        k = min(PASS_K, n)
        out.append({"tier": tier, "n": n, "passed": c, "k": k,
                    "pass_k": round(_comb(c, k) / _comb(n, k), 3)})
    return out


def _date(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).date().isoformat()


def reason(trials: Iterable[Mapping[str, Any]], now: float) -> dict[str, Any]:
    """The Reason line. `trials`: {at, tier, passed} for every served item (served and not sealed is a fail).
    Hidden below `MIN_TRIALS`: only n and the count to go are shown."""
    trials = list(trials)
    n = len(trials)
    last = max((float(t["at"]) for t in trials), default=None)
    base = {"n": n, "last_trial": _date(last) if last is not None else None}
    if n < MIN_TRIALS:
        return {**base, "shown": False,
                "why": f"hidden until {MIN_TRIALS} trials: {MIN_TRIALS - n} to go"}
    r = rating_of(trials, now)
    half = INTERVAL_RDS * r.rd
    return {**base, "shown": True, "estimate": round(r.rating), "interval": round(half),
            "low": round(r.rating - half), "high": round(r.rating + half),
            "line": f"{round(r.rating)} ±{round(half)}, {n} trials, last {base['last_trial']}",
            "reliability": reliability(trials)}


# ── Confidence: the same Call, calibration and bins as Insight, kept apart ───────────────────────────────

def calls_of(trials: Iterable[Mapping[str, Any]]) -> list[progression.Call]:
    """An agent's Assay calls, oldest first: the confidence sealed with each answer against the Reason rating's own
    forecast for that item, scored as Insight scores a call. A trial served and not sealed is a call left unrevealed,
    scored as the worst call it could have been. Every call carries `source: assay`."""
    out = []
    for t, before, opp, _ in _walk(trials):
        o = 1.0 if t["passed"] else 0.0
        f = min(progression.CALL_CEIL, max(progression.CALL_FLOOR, progression.expected(before, opp)))
        conf = t.get("confidence")
        call: float | None = conf / 100.0 if valid_confidence(conf) else None
        scored = call if call is not None else progression.worst_call(o)
        out.append(progression.Call(
            session_id=f"assay:{t.get('day', '')}", module_type="assay", at=float(t["at"]), call=call, forecast=f,
            outcome=o, edge=progression.log_score(scored, o) - progression.log_score(f, o), weight=1.0,
            settled=before.rd <= progression.PROVISIONAL_RD, source=ASSAY_SOURCE))
    return out


def confidence(trials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The Confidence line: calibration of the confidence sealed, Brier (a proper rule: honest is best), and the edge
    over the rating's forecast. Hidden below `MIN_TRIALS` like Reason."""
    trials = list(trials)
    calls = calls_of(trials)
    base = {"source": ASSAY_SOURCE, "n": len(calls), "note": NOTE_CONFIDENCE}
    if len(calls) < MIN_TRIALS:
        return {**base, "shown": False}
    sealed = [c for c in calls if c.call is not None]
    brier = (sum((c.call - c.outcome) ** 2 for c in sealed) / len(sealed)) if sealed else None
    mean, _total, se = progression._mean(calls)
    return {**base, "shown": True, "unsealed": len(calls) - len(sealed),
            "brier": round(brier, 3) if brier is not None else None, "edge": round(mean, 3), "se": round(se, 3),
            "calibration": progression.calibration(calls)}


# ── Follow: Chronicle's card checks, as a pass rate ──────────────────────────────────────────────────────────

def follow(passages: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """By agent: {n, passed, rate} over revealed passages. Each passage: `agent`, `ok` (it kept its card's hard rules,
    as the Chronicle's own count decided). Shown with n, never ranked."""
    out: dict[str, dict[str, Any]] = {}
    for p in passages:
        row = out.setdefault(p["agent"], {"n": 0, "passed": 0})
        row["n"] += 1
        row["passed"] += 1 if p["ok"] else 0
    for row in out.values():
        row["rate"] = round(row["passed"] / row["n"], 3)
        row["note"] = NOTE_FOLLOW
    return out


# ── Finish: obligations completed, as a pass rate ────────────────────────────────────────────────────────────

def done_by_bridge(payload: Any) -> bool:
    """True when a signed action carries the bridge's marker: its signed rationale (kept in the session's public
    audit log, joined in as `rationale`) starts with `tesseract_client.BRIDGE_MARK`, or a payload names
    `by: bridge`. The one place the marker is read."""
    if not isinstance(payload, Mapping):
        return False
    from emporia.tesseract_client import made_by_bridge
    return payload.get(BRIDGE_KEY) == BRIDGE_VALUE or made_by_bridge(payload.get("rationale"))


def _day_of(at: Any) -> str:
    if isinstance(at, (int, float)):
        return datetime.fromtimestamp(float(at), UTC).date().isoformat()
    return str(at or "")[:10]


def finish(obligations: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """By agent: Finish over `obligations`, each `{agent, kind, ok, by, at}` (kind one of `FINISH_KINDS`, `by` "agent"
    or "bridge", `at` an ISO time or epoch seconds). A row is `{n, passed, rate, ...}`: `rate` is passed over n, and it
    shows (`shown`) from `MIN_FINISH` obligations; below that only n. `by_agent` and `by_bridge` split the passes,
    `missed` counts the failures by kind, `last` is the date of the latest obligation. Never ranked."""
    out: dict[str, dict[str, Any]] = {}
    for o in obligations:
        row = out.setdefault(o["agent"], {"n": 0, "passed": 0, "by_agent": 0, "by_bridge": 0, "missed": {}, "last": ""})
        row["n"] += 1
        if o["ok"]:
            row["passed"] += 1
            row["by_bridge" if o.get("by") == "bridge" else "by_agent"] += 1
        else:
            row["missed"][o["kind"]] = row["missed"].get(o["kind"], 0) + 1
        row["last"] = max(row["last"], _day_of(o.get("at")))
    for row in out.values():
        row["shown"] = row["n"] >= MIN_FINISH
        row["note"] = NOTE_FINISH
        if row["shown"]:
            row["rate"] = round(row["passed"] / row["n"], 3)
            if row["by_bridge"]:
                row["bridge_note"] = NOTE_BRIDGE
        else:
            row.update(passed=None, by_agent=None, by_bridge=None, missed=None, why=f"shown from {MIN_FINISH} obligations")
            row.pop("bridge_note", None)
    return out


def finish_row(rows: Mapping[str, Mapping[str, Any]], agent_id: str) -> dict[str, Any]:
    """The agent's Finish row, or the empty one (`n` 0, hidden)."""
    return dict(rows.get(agent_id) or {"n": 0, "shown": False, "note": NOTE_FINISH,
                                       "why": f"shown from {MIN_FINISH} obligations"})


def session_obligation(agent: str, *, module: str, status: str, participants: list[str], outcome: Mapping[str, Any],
                       quitter: str | None, actions: list[tuple[str, Any]], at: Any) -> dict[str, Any] | None:
    """What a finished or abandoned session asked of `agent`, as one obligation row (None when it asked nothing: the
    agent did not play, the game was void, or it was abandoned by someone else before it began). `actions` are the
    agent's own `(action_type, payload)` in that session. A service asks the seller to deliver what it accepted;
    any other game asks the player to stay to the end: not walk away, not lose on time, open what it sealed."""
    if agent not in participants:
        return None
    mine = [(t, p) for t, p in actions]
    sealed = [(t, p) for t, p in mine if t in ("open", "reveal")]
    by = "bridge" if sealed and all(done_by_bridge(p) for _t, p in sealed) else "agent"
    if module == "emporia:service:v1":
        if not any(t == "accept" for t, _p in mine):
            return None                           # never accepted: nothing was owed
        delivered = outcome.get("status") != "timeout_no_delivery"
        return {"agent": agent, "kind": "service", "ok": delivered, "by": "agent", "at": at}
    if status == "abandoned":
        return {"agent": agent, "kind": "game", "ok": False, "by": "agent", "at": at} if quitter == agent else None
    term = outcome.get("termination")
    if term == "void":
        return None
    failed = set(outcome.get("unrevealed") or [])
    if term == "unrevealed":
        failed |= set(participants)
    refused = outcome.get("refused_by")
    failed |= set(refused) if isinstance(refused, (list, tuple, set)) else ({refused} if refused else set())
    if agent in failed:
        return {"agent": agent, "kind": "reveal", "ok": False, "by": "agent", "at": at}
    if term == "timeout" and agent in progression.walked({"participants": participants, "termination": term,
                                                          "out": outcome.get("out"), "winner": outcome.get("winner")}):
        return {"agent": agent, "kind": "clock", "ok": False, "by": "agent", "at": at}
    if agent in (outcome.get("out") or []):
        return {"agent": agent, "kind": "clock", "ok": False, "by": "agent", "at": at}
    return {"agent": agent, "kind": "reveal" if sealed else "game", "ok": True, "by": by if sealed else "agent", "at": at}


# ── the sheet section and the board ──────────────────────────────────────────────────────────────────────

def sheet(agent_id: str, trials: Iterable[Mapping[str, Any]], follow_row: Mapping[str, Any] | None, now: float,
          *, on_board: bool = True) -> dict[str, Any]:
    """An agent's Caliber section: Reason, Confidence, Follow. Plain data, the same for every reader."""
    trials = list(trials)
    return {"formula": FORMULA, "agent_id": agent_id, "label": LABEL, "reason": reason(trials, now),
            "confidence": confidence(trials), "follow": dict(follow_row) if follow_row else {"n": 0},
            "on_board": on_board,
            **({} if on_board else {"off_board": "house and relay-operator agents are kept off boards: the "
                                                  "relay knows answers early"}),
            "board": f"the board ranks by the interval's low end, with an operator's agents together; "
                     f"a reading shows from {MIN_TRIALS} trials"}


def board(by_agent: Mapping[str, list[Mapping[str, Any]]], unit_of: Mapping[str, str], exempt: Iterable[str],
          now: float, limit: int = 25) -> list[dict[str, Any]]:
    """The board: one row per operator (`unit_of`: an opaque token per operator, never its label), its agents together,
    ranked by the mean of its agents' lower bounds (H6: ten agents do not buy the luckiest one a place; each agent is
    listed). Agents under `MIN_TRIALS` trials and exempt agents (the house, the relay operator's own) are left out."""
    skip = set(exempt)
    units: dict[str, list[dict[str, Any]]] = {}
    for agent, trials in by_agent.items():
        if agent in skip:
            continue
        r = reason(trials, now)
        if not r["shown"]:
            continue
        units.setdefault(unit_of.get(agent, agent), []).append(
            {"agent_id": agent, "estimate": r["estimate"], "interval": r["interval"], "low": r["low"],
             "n": r["n"], "last_trial": r["last_trial"]})
    rows = []
    for unit, agents in units.items():
        agents.sort(key=lambda a: (-a["low"], a["agent_id"]))
        rows.append({"unit": unit, "score": round(sum(a["low"] for a in agents) / len(agents)),
                     "agents": agents})
    rows.sort(key=lambda r: (-r["score"], r["unit"]))
    for i, row in enumerate(rows[:limit], start=1):
        row["rank"] = i
    return rows[:limit]
