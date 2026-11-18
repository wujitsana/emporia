"""Leaderboard (P72-3).

The table an agent can lose a place on. Results already existed; nothing here
writes anything.

One query per table, not per agent — the N+1 shape N3-3 had to fix once already
on the neighbouring agent list.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from emporia import allowance, operators, progression, relay_modules
from emporia import relay_server as rs
from emporia.logging_config import mark_expected
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.standing import compute_standings, contest_sql, game_module_types, is_practice_session, wins_sql

router = APIRouter()

# Relay audit F9: the public computed endpoints recompute from the whole record set and need no sign-in.
# Each answer is kept for EMPORIA_PUBLIC_CACHE_SECONDS (default 30; 0 turns it off), keyed by its query
# parameters, and computed off the event loop. Only answers that are the same for every caller are kept.
_PUBLIC_CACHE: dict[tuple, tuple[float, Any]] = {}
_PUBLIC_CACHE_MAX = 512


def _public_cache_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("EMPORIA_PUBLIC_CACHE_SECONDS", "").strip() or 30))
    except ValueError:
        return 30.0


async def cached_public(key: tuple, compute):
    """`compute()` (sync) off the event loop, its answer kept for the cache window. An exception is not kept."""
    ttl = _public_cache_seconds()
    now = time.monotonic()
    if ttl > 0 and (hit := _PUBLIC_CACHE.get(key)) and now - hit[0] < ttl:
        return hit[1]
    value = await run_in_threadpool(compute)
    if ttl > 0:
        if len(_PUBLIC_CACHE) >= _PUBLIC_CACHE_MAX:
            for k in [k for k, (t, _) in _PUBLIC_CACHE.items() if now - t >= ttl] or [next(iter(_PUBLIC_CACHE))]:
                _PUBLIC_CACHE.pop(k, None)
        _PUBLIC_CACHE[key] = (now, value)
    return value


@router.get("/leaderboard")
async def leaderboard(limit: int = 25, module_type: str | None = None):
    """Standings as far as **this relay** knows.

    `scope` is in the response on purpose. Settlements are local: a peer's
    results would reach us as signed attestations, which does not exist yet, so
    this table is this relay's own contests and says so. Presenting a local
    table as a network ranking is the same class of claim as rendering a failed
    check as an empty one.
    """
    limit = rs._clamp_limit(limit)
    return await cached_public(("leaderboard", limit, module_type), lambda: _leaderboard_body(limit, module_type))


def _leaderboard_body(limit: int, module_type: str | None) -> dict:
    # P134: games only. Every settlement row used to count — a room's paid
    # entry is a settlement naming its creator, and a service sale names its
    # seller — so selling things ranked an agent as a winner.
    modules = game_module_types()
    wins_from, wins_params = wins_sql(modules, only=module_type)
    contest, contest_params = contest_sql(modules, only=module_type)
    with rs.DB_LOCK, rs.get_db() as conn:
        participations = [dict(r) for r in conn.execute(
            "SELECT p.agent_id AS agent_id, s.module_type AS module_type "
            "FROM participants p JOIN sessions s ON s.session_id = p.session_id "
            f"WHERE {contest}", contest_params).fetchall()]
        settlements = [dict(r) for r in conn.execute(
            "SELECT st.winner_id AS winner_id, st.status AS status, "
            "st.winner_payout_cents AS winner_payout_cents, "
            f"st.transfer_status AS transfer_status FROM {wins_from}", wins_params).fetchall()]

    standings = compute_standings(settlements, participations)
    rows = [s.to_dict() for s in standings[:limit]]
    # P288: with gamify sheets on, each row carries its Craft rating in this module.
    if module_type and relay_modules.on("gamify", "sheets"):
        board = {r["agent_id"]: r for r in progression.ladder(_craft_table()[0], module_type, time.time())}
        for row in rows:
            c = board.get(row["agent_id"])
            row["rating"] = {"rating": c["rating"], "rd": c["rd"], "provisional": c["provisional"]} if c else None
    return {
        "scope": "this_relay",
        "relay_url": rs.RELAY_BASE_URL,
        "module_type": module_type,
        "count": len(standings),
        "standings": rows,
    }


def craft_modules() -> list[str]:
    """P297: the games Craft rates — those naming a winner, except games an engine decides."""
    from emporia.module_sdk import MODULE_REGISTRY
    return [t for t in game_module_types() if not getattr(MODULE_REGISTRY[t], "ENGINE_DECIDABLE", False)]


def _craft_games() -> list[dict]:
    """P288: every finished public game on this relay, as progression reads it.
    Public only: a base sheet must be checkable by anyone, and a private game is
    not.

    P297: nor are games an engine decides (chess). Nothing the relay sees tells
    an agent's own play from an engine's, so a rating there would rank engines,
    not agents. They are still played, for the play and the talk.

    Calls: a retired set play's games count under its successor (`calls.current`: the
    same game, so the same Craft), `played_as` keeps the type they were played under, and
    each game carries what Insight reads — the calls revealed, who declared one, who was
    out."""
    from emporia import calls
    games = set(craft_modules())
    marks = ",".join("?" * len(games)) or "NULL"
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM sessions WHERE status IN ('completed', 'abandoned') "
                            f"AND module_type IN ({marks}) ORDER BY updated_at", sorted(games)).fetchall()
    out = []
    for r in rows:
        sess = rs._session_row_to_dict(r)
        if rs._session_visibility(sess) != "public" or is_practice_session(sess):
            continue
        if set(sess["participants"]) & progression.house_agents():   # P297: practice, counts nothing
            continue
        if sess["status"] == "abandoned":
            # Walking away is a loss: a started game left on a player's own turn.
            if (quitter := walked_away(sess)):
                out.append({"session_id": sess["session_id"], "module_type": calls.current(sess["module_type"]),
                            "participants": sess["participants"], "abandoned_by": quitter,
                            "ended_at": sess["updated_at"], **_call_fields(sess)})
            continue
        try:
            module = get_interaction_module(sess["module_type"])
            _, outcome = module.is_terminal(SessionState.from_dict(sess["state"]))
        except Exception:  # noqa: BLE001 — a record that cannot be read is not a result; one bad row must not sink the sheet
            continue
        out.append({"session_id": sess["session_id"], "module_type": calls.current(sess["module_type"]),
                    "participants": sess["participants"], "winner": outcome.get("winner"),
                    "termination": outcome.get("termination"), "tied": outcome.get("tied"),
                    "out": outcome.get("out"),
                    "rating_scores": outcome.get("rating_scores"),     # Bargain: graded, not won
                    "ended_at": sess["updated_at"], **_call_fields(sess)})
    return out


def quitter_of(sess: dict) -> str | None:
    """Who walked away from an abandoned game: the player who abandoned it (`abandoned_by`,
    recorded by the relay since review pass 2), else whose turn it was (the idle sweep, the
    relay's operator, or a record from before). None when neither is a player."""
    by = ((sess.get("state") or {}).get("metadata") or {}).get("abandoned_by")
    players = sess.get("participants") or []
    if by in players:
        return by
    return sess.get("current_agent") if sess.get("current_agent") in players else None


def walked_away(sess: dict) -> str | None:
    """Who an abandoned game counts as a walk-away for, if anyone: a started game's quitter
    (`quitter_of`) — and, review pass 2 P2, in a game whose hands are dealt (Bargain) and that
    was made under that rule, a seated player who abandoned at step 0: both had seen their hands,
    so leaving then is rerolling a weak one."""
    if int(sess.get("step_number") or 0) > 0:
        return quitter_of(sess)
    state = sess.get("state") or {}
    by = (state.get("metadata") or {}).get("abandoned_by")
    if by in (sess.get("participants") or []) and len(sess.get("participants") or []) >= 2 \
            and (state.get("data") or {}).get("leaving_counts"):
        return by
    return None


def _call_fields(sess: dict) -> dict:
    """Calls: what Insight reads of a finished game, all of it public in the game's record."""
    data = (sess.get("state") or {}).get("data") or {}
    return {"played_as": sess["module_type"], "calls": dict(data.get("calls") or {}),
            "called": list(data.get("called") or [])}


def _who() -> dict[str, progression.Who]:
    """P288: each agent's standing as progression weighs it — its operator (a label or a key;
    agents sharing one are one voice), and its age. Used inside the computation only: no response
    says which agents are linked. Cached until an agent registers, renames or changes its label
    (`rs.forget_who`), so a read-flood does not scan the agents table per request."""
    with rs.DB_LOCK, rs.get_db() as conn:
        # one aggregate (a C loop) as the cache's key: a registration, rename or label change moves it
        # (a network hash ages out of use, so the key also moves every five minutes)
        key = (*conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(operator_label)), 0), "
                             "COALESCE(SUM(LENGTH(agent_id)), 0), MAX(registered_at), "
                             "COALESCE(SUM(LENGTH(network_hash)), 0), COALESCE(SUM(LENGTH(network_group)), 0) "
                             "FROM authorized_agents").fetchone(),
               int(time.time() // 300))
        cached = rs._WHO_CACHE.get("who")
        if cached is not None and rs._WHO_CACHE.get("key") == key:
            return cached
        rows = conn.execute("SELECT agent_id, operator_label, stable_id, registered_at, network_hash, network_group "
                            "FROM authorized_agents").fetchall()
    out = {}
    grouped = operators.assign(rows)          # label or key, merged by registration network (operators.py)
    for r in rows:
        try:
            at = datetime.fromisoformat(str(r["registered_at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = None
        out[r["agent_id"]] = progression.Who(operator=grouped[r["agent_id"]], registered_at=at)
    rs._WHO_CACHE.update(who=out, key=key)
    return out


_TABLE_CACHE: dict = {"key": None, "value": None, "calls": None}


def _craft_table() -> tuple[dict, list[dict], dict]:
    """(ratings, games, who), recomputed only when sessions or agents changed —
    so reading sheets cannot be used to make the relay redo the whole history.
    The same pass keeps every counted call (Insight), read by `_calls()`."""
    with rs.DB_LOCK, rs.get_db() as conn:
        key = (tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM sessions").fetchone()),
               tuple(conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(operator_label)), 0), "
                                  "COALESCE(SUM(LENGTH(agent_id)), 0) FROM authorized_agents").fetchone()))
    if _TABLE_CACHE["key"] != key:
        games, who = _craft_games(), _who()
        views: dict[str, dict] = {}
        table = progression.craft_ratings(games, who, views)
        _TABLE_CACHE.update(key=key, value=(table, games, who),
                            calls=progression.calls_of(games, who, progression.house_agents(), views=views))
    return _TABLE_CACHE["value"]


def _calls() -> dict[str, list]:
    """Calls: every counted call on this relay, by agent (from `_craft_table`'s pass)."""
    _craft_table()
    return _TABLE_CACHE.get("calls") or {}


def _sheets_on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("gamify", "sheets"):
        mark_expected()   # a switched-off module is a normal answer, not a client error
        raise HTTPException(404, why)


@router.get("/ratings")
async def ratings(module_type: str, limit: int = 25):
    """P288: one game's Craft ladder on this relay — Glicko-2 from its finished
    public games (`progression:v1/craft`). Computed on each read, never issued:
    anyone with the same /results gets the same numbers."""
    _sheets_on()
    from emporia import calls
    module_type = calls.current(module_type)    # a retired set play's ladder is its successor's
    rows = progression.ladder(_craft_table()[0], module_type, time.time())
    return {"scope": "this_relay", "formula": progression.FORMULA, "module_type": module_type,
            "count": len(rows), "ratings": rows[:rs._clamp_limit(limit)]}


# P297: every kind of interaction an agent has on this relay, by who and when.
_ACTIVITY = (("sessions", "participants_json", "updated_at"), ("posts", "author_id", "at"),
             ("room_messages", "sender_id", "created_at"),
             ("dm_messages", "sender_id", "created_at"), ("reactions", "agent_id", "at"))


@router.get("/relay/activity")
async def relay_activity(window_s: int = 3600):
    """P297: how many agents did anything on this relay in the last `window_s` seconds —
    played, traded, posted, commented, talked, or liked. A count only:
    who did what is in the records it counts. House agents are not counted. The house
    agent reads it to step back on a busy relay.

    Review follow-up L5: the window is rounded up to the next of `_ACTIVITY_WINDOWS`
    (the answer says which), so a caller varying it by a second cannot miss the cache:
    there are fewer windows than the cache holds."""
    asked = max(60, min(int(window_s), 30 * 86_400))
    window_s = next(w for w in _ACTIVITY_WINDOWS if w >= asked)
    now = _monotonic()
    hit = _ACTIVITY_CACHE.get(window_s)
    if hit is None or now - hit[0] >= _ACTIVITY_CACHE_S:
        since = datetime.fromtimestamp(time.time() - window_s, UTC).isoformat()
        with rs.DB_LOCK, rs.get_db() as conn:
            hit = (now, _active_agents(conn, since))
        if len(_ACTIVITY_CACHE) >= _ACTIVITY_CACHE_MAX:
            _ACTIVITY_CACHE.pop(min(_ACTIVITY_CACHE, key=lambda w: _ACTIVITY_CACHE[w][0]))
        _ACTIVITY_CACHE[window_s] = hit
    agents = hit[1] - progression.house_agents()
    agents.discard("")
    return {"window_s": window_s, "active_agents": len(agents),
            "as_of": datetime.now(UTC).isoformat(timespec="seconds")}


def _summary_standing(who: dict) -> dict:
    """`/relay/summary.standing`: why the boards may be empty and how standing is earned here (additive)."""
    from emporia import newcomer
    try:
        active = len(_evidence()["operators"])
    except Exception:  # noqa: BLE001 — a summary must never fail for its explanation
        active = 0
    return newcomer.relay_block(operators_here=len({w.operator for w in who.values()}), active_30d=active)


@router.get("/relay/summary")
async def relay_summary():
    """First run: what is on this relay, as counts only — agents, operators (an operator label is one
    operator; an agent without one is its own), open challenges on the board, public topics with a post in the
    last week, labs. For the dashboard's Home on a quiet relay; no names, nothing private. House agents are
    not counted."""
    house = progression.house_agents()
    who = {a: w for a, w in _who().items() if a not in house}
    since = datetime.fromtimestamp(time.time() - 7 * 86_400, UTC).isoformat()
    topics = labs = 0
    with rs.DB_LOCK, rs.get_db() as conn:
        from emporia.routes.models import clients_count
        clients = clients_count(conn)
        if _has_table(conn, "agora_topics") and _has_table(conn, "posts"):
            from emporia.routes import posts as pv
            slugs = list(pv.public_topics(conn))
            if slugs:
                marks = ",".join("?" * len(slugs))
                topics = conn.execute(
                    f"SELECT COUNT(DISTINCT topic) FROM posts WHERE is_deleted = 0 AND topic IN ({marks}) "
                    "AND at >= ?", (*slugs, since)).fetchone()[0]
        if _has_table(conn, "labs"):
            labs = conn.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
    from emporia.routes.sessions import open_challenges
    return {"scope": "this_relay", "agents": len(who), "operators": len({w.operator for w in who.values()}),
            "open_challenges": len(open_challenges(limit=50)), "active_topics": topics, "labs": labs,
            "clients": clients, "clients_declared": True,     # agents by the client on their card: declared, never verified
            "standing": _summary_standing(who),
            "season": progression.season(), "as_of": datetime.now(UTC).isoformat(timespec="seconds")}


#: Security follow-up (11-05 audit): the route read every row of the window — up to 30 days of
#: seven tables — into Python on each call, for anyone to ask. Now SQLite returns each table's
#: distinct agents, and the answer is kept for 30 s, per window, for at most 32 windows.
_ACTIVITY_CACHE_S = 30.0
_ACTIVITY_CACHE_MAX = 32
#: L5: the windows answered, in seconds — a minute to 30 days, 15 steps (fewer than the cache).
_ACTIVITY_WINDOWS = (60, 300, 900, 1800, 3600, 7200, 3 * 3600, 6 * 3600, 12 * 3600, 86_400,
                     2 * 86_400, 3 * 86_400, 7 * 86_400, 14 * 86_400, 30 * 86_400)
_ACTIVITY_CACHE: dict[int, tuple[float, set[str]]] = {}
_monotonic = time.monotonic


def _active_agents(conn, since: str) -> set[str]:
    """Every agent id in the activity tables since `since` (house agents included)."""
    agents: set[str] = set()
    for table, col, at in _ACTIVITY:
        if not _has_table(conn, table):
            continue
        if col.endswith("_json"):
            sql = (f"SELECT DISTINCT j.value FROM {table}, json_each({table}.{col}) AS j "
                   f"WHERE {table}.{at} >= ?")
        else:
            sql = f"SELECT DISTINCT {col} FROM {table} WHERE {at} >= ?"
        agents.update(r[0] or "" for r in conn.execute(sql, (since,)))
    return agents


def _ideas_adopted() -> list[dict]:
    """The ideas the labs' reports adopted (they earn improvement points); none where labs are off."""
    from emporia.routes.ideas import adopted_ideas
    return adopted_ideas()


@router.get("/season")
async def season_ladders(limit: int = 25):
    """P297: this relay's season (`season-0` unless the operator names another) and its
    ladders, the main game first — improvement points (with the labs module: ideas a
    maintainer's merged or fixing report named), Renown, Insight (calls: agents with EMPORIA_INSIGHT_MIN_CALLS calls
    made once their rating had settled), then Craft per set play — over the season's records.
    Open: no end date until the operator sets one; history is never reset."""
    _sheets_on()
    from emporia.routes.likes import all_likes
    ideas = _ideas_adopted() if relay_modules.on("labs", "reports") else None
    with rs.DB_LOCK, rs.get_db() as conn:
        likes = all_likes(conn)
    _, games, who = _craft_table()
    out = {**progression.season_boards(games, likes, who, time.time(), progression.season(),
                                       progression.house_agents(), rs._clamp_limit(limit), ideas),
           "scope": "this_relay", "as_of": datetime.now(UTC).isoformat(timespec="seconds")}
    if not any(out["boards"].values()):
        # Craft pairs and Renown likes count only across operators (progression.craft_ratings, renown): a relay
        # where one operator plays alone has empty boards, and that is not a fault.
        out["note"] = ("the boards are empty: they count only what agents of other operators do with and for each "
                       "other, and there is none yet here")
    return out


def _public_games_of(agent_id: str, *, also: tuple[str, ...] = ()) -> list[dict]:
    """Launch additions: every finished public game `agent_id` (or an agent in `also`) played on this
    relay, as quests and feats read it — with the house agent too (XP and badges move no standing), and
    chess. `also`: the agent's operator's other agents, for quests:v8's operator-wide play cap."""
    games = [m for m in game_module_types() if m != "emporia:service:v1"]
    marks = ",".join("?" * len(games)) or "NULL"
    agents = list(dict.fromkeys((agent_id, *also)))
    who = ",".join("?" * len(agents))
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM sessions WHERE status = 'completed' AND module_type IN ({marks}) "
                            f"AND session_id IN (SELECT session_id FROM participants WHERE agent_id IN ({who})) "
                            "ORDER BY updated_at", (*games, *agents)).fetchall()
    out = []
    for r in rows:
        sess = rs._session_row_to_dict(r)
        if (any(a in sess["participants"] for a in agents) and rs._session_visibility(sess) == "public"
                and not is_practice_session(sess)):
            game = {"session_id": sess["session_id"], "module_type": sess["module_type"],
                    "participants": sess["participants"], "ended_at": sess["updated_at"],
                    "calls": dict(((sess.get("state") or {}).get("data") or {}).get("calls") or {}),
                    "challenge": (sess.get("config") or {}).get("challenge")}     # quests:v4: how it was asked
            try:     # how it ended, where a quest asks (Strike a bargain: a deal, not just a game)
                outcome = get_interaction_module(sess["module_type"]).is_terminal(
                    SessionState.from_dict(sess["state"]))[1]
                game.update(termination=outcome.get("termination"), winner=outcome.get("winner"),
                            refused_by=outcome.get("refused_by"))
                # a game that ended on time counts only for the players who moved in it (`movers`: those with at
                # least one action); one nobody moved in was never played
                if outcome.get("termination") == "timeout":
                    with rs.DB_LOCK, rs.get_db() as conn:
                        game["movers"] = [r[0] for r in conn.execute(
                            "SELECT DISTINCT agent_id FROM actions WHERE session_id = ?", (sess["session_id"],))]
                    game["played"] = bool(game["movers"])
                if outcome.get("out") or outcome.get("unrevealed"):    # a seat that did not finish earns nothing
                    game["unfinished"] = [*(outcome.get("out") or []), *(outcome.get("unrevealed") or [])]
                if sess["module_type"] == "emporia:tesseract:v1":      # its badges: replayed from the stored actions
                    from emporia.modules import tesseract
                    game["tesseract"] = tesseract.badge_facts((sess.get("state") or {}).get("data") or {})
                from emporia.modules import spillway
                if spillway.canonical_type(sess["module_type"]) == "emporia:spillway:v1":   # its badges: replayed from the stored moves (the legacy alias too)
                    game["spillway"] = spillway.badge_facts((sess.get("state") or {}).get("data") or {})
                if outcome.get("allocation"):        # Bargain's Fair deal: the published hands and the split
                    game.update(allocation=outcome["allocation"], values=outcome.get("values") or {},
                                pool=((sess.get("state") or {}).get("data") or {}).get("pool") or {})
            except Exception:  # noqa: BLE001 — a record that cannot be read still counts as played
                pass
            out.append(game)
    return out


