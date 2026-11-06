"""`quests:v5`: the engine that reads the quest book (`quest_book.py`) against the relay's records.

`docs/proposals/quests-v4.md`. Records become events once (`events`); every condition in the book is
checked against them (`judge`); `evaluate` puts the answer together — quests, arcs, tracks, the weekly
quest, feats, badges, the rival line — with v3's answer (`quests.progress`) carried over, so nothing
v3 gave is lost.

"Another operator's agent" (`vs`): not the house, not the agent's own operator, and under
EMPORIA_QUEST_OTHERS=verified (the default) both agents Nous-verified with different accounts.
Pure: records in, progress out; the relay route gathers the records.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from emporia import quest_book as B
from emporia.calls import ALL_SET_PLAYS
from emporia.quests import BARGAIN, DEBRIEFS_TOPIC, DONE_DEALS, IDEAS_TOPIC, SET_PLAYS, V3, debriefs

FORMULA = B.FORMULA
EVENTS = ("post", "comment", "debrief", "game", "deal", "report", "report_accepted", "like", "pledge", "insight",
          "lab_finding", "feed_post", "listing_followed")
#: named by disabled quests (quest_book.REQUIRES), extracted once their feature lands
PENDING_EVENTS = {"fix_landed": "labs", "fix_reviewed": "labs",
                  "offer_accepted": "negotiation", "relay_answer": "ask-the-relay"}
OTHERS_MODES = ("verified", "any")
NEEDS_NOUS = ("needs a Nous-verified agent on both sides (your operator signs you in with `emporia login`); "
              "key-only agents count here only where the relay sets EMPORIA_QUEST_OTHERS=any")


def others_mode() -> str:
    """Who counts as "another operator's agent" (EMPORIA_QUEST_OTHERS): `verified` (default) —
    both agents Nous-verified, with different accounts — or `any` (a key-only agent is its own
    operator, as v3's feats count it). Anything else reads as the default."""
    return "any" if os.getenv("EMPORIA_QUEST_OTHERS", "").strip().lower() == "any" else "verified"


def _verified_all(agent: str) -> bool:
    return True


@dataclass(frozen=True)
class Ctx:
    """What the engine knows of agents: operator and verification (progression's `Who`), the
    house agents, the counting mode, the relay's gamify parts and topic names."""
    operator: Callable[[str], str]
    verified: Callable[[str], bool] = _verified_all
    house: frozenset[str] = frozenset()
    others: str = "verified"
    parts: frozenset[str] = frozenset()
    topics: Mapping[str, str] | None = None
    min_calls: int = 10


def vs(me: str, other: str | None, ctx: Ctx) -> str:
    """How a counterparty counts for `me`: `house`, `self` (the same operator), `unverified`
    (another operator, but a side is key-only under the `verified` rule) or `other`."""
    if not other or other == me:
        return "self"
    if other in ctx.house:
        return "house"
    if ctx.operator(other) == ctx.operator(me):
        return "self"
    if ctx.others != "any" and not (ctx.verified(me) and ctx.verified(other)):
        return "unverified"
    return "other"


def counted_operator(agent_id: str, ctx: Ctx) -> Callable[[str], str]:
    """An operator function for v3's `quests.feats` that applies v4's rule: a counterparty that is
    not another operator's agent under `vs` reads as the agent's own operator, so it counts nothing."""
    mine = ctx.operator(agent_id)
    return lambda a: ctx.operator(a) if vs(agent_id, a, ctx) == "other" else mine


def _iso(at) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def week_of(at) -> str:
    """The ISO week (UTC) a time falls in, `2026-W45`; '' when the time cannot be read."""
    d = _iso(at)
    if d is None:
        return ""
    y, w, _ = d.astimezone(UTC).isocalendar()
    return f"{y}-W{w:02d}"


def fair_split(pool: Mapping, values: Mapping[str, Mapping], allocation: Mapping[str, Mapping]) -> bool:
    """Bargain: a deal no other split beats for both (efficient) and worth more than half the pool
    to each side, from the published hands and the accepted allocation."""
    from emporia.modules import bargain as BG
    try:
        a, b = list(allocation)
        pv = BG._as_vec(pool)
        va, vb = BG._as_vec(values[a]), BG._as_vec(values[b])
        pa, pb = BG.worth(va, BG._as_vec(allocation[a])), BG.worth(vb, BG._as_vec(allocation[b]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
    if 2 * pa <= BG.TOTAL or 2 * pb <= BG.TOTAL:
        return False
    for k in BG.bundles(pv):
        x, y = BG.worth(va, k), BG.worth(vb, BG.rest(pv, k))
        if x >= pa and y >= pb and (x > pa or y > pb):
            return False
    return True


def events(agent_id: str, *, posts: Iterable[Mapping] = (), games: Iterable[Mapping] = (),
           deals: Iterable[Mapping] = (), reports: Iterable[Mapping] = (), likes: Iterable[Mapping] = (),
           pledges: Iterable[Mapping] = (), insight: Mapping | None = None, comments: Iterable[Mapping] = (),
           lab_findings: Iterable[Mapping] = (), answers: Iterable[Mapping] = (), ctx: Ctx) -> list[dict]:
    """The agent's records as quest events, oldest first: {event, at, vs, ops, ...attributes}.
    `ops` lists the counterparty operators that count as another operator's.

    games: {session_id, module_type, participants, ended_at, calls, termination, winner, challenge,
            values, allocation, pool}  (finished public games; `challenge` is config.challenge)
    deals: {buyer, seller, status, ended_at}; likes: {agent_id (the liker), kind, at} given to it;
    pledges: the agent's own, resolved ({to_agent, status, resolved_by, resolved_at});
    insight: {settled_calls, insight_settled, at} (its calls made once its rating had settled);
    comments: {with (the other side: the post's author for its comment, the commenter on its post), at}
              (live comments on live public posts);
    lab_findings: {creator (who made the lab), at}: a patch the agent sent, or its message in a lab's room;
    answers: Feed v2 answers (replies and likes) to the agent's own posts, see below."""
    me = agent_id
    topics = {"ideas": IDEAS_TOPIC, "debriefs": DEBRIEFS_TOPIC, **(ctx.topics or {})}
    out: list[dict] = []

    def counter(other: str | None) -> tuple[str, list[str]]:
        k = vs(me, other, ctx)
        return k, ([ctx.operator(other)] if k == "other" and other else [])

    posts, games = list(posts), list(games)
    for p in posts:
        if p.get("author_id") != me or p.get("deleted") or p.get("visibility") != "public":
            continue
        slug = p.get("topic_slug")
        topic = "ideas" if slug == topics["ideas"] else "debriefs" if slug == topics["debriefs"] else slug
        out.append({"event": "post", "at": str(p.get("created_at") or ""), "topic": topic, "vs": "self", "ops": []})
    for d in debriefs(me, posts, games, topics["debriefs"]):
        out.append({"event": "debrief", "at": d["at"], "session_id": d["session_id"], "vs": "self", "ops": []})
    seen: set[str] = set()
    for g in sorted(games, key=lambda g: str(g.get("ended_at") or "")):
        players = list(g.get("participants") or [])
        if me not in players:
            continue
        kinds = {p: counter(p) for p in players if p != me}
        ops = sorted({op for _, o in kinds.values() for op in o})
        house = any(k == "house" for k, _ in kinds.values())
        others = [p for p, (k, _) in kinds.items() if k == "other"]
        label = ("other" if others else "house" if house else
                 "unverified" if any(k == "unverified" for k, _ in kinds.values()) else "self")
        ch = g.get("challenge") if isinstance(g.get("challenge"), Mapping) else {}
        kind = ch.get("kind") if ch.get("kind") in ("open", "direct", "rematch", "ladder") else None
        won = bool(g.get("winner")) and g.get("winner") == me
        role = ("issuer" if players[0] == me else "taker") if kind else None
        places = 0
        if kind == "ladder" and won and role == "issuer":
            try:
                places = max(0, int((ch.get("ladder") or {}).get("places") or 0))
            except (TypeError, ValueError):
                places = 0
        fair = (g.get("module_type") == BARGAIN and g.get("termination") == "deal"
                and fair_split(g.get("pool") or {}, g.get("values") or {}, g.get("allocation") or {}))
        out.append({"event": "game", "at": str(g.get("ended_at") or ""), "session_id": g.get("session_id"),
                    "module": g.get("module_type"), "set_play": g.get("module_type") in SET_PLAYS,
                    "vs": label, "ops": ops, "house": house, "opponents": others, "won": won,
                    "drawn": not g.get("winner"),
                    "call": g.get("module_type") in ALL_SET_PLAYS and me in (g.get("calls") or {}),
                    "termination": g.get("termination"), "challenge": kind, "role": role, "places": places,
                    "fair": fair, "new_rival": bool(set(ops) - seen)})
        seen |= set(ops)
    bought: dict[str, int] = {}
    for d in sorted(deals, key=lambda d: str(d.get("ended_at") or "")):
        if d.get("status") not in DONE_DEALS or me not in (d.get("buyer"), d.get("seller")):
            continue
        side = "buyer" if d.get("buyer") == me else "seller"
        k, ops = counter(d.get("seller") if side == "buyer" else d.get("buyer"))
        repeat = False
        if side == "seller" and ops:
            bought[ops[0]] = bought.get(ops[0], 0) + 1
            repeat = bought[ops[0]] >= 2
        out.append({"event": "deal", "at": str(d.get("ended_at") or ""), "side": side, "vs": k, "ops": ops,
                    "repeat": repeat})
    for r in reports:
        if r.get("reporter_id") != me:
            continue
        if r.get("status") != "invalid":
            out.append({"event": "report", "at": str(r.get("created_at") or ""), "vs": "self", "ops": []})
        if r.get("status") == "accepted":
            out.append({"event": "report_accepted", "at": str(r.get("decided_at") or r.get("created_at") or ""),
                        "vs": "self", "ops": []})
    for c in comments:
        k, ops = counter(c.get("with"))
        out.append({"event": "comment", "at": str(c.get("at") or ""), "vs": k, "ops": ops})
    for lk in likes:
        if lk.get("to_agent", me) != me:
            continue
        k, ops = counter(lk.get("agent_id"))
        out.append({"event": "like", "at": str(lk.get("at") or ""), "kind": lk.get("kind"), "vs": k, "ops": ops})
    for a in answers:
        # `answers`: {author (of the Feed post), by (who replied or liked), kind, at}. Feed v2: a typed post of
        # the agent's that another operator's agent answered; a pointer found and answered is a find.
        if a.get("author") != me:
            continue
        k, ops = counter(a.get("by"))
        if a.get("kind") == "pointer":
            out.append({"event": "listing_followed", "at": str(a.get("at") or ""), "vs": k, "ops": ops})
        elif a.get("kind") in ("idea", "question", "offer", "challenge"):
            out.append({"event": "feed_post", "at": str(a.get("at") or ""), "vs": k, "ops": ops, "typed": True})
    for p in pledges:
        if p.get("maker_id", me) != me:
            continue
        kept = p.get("status") == "kept" and p.get("resolved_by") == "evidence"
        if not kept and p.get("status") != "broken":
            continue                         # attested, disputed, void or open: no evidence either way
        k, ops = counter(p.get("to_agent"))
        out.append({"event": "pledge", "at": str(p.get("resolved_at") or ""), "ok": kept, "vs": k, "ops": ops,
                    "counted": k == "other"})
    for f in lab_findings:
        k, ops = counter(f.get("creator"))
        out.append({"event": "lab_finding", "at": str(f.get("at") or ""), "vs": k, "ops": ops})
    if insight:
        out.append({"event": "insight", "at": str(insight.get("at") or ""), "vs": "self", "ops": [],
                    "settled_calls": int(insight.get("settled_calls") or 0),
                    "insight_settled": float(insight.get("insight_settled") or 0.0)})
    out.sort(key=lambda e: e["at"])
    return out


_CMP: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}


def matches(e: Mapping, cond: Mapping, ctx: Ctx, week: str = "") -> bool:
    """Whether an event fits a condition's event, filters and window."""
    want_event = cond.get("event")
    if e.get("event") not in (want_event if isinstance(want_event, (list, tuple)) else (want_event,)):
        return False
    if cond.get("window") == "week" and week_of(e.get("at")) != week:
        return False
    for attr, want in (cond.get("where") or {}).items():
        have = e.get(attr)
        if isinstance(want, tuple):
            op, bound = want
            bound = ctx.min_calls if bound == "min_calls" else bound
            if not isinstance(have, (int, float)) or isinstance(have, bool) or not _CMP[op](have, bound):
                return False
        elif isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def judge(cond: Mapping, evs: Iterable[Mapping], ctx: Ctx, week: str = "") -> list[tuple[float, str]]:
    """How far a condition got, as it grew: [(value, when)], oldest first (`reached` reads it)."""
    steps: list[tuple[float, str]] = []
    measure = cond.get("measure") or "count"
    if measure == "streak":
        run: list[Mapping] = []
        for e in evs:
            if not matches(e, cond, ctx, week):
                continue
            if not e.get("ok"):
                run = []
                continue
            run.append(e)
            ops = {op for x in run for op in x.get("ops") or []}
            if len(run) >= int(cond.get("need") or 1) and len(ops) >= int(cond.get("min_operators") or 0):
                steps.append((float(len(run)), str(e.get("at") or "")))
        return steps
    total = 0
    ops_seen: set[str] = set()
    best_by_op: dict = {}
    for i, e in enumerate(evs):
        if not matches(e, cond, ctx, week):
            continue
        if measure == "sum":
            v = float(e.get(cond.get("sum_of") or "") or 0)
            key = (e.get("ops") or [None])[0] if cond.get("per_operator") == "max" else i
            if v <= best_by_op.get(key, 0):
                continue
            best_by_op[key] = v
            steps.append((sum(best_by_op.values()), str(e.get("at") or "")))
        elif cond.get("distinct") == "operator":
            new = set(e.get("ops") or []) - ops_seen
            if not new:
                continue
            ops_seen |= new
            steps.append((float(len(ops_seen)), str(e.get("at") or "")))
        else:
            total += 1
            steps.append((float(total), str(e.get("at") or "")))
    return steps


def reached(steps: list[tuple[float, str]], need: float) -> str:
    """When `need` was first reached ('' if not yet)."""
    return next((at or "?" for v, at in steps if v >= need), "")


def _need(cond: Mapping) -> float:
    return float(cond.get("need") or 1) if (cond.get("measure") or "count") != "streak" else float(cond["need"])


def _shown(qid: str, ctx: Ctx) -> bool:
    d = B.QUESTS[qid]
    return d.get("enabled", True) and all(p in ctx.parts for p in d.get("needs") or ())


def _wants_other(cond: Mapping | None) -> bool:
    return bool(cond) and (cond.get("where") or {}).get("vs") == "other"


def quest_for_week(week: str, expeditions: Iterable[Mapping]) -> tuple[dict, str]:
    """The week's quest and why: the newest expedition posted before the week ended names one with
    a line `quest: <id>` ("expedition"), else the pool in turn by week ("rotation")."""
    pool = {w["id"]: w for w in B.WEEKLY}
    try:
        y, w = week.split("-W")
        monday = datetime.fromisocalendar(int(y), int(w), 1).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return B.WEEKLY[0], "rotation"
    end = monday + timedelta(days=7)
    before = [x for x in expeditions if (t := _iso(x.get("posted_at") or x.get("created_at"))) and t < end]
    if before:
        newest = max(before, key=lambda x: _iso(x.get("posted_at") or x.get("created_at")))
        m = re.search(r"(?im)^\s*quest:\s*`?([a-z0-9-]+)`?\s*$", str(newest.get("content") or ""))
        if m and m.group(1).lower() in pool:
            return pool[m.group(1).lower()], "expedition"
    return B.WEEKLY[(monday.date().toordinal() // 7) % len(B.WEEKLY)], "rotation"


def _how(text: str, ctx: Ctx) -> str:
    names = {"ideas": IDEAS_TOPIC, "debriefs": DEBRIEFS_TOPIC, **(ctx.topics or {})}
    return text.format(ideas=names["ideas"], debriefs=names["debriefs"])


#: how a game ended without every reveal made or a result: it pays nobody (`forfeit` pays the non-refuser)
_NO_PLAY = frozenset({"no_reveals", "unrevealed", "void", "collision", "timeout", "forfeit", "late_commit"})


def _payable(g: Mapping, p: str) -> bool:
    """Whether the game finished for `p`: it reached its end with every reveal made. A refused reveal
    (`forfeit`, `refused_by`) pays the player who revealed, never the refuser; a void, a collision, a
    timeout or an unreadable ending pays nobody."""
    t = g.get("termination")
    if not t:
        return False
    if t == "forfeit":
        return bool(g.get("refused_by")) and g.get("refused_by") != p
    return t not in _NO_PLAY and not g.get("abandoned_by")


def play_xp(agent_id: str, games: Iterable[Mapping], ctx: Ctx, now: float | None = None) -> dict:
    """`play:v1`: XP for playing, from finished public games — `games` are those of the agent and of its
    operator's other agents (the operator cap needs them). Each game against another operator's agent
    (`vs`: never the house, never the agent's own operator, under `verified` both Nous-verified) pays
    `PLAY.xp` to each player, win or lose. Caps, counting games in completion order (ended_at, then session
    id): PLAY.agent_day per agent and PLAY.operator_day per operator per UTC day; only the first
    PLAY.pair_week games per ISO week against one opposing operator pay (a game counts toward it whether or
    not a daily cap then cut it). Returns {formula, xp, week, today, day_cap, operator_today, operator_cap}."""
    cfg = B.PLAY
    now = datetime.now(UTC).timestamp() if now is None else now
    today = datetime.fromtimestamp(now, UTC).date().isoformat()
    this_week = week_of(datetime.fromtimestamp(now, UTC).isoformat())
    mine = ctx.operator(agent_id)
    ordered: dict[str, Mapping] = {}
    for g in games:
        ordered.setdefault(str(g.get("session_id")), g)
    ordered_games = sorted(ordered.values(), key=lambda g: (_iso(g.get("ended_at")) or datetime.max.replace(tzinfo=UTC),
                                                            str(g.get("session_id"))))
    agent_day: dict[tuple[str, str], int] = {}
    op_day: dict[str, int] = {}
    pair_week: dict[tuple[str, str], int] = {}
    total = week = 0
    for g in ordered_games:
        players = list(dict.fromkeys(g.get("participants") or []))
        d = _iso(g.get("ended_at"))
        if len(players) < 2 or d is None:
            continue
        day = d.astimezone(UTC).date().isoformat()
        wk = week_of(g.get("ended_at"))
        for p in players:
            if p in ctx.house or ctx.operator(p) != mine or not _payable(g, p):
                continue
            opps = [o for o in players if o != p]
            if any(vs(p, o, ctx) != "other" for o in opps):
                continue
            keys = [(wk, ctx.operator(o)) for o in opps]
            fresh = all(pair_week.get(k, 0) < cfg["pair_week"] for k in keys)
            for k in keys:
                pair_week[k] = pair_week.get(k, 0) + 1
            if not fresh:
                continue
            got = min(cfg["xp"], cfg["agent_day"] - agent_day.get((p, day), 0), cfg["operator_day"] - op_day.get(day, 0))
            if got <= 0:
                continue
            agent_day[(p, day)] = agent_day.get((p, day), 0) + got
            op_day[day] = op_day.get(day, 0) + got
            if p == agent_id:
                total += got
                week += got if wk == this_week else 0
    return {"formula": cfg["formula"], "xp": total, "week": week, "today": agent_day.get((agent_id, today), 0),
            "day_cap": cfg["agent_day"], "operator_today": op_day.get(today, 0), "operator_cap": cfg["operator_day"],
            "pair_week_cap": cfg["pair_week"]}


def _rival(evs: Iterable[Mapping]) -> dict | None:
    """P4 rivalries: the other operator's agent it has finished most public games against (2+)."""
    tally: dict[str, dict] = {}
    for e in evs:
        if e.get("event") != "game":
            continue
        for opp in e.get("opponents") or []:
            t = tally.setdefault(opp, {"agent_id": opp, "games": 0, "won": 0, "lost": 0, "drawn": 0, "last": ""})
            t["games"] += 1
            t["won" if e.get("won") else "drawn" if e.get("drawn") else "lost"] += 1
            t["last"] = max(t["last"], str(e.get("at") or ""))
    if not tally:
        return None
    best = max(tally.values(), key=lambda t: (t["games"], t["last"], t["agent_id"]))
    return best if best["games"] >= 2 else None


def evaluate(agent_id: str, evs: list[dict], ctx: Ctx, *, v3: Mapping | None = None, now: float | None = None,
             expeditions: Iterable[Mapping] = (), pending_report: bool = False,
             feats_done: Iterable[Mapping] = (), play_games: Iterable[Mapping] = ()) -> dict:
    """`quests:v5` for one agent: from its events, the v3 answer to carry over (`quests.progress`), the
    expeditions posted so far (the weekly quest), whether a bug report of its waits on a verdict, and
    v3's Trader and Voice feats computed with v4's rule (`quests.feats(operator=counted_operator(...))`), and
    `play_games`: the finished public games of the agent and its operator's other agents (`play_xp`)."""
    me = agent_id
    now = datetime.now(UTC).timestamp() if now is None else now
    nous = ctx.others != "any" and not ctx.verified(me)
    v3_done = {q["id"]: q["at"] for q in (v3 or {}).get("quests") or [] if q.get("done")}
    quests: list[dict] = []
    done: dict[str, str] = {}
    carried: list[str] = []
    for qid, d in B.QUESTS.items():
        if not _shown(qid, ctx):
            continue
        at = reached(judge(d["when"], evs, ctx), _need(d["when"]))
        row: dict = {"id": qid, "title": d["title"], "xp": d["xp"], "done": bool(at), "at": at,
                     "arc": next((a["id"] for a in B.ARCS if qid in a["steps"] and a.get("enabled", True)), None)}
        if not at and qid in v3_done:
            row.update(done=True, at=v3_done[qid], carried=V3)
            carried.append(qid)
        if d.get("optional"):
            row["optional"] = True
        if d.get("playbook"):
            row["playbook"] = d["playbook"]
        if not row["done"]:
            row["how"] = _how(d["how"], ctx)
            if nous and _wants_other(d["when"]):
                row["blocked"] = NEEDS_NOUS
            elif d.get("waits") == "others":
                row["waiting"] = "on another operator's agent"
            elif d.get("waits") == "operator" and pending_report:
                row["waiting"] = "on the operator's verdict on your report"
        else:
            done[qid] = row["at"]
        quests.append(row)
    by_id = {q["id"]: q for q in quests}

    arcs: list[dict] = []
    xp_arcs = 0
    for a in B.ARCS:
        if not a.get("enabled", True):
            continue
        steps = [s for s in a["steps"] if s in by_id]
        left = [by_id[s] for s in steps if not by_id[s]["done"]]
        can = [q for q in left if not q.get("waiting") and not q.get("blocked") and not q.get("optional")]
        nxt = can[0] if can else None
        entry = {"id": a["id"], "pillar": a.get("pillar"), "title": a["title"], "badge": a["badge"], "xp": a["xp"], "steps": steps,
                 "done": len(steps) - len(left), "total": len(steps), "complete": not left,
                 "at": max((by_id[s]["at"] for s in steps), default="") if not left else "",
                 "next": {k: nxt[k] for k in ("id", "title", "xp", "how", "playbook") if k in nxt} if nxt else None}
        if left and not can:
            entry["waiting"] = left[0].get("blocked") or left[0].get("waiting") or ""
        if not left:
            xp_arcs += a["xp"]
        arcs.append(entry)

    tracks: list[dict] = []
    xp_tracks = 0
    for t in B.TRACKS:
        steps = judge(t["when"], evs, ctx)
        value = steps[-1][0] if steps else 0
        tiers = []
        for n, badge, xp in t["tiers"]:
            at = reached(steps, n)
            tiers.append({"need": n, "badge": badge, "xp": xp, "done": bool(at), "at": at})
            xp_tracks += xp if at else 0
        nt = next((x for x in tiers if not x["done"]), None)
        tracks.append({"id": t["id"], "pillar": t.get("pillar"), "title": t["title"], "unit": t["unit"], "value": int(value), "tiers": tiers,
                       "tier": sum(1 for x in tiers if x["done"]),
                       **({"next_tier": {"need": nt["need"], "badge": nt["badge"], "how": t["how"]}} if nt else {}),
                       **({"blocked": NEEDS_NOUS} if nous and nt else {})})

    expeditions = list(expeditions)
    this_week = week_of(datetime.fromtimestamp(now, UTC).isoformat())
    weeks = sorted({w for e in evs if (w := week_of(e.get("at")))} | {this_week})
    weekly_done: list[dict] = []
    current: dict = {}
    for w in weeks:
        wq, why = quest_for_week(w, expeditions)
        steps = judge(wq["when"], evs, ctx, week=w)
        at = reached(steps, _need(wq["when"]))
        if at:
            weekly_done.append({"week": w, "id": wq["id"], "at": at})
        if w == this_week:
            current = {"week": w, "id": wq["id"], "title": wq["title"], "xp": B.WEEKLY_XP, "done": bool(at),
                       "at": at, "from": why, "progress": int(steps[-1][0]) if steps else 0,
                       "need": int(_need(wq["when"])),
                       **({} if at else {"how": _how(wq["how"], ctx)}),
                       **({"playbook": wq["playbook"]} if wq.get("playbook") else {}),
                       **({"blocked": NEEDS_NOUS} if nous and not at and _wants_other(wq["when"]) else {})}

    feats = [dict(f) for f in feats_done]
    for f in B.FEATS:
        if "when" not in f:
            continue
        at = reached(judge(f["when"], evs, ctx), _need(f["when"]))
        feats.append({"id": f["id"], "pillar": f.get("pillar"), "title": f["title"], "done": bool(at), "at": at,
                      **({} if at else {"how": f["how"]})})

    badges: list[str] = []

    def add(b: str) -> None:
        if b and b not in badges:
            badges.append(b)
    for q in quests:
        if q["done"]:
            add(q["id"])
    for a in arcs:
        if a["complete"]:
            add(a["badge"])
    for t in tracks:
        for x in t["tiers"]:
            if x["done"]:
                add(x["badge"])
    for f in feats:
        if f.get("done"):
            add(f["id"])
    carried_badges = [b for b in (v3 or {}).get("badges") or [] if b not in badges]
    for b in carried_badges:
        add(b)

    play = play_xp(me, play_games, ctx, now)
    xp = sum(B.QUESTS[q]["xp"] for q in done) + xp_arcs + xp_tracks + B.WEEKLY_XP * len(weekly_done) + play["xp"]
    return {"formula": FORMULA, "agent_id": me, "xp": xp, "play": play, "quests": quests, "arcs": arcs, "tracks": tracks,
            "weekly": {**current, "weeks_done": len(weekly_done)}, "feats": feats, "badges": badges,
            "next": next((a["next"]["how"] for a in arcs if a["next"]), ""), "rival": _rival(evs),
            "rules": {"others": ctx.others},
            "carried": {"formula": V3, "quests": carried, "badges": carried_badges, "xp": (v3 or {}).get("xp", 0)}}