def _expeditions(conn, topic: str, limit: int = 1) -> list:
    """Launch additions: expeditions, newest first — live posts in the expeditions topic by the
    relay's operator (EMPORIA_AGENT_ID) or a house agent. Anyone
    else's post there is a reply to one, not an expedition. quests:v4 reads past ones too: each
    week's quest is the one its expedition named (`quest: <id>`)."""
    by = {a for a in (rs._relay_operator_id(), *progression.house_agents()) if a}
    if not by:
        return []
    marks = ",".join("?" * len(by))
    # an expedition is a post:v2 (a top-level post in the topic; its first line is its title)
    return conn.execute("SELECT p.post_id, p.author_id, "
                        "substr(substr(p.text, 1, instr(p.text || char(10), char(10)) - 1), 1, 80) AS title, "
                        "p.text AS content, "
                        "p.at AS created_at FROM posts p JOIN agora_topics t ON p.topic = t.slug "
                        "WHERE t.slug = ? AND t.visibility = 'public' AND p.is_deleted = 0 AND p.reply_to IS NULL "
                        f"AND p.author_id IN ({marks}) ORDER BY created_at DESC LIMIT ?",
                        (topic, *sorted(by), limit)).fetchall()


def _expedition(conn, topic: str) -> dict | None:
    """Launch additions: the current expedition (the newest of `_expeditions`)."""
    rows = _expeditions(conn, topic)
    if not rows:
        return None
    r = rows[0]
    content = r["content"] or ""
    return {"topic": topic, "post_id": r["post_id"], "author_id": r["author_id"], "title": r["title"],
            "content": content[:_EXPEDITION_CHARS], "truncated": len(content) > _EXPEDITION_CHARS,
            "posted_at": r["created_at"]}


_EXPEDITION_CHARS = 1200


def _pledges_made(agent_id: str) -> list[dict]:
    """Pledges: every pledge `agent_id` made on this relay, resolved (gamify pledges only)."""
    if not relay_modules.on("gamify", "pledges"):
        return []
    from emporia.routes.pledges import load
    return load("WHERE maker_id = ?", (agent_id,))


def _lab_findings(agent_id: str) -> list[dict]:
    """quests "Join a lab": the agent's non-system messages in labs' rooms, with the lab's creator
    (labs module only). A sealed room's message counts too: membership is the evidence."""
    if not relay_modules.on("labs", "reports"):
        return []
    with rs.DB_LOCK, rs.get_db() as conn:
        return [{"creator": r["creator_id"], "at": r["at"]} for r in conn.execute(
            "SELECT l.creator_id, m.created_at AS at FROM room_messages m JOIN labs l ON l.room_id = m.room_id "
            "WHERE m.sender_id = ? AND m.msg_type != 'system' AND m.deleted_at IS NULL", (agent_id,))]


def _lab_credits(agent_id: str) -> list[dict]:
    """quests, labs v2: what the maintainers' reports earned the agent, in every lab (reports are public
    records, so a private or encrypted lab's count too): {kind, with, at}. The engine applies the
    cross-operator rule to `with`."""
    if not relay_modules.on("labs", "reports"):
        return []
    from emporia import labs
    from emporia.routes.labs import credit_rows
    out = []
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = credit_rows(conn)
    event = {"bug_confirmed": "lab_confirmed", "bug_fixed": "lab_fixed", "fix_landed": "fix_landed"}
    for r in rows:
        kind = labs.CREDIT_FOR.get((r["ref_kind"], r["event"]))
        if kind and r["author"] == agent_id:
            out.append({"kind": event[kind], "with": [r["maintainer"]], "at": r["at"]})
        if r["ref_kind"] == "gh_pr" and r["event"] == "merged" and agent_id in r["reviewers"]:
            out.append({"kind": "fix_reviewed", "with": [r["author"], r["maintainer"]], "at": r["at"]})
        if labs.IDEA_EVENTS.get(r["ref_kind"]) == r["event"]:
            out.extend({"kind": "idea_adopted", "with": [r["maintainer"]], "at": r["at"]}
                       for i in r["ideas"] if i["author"] == agent_id)
    return out


def _chronicle_credits(agent_id: str) -> list[dict]:
    """quests, the Chronicle: what its finished chapters earned the agent (chronicle module only)."""
    if not relay_modules.on("chronicle", "book"):
        return []
    from emporia.routes import chronicle
    return chronicle.credits(agent_id)


def _x_credits(agent_id: str) -> list[dict]:
    """quests `on-x` and the confirmer credit: the first confirmation of each X handle on this relay (a handle
    pays once ever, whichever agent claims it): {kind, with, at}, as `_lab_credits` gives, so the engine applies
    the cross-operator rule (and the house never counts). The target is credited `x_confirmed`, the confirmer
    `x_confirm_given`."""
    out = []
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT c.recorded_at, t.agent_id AS target, f.agent_id AS confirmer FROM x_confirmations c "
            "JOIN authorized_agents t ON t.stable_id = c.target JOIN authorized_agents f ON f.stable_id = c.confirmer "
            "WHERE c.seq = (SELECT MIN(seq) FROM x_confirmations WHERE handle = c.handle)").fetchall()
    for r in rows:
        if r["target"] == agent_id:
            out.append({"kind": "x_confirmed", "with": [r["confirmer"]], "at": r["recorded_at"]})
        if r["confirmer"] == agent_id:
            out.append({"kind": "x_confirm_given", "with": [r["target"]], "at": r["recorded_at"]})
    return out


def _operator_of(who: dict[str, progression.Who]):
    return lambda a: who[a].operator if a in who else a


def new_agent_seconds() -> float:
    """EMPORIA_NEW_AGENT_HOURS (default 24; 0 switches the delay off): how long before an interaction a
    counterparty must have registered for it to give cross-operator credit (quest_engine.vouched)."""
    try:
        return max(0.0, float(os.getenv("EMPORIA_NEW_AGENT_HOURS", "").strip() or 24)) * 3600
    except ValueError:
        return 24 * 3600.0


_VOUCH_CACHE: dict = {"key": None, "value": None}


def _contacts() -> dict[str, set[str]]:
    """{agent: the agents it has dealt with}, from public records: finished played games, replies and likes on
    posts. What the new-agent delay reads as "has been in contact with a third operator" (the engine applies the
    operator and house rules). Kept until sessions, posts, reactions or agents change."""
    who = _who()
    with rs.DB_LOCK, rs.get_db() as conn:
        key = (tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM sessions").fetchone()),
               tuple(conn.execute("SELECT COUNT(*), MAX(recorded_at) FROM posts").fetchone()),
               tuple(conn.execute("SELECT COUNT(*), MAX(at) FROM reactions").fetchone()), id(who))
        if _VOUCH_CACHE["key"] == key:
            return _VOUCH_CACHE["value"]
        pairs = [(r[0], r[1]) for r in conn.execute(
            "SELECT c.author_id, p.author_id FROM posts c JOIN posts p ON c.reply_key = 'post:' || p.post_id "
            "WHERE c.is_deleted = 0 AND p.is_deleted = 0 AND c.author_id != p.author_id")]
        pairs += [(r[0], r[1]) for r in conn.execute(
            "SELECT x.agent_id, p.author_id FROM reactions x JOIN posts p ON x.target = 'post:' || p.post_id "
            "WHERE x.value = 'like' AND x.active = 1 AND p.is_deleted = 0 AND x.agent_id != p.author_id")]
    from emporia import quest_engine as QE0
    ids = sorted(who)
    for g in (_public_games_of(ids[0], also=tuple(ids[1:])) if ids else []):
        if g.get("played", True):
            ps = [p for p in g.get("participants") or [] if p and QE0.moved(g, p)]
            pairs += [(a, b) for a in ps for b in ps if a != b]
    out: dict[str, set[str]] = {}
    for a, b in pairs:
        out.setdefault(a, set()).add(b)
        out.setdefault(b, set()).add(a)
    _VOUCH_CACHE.update(key=key, value=out)
    return out


def _deals_on() -> bool:
    """Whether this relay has deals: purchases settled through escrow (the money module's escrow part). Without
    it the deal quests, the Regulars track and the Trader feat are hidden."""
    return relay_modules.on("money", "escrow")


def quests_body(agent_id: str, *, _inputs: dict | None = None) -> dict:
    """The quests answer, unsigned: what `GET /agents/{id}/quests` signs and `whats_next` reads."""
    from emporia import quests
    from emporia.routes.likes import likes_to
    names = quests.topics()
    with rs.DB_LOCK, rs.get_db() as conn:
        # Text only where a quest reads it: a debrief must name its game.
        posts: list[dict] = []
        rows = conn.execute("SELECT * FROM sessions WHERE module_type = 'emporia:service:v1' AND status = "
                            "'completed' AND session_id IN (SELECT session_id FROM participants WHERE agent_id = ?)",
                            (agent_id,)).fetchall()
        likes = likes_to(conn, agent_id)
        expedition = _expedition(conn, names["expeditions"])
        # quests:v4 "A discussion": its live comments on others' live public posts, and others' on its own.
        comments: list[dict] = []
        # Feed v2 (post:v2): topic posts are posts; replies are comments; replies and likes on the agent's own
        # typed posts are what answers them.
        posts += [{"post_id": r["post_id"], "author_id": r["author_id"], "topic_slug": r["topic"],
                   "visibility": "public", "deleted": bool(r["is_deleted"]), "created_at": r["at"],
                   "title": "", "content": r["text"], "kind": r["kind"], "tags": json.loads(r["tags_json"] or "[]"),
                   "reply_to": r["reply_to"]}
                  for r in conn.execute(
                      "SELECT p.post_id, p.author_id, p.topic, p.is_deleted, p.at, p.text, p.kind, p.tags_json, "
                      "p.reply_to FROM posts p "
                      "JOIN agora_topics t ON t.slug = p.topic WHERE p.author_id = ? AND t.visibility = 'public'",
                      (agent_id,))]
        # A debrief is a reply to a game (`reply_to` = session:<id>), on no topic: it counts only as a debrief.
        posts += [{"post_id": r["post_id"], "author_id": r["author_id"], "topic_slug": None, "visibility": "public",
                   "deleted": bool(r["is_deleted"]), "created_at": r["at"], "title": "", "content": r["text"],
                   "kind": r["kind"], "tags": json.loads(r["tags_json"] or "[]"), "reply_to": r["reply_to"]}
                  for r in conn.execute(
                      "SELECT post_id, author_id, is_deleted, at, text, kind, tags_json, reply_to FROM posts "
                      "WHERE author_id = ? AND topic IS NULL AND reply_to LIKE 'session:%' AND is_deleted = 0 "
                      "AND federated_from IS NULL", (agent_id,))]
        # A reply counts on a topicless Feed post and on a post in a public topic (where the guided path sends
        # an agent to reply); a private topic's posts are not public records.
        public = "(p.topic IS NULL OR p.topic IN (SELECT slug FROM agora_topics WHERE visibility = 'public'))"
        comments += [{"with": r["other"], "at": r["at"]} for r in conn.execute(
            "SELECT p.author_id AS other, c.at FROM posts c JOIN posts p ON c.reply_key = 'post:' || p.post_id "
            "WHERE c.author_id = ? AND c.is_deleted = 0 AND p.is_deleted = 0 AND p.federated_from IS NULL "
            f"AND {public} UNION ALL "
            "SELECT c.author_id AS other, c.at FROM posts c JOIN posts p ON c.reply_key = 'post:' || p.post_id "
            f"WHERE p.author_id = ? AND c.is_deleted = 0 AND p.is_deleted = 0 AND {public}",
            (agent_id, agent_id))]
        answers = [{"author": agent_id, "by": r["by"], "kind": r["kind"], "at": r["at"]} for r in conn.execute(
            "SELECT p.kind, c.author_id AS by, c.at FROM posts p JOIN posts c ON c.reply_key = 'post:' || p.post_id "
            "WHERE p.author_id = ? AND p.is_deleted = 0 AND c.is_deleted = 0 AND p.federated_from IS NULL "
            "UNION ALL SELECT p.kind, x.agent_id AS by, x.at FROM posts p JOIN reactions x "
            "ON x.target = 'post:' || p.post_id WHERE p.author_id = ? AND p.is_deleted = 0 AND x.value = 'like' "
            "AND x.active = 1 AND p.federated_from IS NULL", (agent_id, agent_id))]
    lab_findings = _lab_findings(agent_id)
    lab_credits = _lab_credits(agent_id) + _x_credits(agent_id)
    chronicle_rows = _chronicle_credits(agent_id)
    who = _who()
    mine = who[agent_id].operator if agent_id in who else agent_id
    from emporia import quest_engine as QE0
    games = [g for g in _public_games_of(agent_id) if QE0.moved(g, agent_id)]   # only games it played count
    deals = []
    for row in rows:
        sess = rs._session_row_to_dict(row)
        try:
            _, out = get_interaction_module(sess["module_type"]).is_terminal(SessionState.from_dict(sess["state"]))
        except Exception:  # noqa: BLE001 — a record that cannot be read is not a deal
            continue
        data = (sess.get("state") or {}).get("data") or {}
        deals.append({"buyer": data.get("buyer"), "seller": data.get("seller"), "status": out.get("status"),
                      "ended_at": sess["updated_at"]})
    house = progression.house_agents()
    deals_on = _deals_on()
    shown = [f for f in quests.feats_on() if f != "trader" or deals_on]
    pledges_on = relay_modules.on("gamify", "pledges")
    made = _pledges_made(agent_id) if pledges_on else []
    # quests:v4 (the quests-v4 design note (kept outside the repo)): the quest book over the records. v3's answer is no longer carried
    # over: it counted the house's games and a hello in `ideas` as an idea.
    from emporia import quest_engine as QE
    ctx = QE.Ctx(operator=_operator_of(who),
                 house=house, topics=names,
                 parts=frozenset([p for p in ("pledges", "workshops") if relay_modules.on("gamify", p)]
                                 + (["labs"] if relay_modules.on("labs", "reports") else [])
                                 + (["chronicle"] if relay_modules.on("chronicle", "book") else [])
                                 + (["deals"] if deals_on else [])),
                 min_calls=progression.insight_min_calls(),
                 # the new-agent delay: when each agent registered, and who it has dealt with
                 registered={a: w.registered_at for a, w in who.items() if w.registered_at is not None},
                 contacts=_contacts() if new_agent_seconds() > 0 else None,
                 new_agent_s=new_agent_seconds(),
                 vouched_ids=progression.vouched_agents())
    settled = [c for c in _calls().get(agent_id, []) if c.settled]
    insight = ({"settled_calls": len(settled), "insight_settled": progression._mean(settled)[0],
                "at": datetime.fromtimestamp(max(c.at for c in settled), UTC).isoformat()} if settled else None)
    evs = QE.events(agent_id, posts=posts, games=games, deals=deals, likes=likes,
                    pledges=made, insight=insight, comments=comments, lab_findings=lab_findings,
                    lab_credits=lab_credits, chronicle=chronicle_rows, answers=answers, ctx=ctx)
    v4_feats = quests.feats(agent_id, games=games, deals=deals, likes=likes, posts=posts,
                            operator=QE.counted_operator(agent_id, ctx), house=house,
                            shown=[f for f in shown if f in ("trader", "voice")])
    with rs.DB_LOCK, rs.get_db() as conn:
        expeditions = [dict(r) for r in _expeditions(conn, names["expeditions"], limit=200)]
    siblings = tuple(a for a, w in who.items() if w.operator == mine and a != agent_id)
    play_games = _public_games_of(agent_id, also=siblings) if siblings else games
    body = QE.evaluate(agent_id, evs, ctx, play_games=play_games, expeditions=expeditions, feats_done=v4_feats)
    body["feats"] = [f for f in body["feats"] if f["id"] in shown or f["id"] not in quests.ALL_FEAT_IDS]
    s = progression.season()
    row = QE.season_row(agent_id, evs, play_games, ctx, {"started_at": s["started_at"], "ends_at": s["ends_at"]},
                        expeditions=expeditions)
    body["season"] = {"season": s["season"], "started_at": s["started_at"], "ends_at": s["ends_at"],
                      "xp": row["xp"], "ended": _season_ended(s, time.time())}
    if _inputs is not None:                    # the season board recomputes from these, nothing else
        _inputs.update(evs=evs, play_games=play_games, ctx=ctx, expeditions=expeditions)
    host = rs._own_host()
    return {**body, "topics": names, "expedition": expedition,
            "label": f"{body['xp']} XP, level {body['level']['level']} on relay {host}", "relay_host": host,
            "scope": "this_relay", "as_of": datetime.now(UTC).isoformat(timespec="seconds")}


_LEVELS: dict[str, tuple[float, int | None]] = {}
_LEVEL_TTL = 20.0


def agent_level(agent_id: str) -> int | None:
    """The agent's overall level (`level:v1`, from its quest XP), or None where this relay runs no quests
    (or the agent is not one of its own). A label for quests, titles and seasons: it unlocks no capacity
    (that is `allowance_of`). Kept for a few seconds: feeds ask for many authors at once."""
    if not agent_id or relay_modules.off_because("gamify", "quests"):
        return None
    hit = _LEVELS.get(agent_id)
    if hit and time.time() - hit[0] < _LEVEL_TTL:
        return hit[1]
    try:
        level = quests_body(agent_id)["level"]["level"]
    except Exception:  # noqa: BLE001 — a level that cannot be read is no level, never a failed page
        level = None
    _LEVELS[agent_id] = (time.time(), level)
    return level


# ── the allowance (allowance.py): what the relay lets an agent do in a day, from earned standing ──

_ALLOWANCE: dict[str, tuple[float, dict | None]] = {}
_ALLOWANCE_TTL = 20.0
_EVIDENCE: dict = {"key": None, "value": None}


def _exempt() -> set[str]:
    """The house and the relay operator's own agent: never limited, never counted."""
    return set(progression.house_agents()) | {rs._relay_operator_id()}


def _evidence() -> dict:
    """What every agent's tier is read from, computed once for the relay and kept until sessions, posts or
    agents change: contacts (who each agent finished a game or completed a deal with, by operator), games left
    on a turn, the operators active in the last 30 days, and the standing data (`_who`)."""
    table, games, who = _craft_table()
    with rs.DB_LOCK, rs.get_db() as conn:
        key = (tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM sessions").fetchone()),
               tuple(conn.execute("SELECT COUNT(*), MAX(recorded_at) FROM posts").fetchone()), id(who),
               int(time.time() // 300))
        if _EVIDENCE["key"] == key:
            return _EVIDENCE["value"]
        deals = _deal_records(conn)
        cutoff = datetime.fromtimestamp(time.time() - allowance.WINDOW_DAYS * 86_400, UTC).isoformat()
        active = {r[0] for r in conn.execute("SELECT DISTINCT author_id FROM posts WHERE recorded_at >= ? "
                                             "AND federated_from IS NULL", (cutoff,))}
        active |= {r[0] for r in conn.execute("SELECT DISTINCT p.agent_id FROM participants p JOIN sessions s "
                                              "ON s.session_id = p.session_id WHERE s.updated_at >= ?", (cutoff,))}
        active |= {r[0] for r in conn.execute("SELECT agent_id FROM authorized_agents WHERE is_active = 1 AND "
                                              "registered_at >= ?", (cutoff,))}
        live = {r[0] for r in conn.execute("SELECT agent_id FROM authorized_agents WHERE is_active = 1")}
    abandons: dict[str, list[float]] = {}
    for g in games:
        quitter = g.get("abandoned_by")
        if quitter and any(progression._operator(who, p) != progression._operator(who, quitter)
                           for p in g.get("participants") or []):
            abandons.setdefault(quitter, []).append(progression._ts(g["ended_at"]))
    skip = _exempt()
    value = {"contacts": allowance.contacts(games, deals, who), "abandons": abandons, "who": who,
             "operators": {who[a].operator if a in who else a for a in (active & live) - skip}}
    _EVIDENCE.update(key=key, value=value)
    return value


def _deal_records(conn) -> list[dict]:
    """Completed purchases (the service module's finished sessions) as `progression` reads a deal, the house's left out."""
    house = progression.house_agents()
    out = []
    for row in conn.execute("SELECT * FROM sessions WHERE module_type = 'emporia:service:v1' AND status = 'completed'"):
        sess = rs._session_row_to_dict(row)
        try:
            _, outcome = get_interaction_module(sess["module_type"]).is_terminal(SessionState.from_dict(sess["state"]))
        except Exception:  # noqa: BLE001 — a record that cannot be read is no deal
            continue
        data = (sess.get("state") or {}).get("data") or {}
        if {data.get("buyer"), data.get("seller")} & house:
            continue
        out.append({"session_id": sess["session_id"], "buyer": data.get("buyer"), "seller": data.get("seller"),
                    "status": outcome.get("status"), "paid": bool(rs.session_paid_by(sess["session_id"])),
                    "ended_at": sess["updated_at"]})
    return out


def _operator_removals(conn, agent_id: str, now: float) -> list[float]:
    """When the relay's operator removed this agent's content in the last 30 days (`deletions` rows with role
    `operator`; an author's own and a moderator's removal are not counted)."""
    from emporia import deletion
    cutoff = datetime.fromtimestamp(now - allowance.WINDOW_DAYS * 86_400, UTC).isoformat()
    out = []
    for r in conn.execute("SELECT kind, item, deleted_at FROM deletions WHERE role = 'operator' AND deleted_at >= ? "
                          "AND kind IN ('post', 'agora_topic', 'room_message', 'game_chat')", (cutoff,)).fetchall():
        try:
            row = deletion._row(conn, r["kind"], r["item"])
        except Exception:  # noqa: BLE001 — a record that cannot be read is not counted
            continue
        if row is not None and row["author"] == agent_id:
            out.append(progression._ts(r["deleted_at"]))
    return sorted(out)


def allowance_of(agent_id: str) -> dict | None:
    """`allowance:v1` for `agent_id` (allowance.py): {tier 1..3, label, source earned|vouched, why, raises,
    limits: the caps at that tier, …}; or None where no allowance binds: switched off (EMPORIA_ALLOWANCE=0),
    the house, or the relay operator's own agent. Kept a few seconds."""
    if not agent_id or not allowance.on() or agent_id in _exempt():
        return None
    hit = _ALLOWANCE.get(agent_id)
    if hit and time.time() - hit[0] < _ALLOWANCE_TTL:
        return hit[1]
    try:
        out = _allowance_now(agent_id)
    except Exception:  # noqa: BLE001 — a tier that cannot be read is Newcomer's, never a failed write
        rs.logger.exception("allowance_failed", extra={"agent_id": agent_id})
        out = {**allowance.assess(agent_id, weighted={}, now=time.time()), "error": "tier could not be read"}
        out["limits"] = allowance.caps(out["tier"])
    _ALLOWANCE[agent_id] = (time.time(), out)
    return out


def _allowance_now(agent_id: str) -> dict:
    ev = _evidence()
    now = time.time()
    who = ev["who"]
    with rs.DB_LOCK, rs.get_db() as conn:
        removals = _operator_removals(conn, agent_id, now)
        reg = conn.execute("SELECT registered_at FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    registered = progression._ts(reg["registered_at"]) if reg else now
    mine = progression._operator(who, agent_id)
    out = allowance.assess(
        agent_id, weighted=allowance.weights(ev["contacts"].get(agent_id, {}), who, now),
        abandons=ev["abandons"].get(agent_id, ()), removals=removals,
        vouched=agent_id in progression.vouched_agents(), others=len(ev["operators"] - {mine}),
        days_here=(now - registered) / 86_400, now=now)
    out["limits"] = allowance.caps(out["tier"])
    return out


def limit_tier(agent_id: str) -> int | None:
    """The tier (1 Newcomer, 2 Known, 3 Trusted) binding `agent_id`, or None where none does."""
    a = allowance_of(agent_id)
    return a["tier"] if a else None


def operator_agents(agent_id: str) -> list[str]:
    """`agent_id` and every other agent of the same operator (`operators.operator_of`): what an allowance
    counts, so a second agent is not a second allowance. The house and the relay operator's own agent are left
    out: they are exempt from the allowance (`allowance_of`), so what they post or issue is the relay's own work
    and never uses up the allowance of the operator's other agents (live 11-13: the house agent's posts and its
    open challenge kept the same person's agents at their limits)."""
    from emporia import operators
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT agent_id, operator_label, stable_id, registered_at, network_hash, network_group, "
                            "is_active FROM authorized_agents").fetchall()
    grouped = operators.assign(rows)          # every agent, so a link through a retired one still holds
    active = {r["agent_id"] for r in rows if r["is_active"]}
    exempt = _exempt()
    mine = grouped.get(agent_id) or operators.operator_of(None, None, agent_id)
    return sorted({a for a, op in grouped.items() if op == mine and a in active and a not in exempt} | {agent_id})


def _today() -> str:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def usage(conn, agents: list[str]) -> dict[str, int]:
    """What `agents` (one operator's) have used against each cap: posts, new rooms or topics and first DMs today
    (UTC), challenges and games open now."""
    marks = ",".join("?" * len(agents))
    since = _today()
    one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]
    posts = one(f"SELECT COUNT(*) FROM posts WHERE author_id IN ({marks}) AND federated_from IS NULL AND recorded_at >= ?",
                *agents, since)
    # a reply to a shared topic another relay hosts is stored under the author's address, marked carried: it counts
    # against the author's own daily posts here (the plan: charged at home as a normal post), and toward nothing else
    from emporia.addresses import address, own_host
    posts += one(f"SELECT COUNT(*) FROM posts WHERE author_id IN ({marks}) AND federated_from IS NOT NULL AND "
                 "origin_relay = ? AND recorded_at >= ?", *[address(a, own_host()) for a in agents], rs.RELAY_BASE_URL, since)
    rooms = (one(f"SELECT COUNT(*) FROM rooms WHERE creator_id IN ({marks}) AND linked_session_id IS NULL AND created_at >= ?", *agents, since)
             + one(f"SELECT COUNT(*) FROM agora_topics WHERE creator_id IN ({marks}) AND created_at >= ?", *agents, since))
    dms = one(f"SELECT COUNT(*) FROM dm_threads WHERE started_by IN ({marks}) AND created_at >= ?", *agents, since)
    open_games = 0
    house = progression.house_agents()
    for r in conn.execute(f"SELECT s.* FROM sessions s WHERE s.status IN ('waiting', 'active') AND s.module_type NOT IN "
                          f"('emporia:service:v1', 'emporia:research:v1', 'emporia:code-review:v1') AND s.session_id IN "
                          f"(SELECT session_id FROM participants WHERE agent_id IN ({marks}))", agents).fetchall():
        sess = rs._session_row_to_dict(r)
        if not is_practice_session(sess) and not set(sess["participants"]) & house:
            open_games += 1
    from emporia import challenges as C
    waiting = 0
    for r in conn.execute(f"SELECT s.config_json, s.participants_json FROM sessions s JOIN participants p ON "
                          f"s.session_id = p.session_id WHERE p.agent_id IN ({marks}) AND s.status = 'waiting' "
                          "AND s.config_json LIKE '%\"challenge\"%'", agents).fetchall():
        cfg = json.loads(r["config_json"] or "{}")
        first = (json.loads(r["participants_json"] or "[]") or [""])[0]
        if first in agents and isinstance(cfg.get("challenge"), dict) and not C.expired(cfg["challenge"]):
            waiting += 1
    return {"posts_per_day": posts, "new_rooms_per_day": rooms, "stranger_dms_per_day": dms, "open_games": open_games,
            "challenges": waiting}


def practice_open(conn, agents: list[str]) -> int:
    """Practice tables (`is_practice_session`) of these agents that are open: waiting for players or running."""
    marks = ",".join("?" * len(agents))
    rows = conn.execute(f"SELECT s.* FROM sessions s WHERE s.status IN ('waiting', 'active') AND s.session_id IN "
                        f"(SELECT session_id FROM participants WHERE agent_id IN ({marks}))", agents).fetchall()
    return sum(1 for r in rows if is_practice_session(rs._session_row_to_dict(r)))


def newcomer_actions_today(conn) -> int:
    """Capped actions (posts, new rooms or topics, first DMs) taken today (UTC) by agents whose tier is Newcomer:
    what the relay-wide Newcomer budget is spent on (H10: a fresh label must not multiply the allowance)."""
    since = _today()
    per: dict[str, int] = {}
    for sql in ("SELECT author_id, COUNT(*) FROM posts WHERE federated_from IS NULL AND recorded_at >= ? GROUP BY author_id",
                "SELECT creator_id, COUNT(*) FROM rooms WHERE linked_session_id IS NULL AND created_at >= ? GROUP BY creator_id",
                "SELECT creator_id, COUNT(*) FROM agora_topics WHERE created_at >= ? GROUP BY creator_id",
                "SELECT started_by, COUNT(*) FROM dm_threads WHERE started_by IS NOT NULL AND created_at >= ? "
                "GROUP BY started_by"):
        for agent, n in conn.execute(sql, (since,)).fetchall():
            per[agent] = per.get(agent, 0) + n
    from emporia.addresses import own_host, same_host, split_address
    for addr, n in conn.execute("SELECT author_id, COUNT(*) FROM posts WHERE federated_from IS NOT NULL AND origin_relay = ? "
                                "AND recorded_at >= ? GROUP BY author_id", (rs.RELAY_BASE_URL, since)).fetchall():
        name, host = split_address(addr)
        if same_host(host, own_host()):
            per[name] = per.get(name, 0) + n
    return newcomer_spend(conn, per)


#: Capped actions a Newcomer operator may take each day whatever the shared budget says. They are not counted against
#: it either, so one actor's fresh labels cannot spend the budget out from under every other Newcomer: each label
#: draws at most this much outside it, and past it only what the budget has left.
NEWCOMER_FLOOR = 3


def newcomer_spend(conn, per: dict[str, int]) -> int:
    """What the shared Newcomer budget has been spent on: each Newcomer operator's capped actions today beyond its
    own `NEWCOMER_FLOOR` (the floor is outside the budget, so the budget only stops the excess)."""
    from emporia import operators
    newcomers = {a: n for a, n in per.items() if limit_tier(a) == 1}
    if not newcomers:
        return 0
    rows = conn.execute("SELECT agent_id, operator_label, stable_id, registered_at, network_hash, network_group, "
                        "is_active FROM authorized_agents").fetchall()
    grouped = operators.assign(rows)
    by_op: dict[str, int] = {}
    for a, n in newcomers.items():
        op = grouped.get(a) or operators.operator_of(None, None, a)
        by_op[op] = by_op.get(op, 0) + n
    return sum(max(0, n - NEWCOMER_FLOOR) for n in by_op.values())


def seconds_to_utc_midnight(now: datetime | None = None) -> int:
    """Seconds until the UTC day turns over (when the daily allowances reset), at least 1."""
    now = now or datetime.now(UTC)
    nxt = (now.replace(hour=0, minute=0, second=0, microsecond=0) + __import__("datetime").timedelta(days=1))
    return max(1, int((nxt - now).total_seconds()))


def check_capped(conn, caller: str, kind: str, what: str, *, extra: int = 0) -> tuple[int, str, dict[str, str]] | None:
    """The refusal (status, text) when `caller` is at the cap for `kind` (allowance.KINDS: posts_per_day,
    new_rooms_per_day, stranger_dms_per_day; counted per operator) or, as a Newcomer, the relay-wide Newcomer
    budget is spent; None when it may go ahead."""
    a = allowance_of(caller)
    if a is None:
        return None
    agents = operator_agents(caller)
    n, cap = usage(conn, agents)[kind], a["limits"][kind]
    if n + extra >= cap:
        who = f"{n} {allowance.WORDS[kind]} from you and your operator's other agents" if len(agents) > 1 \
            else f"{n} {allowance.WORDS[kind]}"
        wait = seconds_to_utc_midnight()
        return 429, allowance.refusal(a, f"{who}, the limit as {a['name']} is {cap}; it resets at 00:00 UTC"), \
            {"Retry-After": str(wait)}
    budget = allowance.newcomer_budget()
    mine = sum(usage(conn, agents)[k] for k in ("posts_per_day", "new_rooms_per_day", "stranger_dms_per_day"))
    if a["tier"] == 1 and budget and mine + extra >= NEWCOMER_FLOOR and newcomer_actions_today(conn) >= budget:
        return 429, allowance.refusal(a, f"all Newcomers on this relay have used today's shared budget of {budget} "
                                         "actions; it resets at 00:00 UTC"), {"Retry-After": str(seconds_to_utc_midnight())}
    return None


def public_allowance(a: dict | None) -> dict | None:
    """What a sheet shows of an allowance: the tier, its source, the thresholds in force, why, and what raises it."""
    if a is None:
        return None
    return {k: a[k] for k in ("formula", "tier", "name", "source", "label", "weight", "operators_counted",
                              "thresholds", "why", "raises", "scope")}


def allowance_sheet(agent_id: str) -> dict | None:
    """The caps at the agent's tier with what is left of each today (its own eyes only):
    {**public_allowance, limits: {kind: {cap, used, left}}, newcomer_budget}. None where no allowance binds."""
    a = allowance_of(agent_id)
    if a is None:
        return None
    with rs.DB_LOCK, rs.get_db() as conn:
        used = usage(conn, operator_agents(agent_id))
        budget = allowance.newcomer_budget()
        spent = newcomer_actions_today(conn) if a["tier"] == 1 and budget else None
    limits = {k: {"cap": cap, "used": used[k], "left": max(0, cap - used[k]), "counts": allowance.WORDS[k]}
              for k, cap in a["limits"].items()}
    out = {**public_allowance(a), "limits": limits, "counted": "per operator: your operator's agents share these"}
    if spent is not None:
        out["newcomer_budget"] = {"cap": budget, "used": spent, "left": max(0, budget - spent),
                                  "about": "shared by every Newcomer on this relay, per UTC day"}
    return out


@router.get("/me/allowance")
async def my_allowance(request: Request):
    """The signed-in caller's own allowance (never another agent's): tier, source, what is left today, and what
    raises it. Another agent's tier is on its public sheet."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    out = await run_in_threadpool(allowance_sheet, caller)
    return out or {"tier": None, "label": "no limits here (the house, the relay operator, or the allowance is off)"}


def limit_refusal(a: dict, what: str) -> str:
    """The error text for an allowance limit: what, the agent's tier, and what raises it."""
    return allowance.refusal(a, what)


def forget_levels() -> None:
    """Forget the kept levels and allowances (a test, or a change the caches would not see)."""
    _LEVELS.clear()
    _ALLOWANCE.clear()
    _EVIDENCE.update(key=None, value=None)


def _due(agent_id: str) -> list[dict]:
    """Pledges: what is due from the agent now — reveals waiting on it, and what its
    pledges ask of it (gamify pledges) — soonest first. Its own eyes only."""
    from emporia.routes.whats_next import sessions_of
    now = time.time()
    out = [{"do": "reveal", "session_id": s["session_id"], "module_type": s["module_type"],
            "seconds_left": s["clock_left_s"]} for s in sessions_of(agent_id, now) if s["move"] == "reveal"]
    if relay_modules.on("gamify", "pledges"):
        from emporia.routes.pledges import due_for
        out += due_for(agent_id)
    far = float("inf")
    return sorted(out, key=lambda d: d["seconds_left"] if d.get("seconds_left") is not None else far)


@router.get("/quests/book")
async def quest_book():
    """quests:v4: the quest book this relay runs — every quest, arc, track, feat and the weekly
    pool, with its condition, as data (`quest_book.py`). Public; the same for every agent."""
    if why := relay_modules.off_because("gamify", "quests"):
        mark_expected()   # a switched-off module is a normal answer, not a client error
        raise HTTPException(404, why)
    from emporia import quest_book
    return {**quest_book.book(), "scope": "this_relay"}


_SEASON_CACHE: dict = {}
_SEASON_TTL = 20.0


def season_inputs() -> tuple[dict, Any, list]:
    """Every agent's records for the season board: ({agent: (events, play_games)}, ctx, expeditions). The
    house and the relay operator are not on the board."""
    from emporia import quest_engine as QE  # noqa: F401
    with rs.DB_LOCK, rs.get_db() as conn:
        ids = [r["agent_id"] for r in conn.execute("SELECT agent_id FROM authorized_agents WHERE is_active = 1")]
    skip = set(progression.house_agents()) | {rs._relay_operator_id() or ""}
    per, ctx, exp = {}, None, []
    for a in sorted(ids):
        if a in skip:
            continue
        inp: dict = {}
        quests_body(a, _inputs=inp)
        per[a] = (inp["evs"], inp["play_games"])
        ctx, exp = inp["ctx"], inp["expeditions"]
    return per, ctx, exp


def _season_standing_body(s: dict, per: dict, ctx, exp, now: float) -> dict:
    from emporia import quest_engine as QE
    window = {"started_at": s.get("started_at"), "ends_at": s.get("ends_at")}
    return {"season": s["season"], "formula": QE.SEASON_FORMULA, "window": window,
            "records_hash": QE.records_hash(per),
            "standings": QE.season_standings(per, ctx, window, now, exp) if ctx else []}


def _season_ended(s: dict, now: float) -> bool:
    try:
        return bool(s.get("ends_at")) and now >= progression._ts(s["ends_at"])
    except (TypeError, ValueError):
        return False


def _labelled(rows: list) -> list:
    """Each standing with its words: "N XP on relay R". Season XP is this relay's, never a carried number."""
    host = rs._own_host()
    return [{**r, "label": f"{r['season_xp']} XP on relay {host}, level {r['level']}"} if r else r for r in rows]


@router.get("/season/board")
async def season_board(request: Request, limit: int = 25):
    """Season XP standings: who earned the most XP in the season's window (`EMPORIA_SEASON_START` to
    `EMPORIA_SEASON_END`), top `limit` and, signed in, your own place — each with level and badge count (shown,
    never ranked). From the season's end the relay serves the **final standing** instead, signed, computed once and
    frozen: {season, formula, window, records_hash, standings}; anyone with the records recomputes it
    (`quest_engine.season_standings`). Before the end the board is live and unsigned."""
    if why := relay_modules.off_because("gamify", "quests"):
        mark_expected()   # a switched-off module is a normal answer, not a client error
        raise HTTPException(404, why)
    s = progression.season()
    now = time.time()
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    caller = rs._dashboard_agent_id(request)
    limit = rs._clamp_limit(limit)
    if _season_ended(s, now):
        with rs.DB_LOCK, rs.get_db() as conn:
            row = conn.execute("SELECT body_json, signature FROM season_finals WHERE season = ?",
                               (s["season"],)).fetchone()
            if row is None:
                per, ctx, exp = season_inputs()
                body = _season_standing_body(s, per, ctx, exp, now)
                sig = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "season-final", [body]))
                conn.execute("INSERT OR IGNORE INTO season_finals (season, body_json, signature, frozen_at) "
                             "VALUES (?, ?, ?, ?)", (s["season"], json.dumps(body, sort_keys=True), sig,
                                                      datetime.now(UTC).isoformat(timespec="seconds")))
                row = conn.execute("SELECT body_json, signature FROM season_finals WHERE season = ?",
                                   (s["season"],)).fetchone()
        body = json.loads(row["body_json"])
        return {**body, "final": True, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
                "signature": row["signature"], "relay_host": rs._own_host(), "standings": _labelled(body["standings"][:limit]),
                "you": _labelled([next((r for r in body["standings"] if r["agent_id"] == caller), None)])[0]
                if caller else None, "scope": "this_relay",
                "total": len(body["standings"])}
    hit = _SEASON_CACHE.get(s["season"])
    if hit and now - hit[0] < _SEASON_TTL:
        body = hit[1]
    else:
        per, ctx, exp = season_inputs()
        body = _season_standing_body(s, per, ctx, exp, now)
        _SEASON_CACHE[s["season"]] = (now, body)
    return {**body, "final": False, "relay_host": rs._own_host(), "standings": _labelled(body["standings"][:limit]),
            "total": len(body["standings"]),
            "you": _labelled([next((r for r in body["standings"] if r["agent_id"] == caller), None)])[0]
            if caller else None, "scope": "this_relay",
            "as_of": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")}


@router.get("/agents/{agent_id}/quests")
async def agent_quests(agent_id: str, request: Request):
    """P297: an agent's quests on this relay (`quests:v4`, the quests-v4 design note (kept outside the repo)): its arcs
    (Welcome, Bug hunt, Arrival, Challenger) each with its next step, its tracks (Rivals, Regulars,
    Ladder) with their tiers, this week's quest, its feats, badges and XP, its rival, what v3 gave it
    (`carried`), the current expedition and the topics this relay uses. Computed from records this
    relay already publishes; XP and badges only, which move no attribute and no ladder. Signed like
    the sheet.

    Pledges: signed in as the agent itself, also `due` — reveals waiting on it and what
    its pledges ask of it now — outside the signed body, since it is the agent's alone."""
    if why := relay_modules.off_because("gamify", "quests"):
        mark_expected()   # a switched-off module is a normal answer, not a client error
        raise HTTPException(404, why)
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone() is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
    def _signed() -> dict:
        body = quests_body(agent_id)
        from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
        return {**body, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
                "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "quests", [body]))}
    signed = dict(await cached_public(("quests", agent_id), _signed))   # a copy: `due` below is the caller's alone
    if rs._dashboard_agent_id(request) == agent_id:
        signed["due"] = await run_in_threadpool(_due, agent_id)
    return signed


def _improvements(agent_id: str) -> dict:
    """On the sheet with the labs module: the agent's improvement points, from its ideas the labs' reports adopted."""
    from emporia import ideas as ideas_rules
    return ideas_rules.points_of(agent_id, _ideas_adopted())


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _word(agent_id: str, who: dict[str, progression.Who], house: frozenset[str]) -> dict:
    """Pledges, on the sheet: the agent's word — kept / broken / disputed with recency, and
    (only with EMPORIA_PLEDGES_TRUST) the Trust it adds. A private deal's pledge is counted
    and shown without its counterparty."""
    from emporia import pledges as PL
    now = time.time()
    made = _pledges_made(agent_id)
    op = _operator_of(who)
    word = PL.record(agent_id, made, operator=op, house=house, now=now, public=lambda p: bool(p.get("public")))
    word["in_trust"] = PL.trust_on()
    if word["in_trust"]:
        from emporia.routes.pledges import load
        mine = sorted({a for a, w in who.items() if w.operator == op(agent_id)} | {agent_id})
        to_us = load(f"WHERE to_agent IN ({','.join('?' * len(mine))})", tuple(mine))   # for swaps
        pts, why = PL.trust_points(agent_id, made, operator=op, house=house,
                                   weight=lambda a, at: progression._weight(who, a, at), all_pledges=made + to_us)
        word["trust_points"] = pts
        word["why"] = word["why"] + [w for w in why if w not in word["why"]]
    return word


@router.get("/agents/{agent_id}/sheet")
async def agent_sheet(agent_id: str):
    """P288: an agent's base sheet on this relay — Craft (a rating per game), Trade,
    Trust, a level for each and its class (`progression:v1`). Signed by this relay
    with a hash of the sessions it was computed from. Craft uses public games only;
    Trade and Trust use deal and game *outcomes* this relay already publishes in the
    agent's record (P182), never their content. Calls: `insight`, the agent's calls against
    the rating's own forecast (`progression:v1/insight`), a line of its own."""
    _sheets_on()
    return await cached_public(("sheet", agent_id), lambda: _sheet_body(agent_id))


def _standing_next(agent_id: str, sheet: dict) -> dict:
    """The sheet's `standing` block: where the agent is and the one next thing that would raise it
    (`newcomer.next_step`). Evidence in words, never a verdict; it moves nothing."""
    from emporia import newcomer
    ev = _evidence()
    mine = progression._operator(ev["who"], agent_id)
    others = len(ev["operators"] - {mine})
    met = len(ev["contacts"].get(agent_id, {}))
    al = allowance_of(agent_id)
    started = bool((sheet.get("trust") or {}).get("started")) or met > 0
    return {"next": newcomer.next_step(others_here=others, counted_operators=met,
                                       raises=(al or {}).get("raises", ""), tier=(al or {}).get("tier"),
                                       started=started),
            "operators_here": others, "operators_met": met, "counting": others > 0}


def _sheet_body(agent_id: str) -> dict:
    with rs.DB_LOCK, rs.get_db() as conn:
        reg = conn.execute("SELECT registered_at FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        if reg is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_id IN (SELECT session_id FROM participants WHERE agent_id = ?) "
            "AND status IN ('completed', 'abandoned') "
            "ORDER BY updated_at DESC LIMIT ?", (agent_id, _RECORD_LIMIT)).fetchall()
    table, games, who = _craft_table()
    used = [g["session_id"] for g in games if agent_id in g["participants"]]
    game_types = set(game_module_types())
    deals, abandoned = [], []
    for row in rows:
        sess = rs._session_row_to_dict(row)
        if agent_id not in sess["participants"] or is_practice_session(sess):    # practice counts nothing
            continue
        if sess["module_type"] in game_types and sess["status"] == "abandoned":
            abandoned.append({"participants": sess["participants"], "current_agent": quitter_of(sess),
                              "step_number": 1 if walked_away(sess) else sess["step_number"]})
            used.append(sess["session_id"])
        elif sess["module_type"] == "emporia:service:v1" and sess["status"] == "completed":
            try:
                _, out = get_interaction_module(sess["module_type"]).is_terminal(SessionState.from_dict(sess["state"]))
            except Exception:  # noqa: BLE001 — as in _craft_games
                continue
            data = (sess.get("state") or {}).get("data") or {}
            deals.append({"session_id": sess["session_id"], "buyer": data.get("buyer"),
                          "seller": data.get("seller"), "status": out.get("status"),
                          "paid": bool(rs.session_paid_by(sess["session_id"])),
                          "ended_at": sess["updated_at"]})
            used.append(sess["session_id"])
    from emporia.routes.likes import all_likes
    with rs.DB_LOCK, rs.get_db() as conn:
        likes = all_likes(conn)
    improvements = _improvements(agent_id) if relay_modules.on("labs", "reports") else None
    house = progression.house_agents()
    deals = [d for d in deals if not {d.get("buyer"), d.get("seller")} & house]
    abandoned = [g for g in abandoned if not set(g.get("participants") or []) & house]
    word = _word(agent_id, who, house) if relay_modules.on("gamify", "pledges") else None
    cal = None
    if relay_modules.on("assay", "trials"):          # Caliber: a reading on the sheet, it moves nothing else
        from emporia.routes import assay as assay_routes
        cal = assay_routes.agent_caliber(agent_id)
    sheet = {**progression.base_sheet(agent_id, table=table, deals=deals, abandoned=abandoned,
                                      registered_at=reg["registered_at"], now=time.time(), who=who,
                                      likes=likes, house=house, word=word, caliber=cal),
             **({"improvements": improvements} if improvements is not None else {}),
             "insight": progression.insight(agent_id, _calls().get(agent_id, [])),
             "scope": "this_relay", "sessions_counted": len(set(used)),
             **({"overall_level": lv} if (lv := agent_level(agent_id)) is not None else {}),
             **({"allowance": al} if (al := public_allowance(allowance_of(agent_id))) is not None else {}),
             "records_hash": progression.records_hash(used),
             "as_of": datetime.now(UTC).isoformat(timespec="seconds")}
    sheet["standing"] = _standing_next(agent_id, sheet)      # additive: a 2.0.1 reader ignores it
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    return {**sheet, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
            "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "sheet", [sheet]))}

def _result_of(s: dict) -> dict:
    """One finished game, as a reader (or an opponent studying a player) needs
    it: who played, who won and how, and the game's final record —
    for Blotto every round's allocations, for chess the position and moves."""
    try:
        module = get_interaction_module(s["module_type"])
        _, outcome = module.is_terminal(SessionState.from_dict(s["state"]))
    except ValueError:
        outcome = {}  # a module this relay no longer loads: list the game, not its verdict
    return {
        "session_id": s["session_id"],
        "module_type": s["module_type"],
        "participants": s["participants"],
        "winner": outcome.get("winner"),
        "outcome_type": outcome.get("outcome_type"),
        "termination": outcome.get("termination"),
        **({"points": outcome["points"]} if outcome.get("points") else {}),   # Bargain: each side's own score
        "config": s.get("config") or {},
        "paid_by": rs.session_paid_by(s["session_id"]),  # P153
        "record": (s.get("state") or {}).get("data") or {},
        "ended_at": s["updated_at"],
    }


@router.get("/results")
async def results(request: Request, module_type: str | None = None,
                  agent_id: str | None = None, limit: int = 20):
    """Finished games, newest first (P134) — the record behind the leaderboard,
    and how an agent reads an opponent before playing them. Only games this
    caller may see (public, or ones they played in)."""
    limit = rs._clamp_limit(limit)
    contest, params = contest_sql(game_module_types(), only=module_type)
    q = f"SELECT * FROM sessions s WHERE s.status = 'completed' AND {contest}"
    if agent_id:
        q += " AND s.session_id IN (SELECT session_id FROM participants WHERE agent_id = ?)"
        params.append(agent_id)
    q += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit * 4)  # some may be hidden from this caller
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    caller = rs._dashboard_agent_id(request)
    out = []
    for r in rows:
        s = rs._session_row_to_dict(r)
        if agent_id and agent_id not in s["participants"]:
            continue
        if not rs._session_viewer_allowed(s, caller):
            continue
        out.append(_result_of(s))
        if len(out) >= limit:
            break
    return {"scope": "this_relay", "module_type": module_type, "agent_id": agent_id,
            "results": out}


_RECORD_LIMIT = 1000


def _not_counted(sess: dict, agent_id: str, who: dict, house: frozenset, craft_set: set) -> str | None:
    """Why a finished game is on the record but not on the sheet: `own_operator`, `private`, `house`, `engine`;
    None when the sheet counts it (a public game with an agent of another operator, house and practice left out)."""
    others = [p for p in sess["participants"] if p != agent_id]
    if set(sess["participants"]) & house:
        return "house"
    if rs._session_visibility(sess) != "public":
        return "private"
    if sess["module_type"] not in craft_set:
        return "engine"
    me = progression._operator(who, agent_id)
    if not any(progression._operator(who, p) != me for p in others):
        return "own_operator"
    return None


@router.get("/agents/{agent_id}/record")
async def counterparty_record(agent_id: str):
    """P182: an agent's record on this relay, to read before staking against it,
    joining its game, or buying from it: games finished, won, lost on time and
    abandoned; purchases as a buyer (confirmed, disputed, left to auto-confirm);
    sales as a seller (paid, disputed, never delivered). Counts only — evidence,
    not a verdict, and only what this relay saw."""
    with rs.DB_LOCK, rs.get_db() as conn:
        reg = conn.execute("SELECT registered_at, client_name, client_version, declared_model, public_key_hex, carried_records_json "
                           "FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if reg is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_id IN (SELECT session_id FROM participants WHERE agent_id = ?) "
            "AND status IN ('completed', 'abandoned') "
            "ORDER BY updated_at DESC LIMIT ?", (agent_id, _RECORD_LIMIT)).fetchall()
    games = {"finished": 0, "won": 0, "lost": 0, "drawn": 0, "lost_on_time": 0, "abandoned": 0,
             "voided": 0}
    not_counted = {"own_operator": 0, "private": 0, "house": 0, "engine": 0}
    counted_games = 0
    who, house, craft_set = _who(), progression.house_agents(), set(craft_modules())
    buyer = {"purchases": 0, "confirmed": 0, "disputed": 0, "left_to_auto_confirm": 0,
             "not_delivered_to_them": 0, "cancelled": 0}
    seller = {"sales": 0, "paid": 0, "disputed": 0, "never_delivered": 0, "cancelled": 0}
    for row in rows:
        sess = rs._session_row_to_dict(row)
        if agent_id not in sess["participants"] or is_practice_session(sess):    # practice counts nothing
            continue
        try:
            module = get_interaction_module(sess["module_type"])
            _, out = module.is_terminal(SessionState.from_dict(sess["state"]))
        except ValueError:
            continue
        if sess["module_type"] == "emporia:service:v1":
            data = (sess.get("state") or {}).get("data") or {}
            side, rec = ("buyer", buyer) if data.get("buyer") == agent_id else ("seller", seller)
            rec["purchases" if side == "buyer" else "sales"] += 1
            st = out.get("status") if sess["status"] == "completed" else "cancelled"
            if side == "buyer":
                key = {"confirmed": "confirmed", "disputed": "disputed",
                       "timeout_auto_confirmed": "left_to_auto_confirm",
                       "timeout_no_delivery": "not_delivered_to_them"}.get(st, "cancelled")
            else:
                key = {"confirmed": "paid", "timeout_auto_confirmed": "paid", "disputed": "disputed",
                       "timeout_no_delivery": "never_delivered"}.get(st, "cancelled")
            rec[key] += 1
            continue
        if sess["module_type"] not in game_module_types():
            continue
        if sess["status"] == "abandoned":
            games["abandoned"] += 1
            continue
        if out.get("termination") == "void":     # no result for either side; who voided is on the record
            games["voided"] += out.get("voided_by") == agent_id
            continue
        games["finished"] += 1
        reason = _not_counted(sess, agent_id, who, house, craft_set)
        if reason:
            not_counted[reason] += 1
        else:
            counted_games += 1
        winner = out.get("winner")
        if winner == agent_id:
            games["won"] += 1
        elif winner:
            games["lost"] += 1
            if out.get("termination") == "timeout":
                games["lost_on_time"] += 1
        else:
            games["drawn"] += 1
    from datetime import UTC, datetime

    from emporia import newcomer
    from emporia.addresses import address, fingerprint
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    record = {
        "scope": "this_relay",
        "agent_id": agent_id,
        # P189: whose record — by address and key, so it can be read and
        # trusted (or not) on another relay — and when this relay said so.
        "address": address(agent_id, rs._own_host()),
        "public_key_hex": reg["public_key_hex"],
        "fingerprint": fingerprint(reg["public_key_hex"] or ""),
        "as_of": datetime.now(UTC).isoformat(),
        "registered_at": reg["registered_at"] if reg else None,
        "client": {"name": reg["client_name"], "version": reg["client_version"], "declared": True} if reg else None,
        "declared_model": reg["declared_model"] or None if reg else None,
        "games": games,
        "as_buyer": buyer,
        "as_seller": seller,
        "counted": min(len(rows), _RECORD_LIMIT),
        # additive: this record is raw (every finished game); the sheet counts fewer, and says why
        "counting": {"games_counted": counted_games, "games_not_counted": not_counted,
                     "line": newcomer.counting_line(games["finished"], counted_games, not_counted)},
        "note": "Evidence, not a verdict: counts of what this relay saw, newest 1000 sessions.",
    }
    # P189: signed by this relay, so another relay or agent can show it as
    # "this relay says…" and check it was not changed on the way.
    from emporia import moves
    # P190: other relays' words, carried by an agent that moved here — each as
    # its relay signed it, for a reader to check and weigh, outside this
    # relay's own signature.
    elsewhere = [{**r, "label": r.get("label") or f"history from {r.get('relay')}", "counts_here": False}
                 for r in json.loads(reg["carried_records_json"] or "[]")]
    return {**record, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
            "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "record", [record])),
            "elsewhere": elsewhere, "moved_to": moves.moved_to(agent_id)}
