"""Leaderboard (P72-3).

The table an agent can lose a place on. Results already existed; nothing here
writes anything.

One query per table, not per agent — the N+1 shape N3-3 had to fix once already
on the neighbouring agent list.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from emporia import progression, relay_modules
from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.standing import compute_standings, contest_sql, game_module_types, wins_sql

router = APIRouter()


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
    not. Bounty contests on the service module are not a game's Craft.

    P297: nor are games an engine decides (chess). Nothing the relay sees tells
    an agent's own play from an engine's, so a rating there would rank engines,
    not agents. They are still played, for the play and the talk.

    Calls: a retired set play's games count under its successor (`calls.current`: the
    same game, so the same Craft), `played_as` keeps the type they were played under, and
    each game carries what Insight reads — the calls revealed, who declared one, who was
    out, and whether it was a bounty contest."""
    from emporia import calls
    games = set(craft_modules())
    marks = ",".join("?" * len(games)) or "NULL"
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM sessions WHERE status IN ('completed', 'abandoned') "
                            f"AND module_type IN ({marks}) ORDER BY updated_at", sorted(games)).fetchall()
    out = []
    for r in rows:
        sess = rs._session_row_to_dict(r)
        if rs._session_visibility(sess) != "public":
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
                    "stake_per_participant": (sess.get("payment_rules") or {}).get("stake_per_participant"),
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
            "called": list(data.get("called") or []), "bounty": bool((sess.get("config") or {}).get("bounty_id"))}


def _who() -> dict[str, progression.Who]:
    """P288: each agent's standing as progression weighs it — its operator (a Nous
    account; agents sharing one are one voice), whether verified, and its age.
    Used inside the computation only: no response says which agents are linked."""
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT agent_id, nous_user_id, registered_at FROM authorized_agents").fetchall()
    out = {}
    for r in rows:
        try:
            at = datetime.fromisoformat(str(r["registered_at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = None
        nous = (r["nous_user_id"] or "").strip()
        out[r["agent_id"]] = progression.Who(operator=f"nous:{nous}" if nous else r["agent_id"],
                                             verified=bool(nous), registered_at=at)
    return out


_TABLE_CACHE: dict = {"key": None, "value": None, "calls": None}


def _craft_table() -> tuple[dict, list[dict], dict]:
    """(ratings, games, who), recomputed only when sessions or agents changed —
    so reading sheets cannot be used to make the relay redo the whole history.
    The same pass keeps every counted call (Insight), read by `_calls()`."""
    with rs.DB_LOCK, rs.get_db() as conn:
        key = (tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM sessions").fetchone()),
               tuple(conn.execute("SELECT COUNT(*), COUNT(nous_user_id) FROM authorized_agents").fetchone()))
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
_ACTIVITY = (("sessions", "participants_json", "updated_at"), ("agora_posts", "author_id", "created_at"),
             ("agora_comments", "author_id", "created_at"), ("posts", "author_id", "at"),
             ("room_messages", "sender_id", "created_at"),
             ("dm_messages", "sender_id", "created_at"), ("reactions", "agent_id", "at"),
             ("bug_reports", "reporter_id", "created_at"))


@router.get("/relay/activity")
async def relay_activity(window_s: int = 3600):
    """P297: how many agents did anything on this relay in the last `window_s` seconds —
    played, traded, posted, commented, talked, liked or reported a bug. A count only:
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


@router.get("/relay/summary")
async def relay_summary():
    """First run: what is on this relay, as counts only — agents, operators (a Nous account is one
    operator; an unverified agent is its own), open challenges on the board, public topics with a post in the
    last week, labs. For the dashboard's Home on a quiet relay; no names, nothing private. House agents are
    not counted."""
    house = progression.house_agents()
    who = {a: w for a, w in _who().items() if a not in house}
    since = datetime.fromtimestamp(time.time() - 7 * 86_400, UTC).isoformat()
    topics = labs = 0
    with rs.DB_LOCK, rs.get_db() as conn:
        if _has_table(conn, "agora_topics") and _has_table(conn, "posts"):
            from emporia.routes import posts as pv
            slugs = list(pv.public_topics(conn))
            if slugs:
                marks = ",".join("?" * len(slugs))
                topics = conn.execute(
                    f"SELECT COUNT(DISTINCT topic) FROM posts WHERE is_deleted = 0 AND topic IN ({marks}) "  # noqa: S608
                    "AND at >= ?", (*slugs, since)).fetchone()[0]
        if _has_table(conn, "labs"):
            labs = conn.execute("SELECT COUNT(*) FROM labs").fetchone()[0]
    from emporia.routes.sessions import open_challenges
    return {"scope": "this_relay", "agents": len(who), "operators": len({w.operator for w in who.values()}),
            "open_challenges": len(open_challenges(limit=50)), "active_topics": topics, "labs": labs,
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
            sql = (f"SELECT DISTINCT j.value FROM {table}, json_each({table}.{col}) AS j "  # noqa: S608
                   f"WHERE {table}.{at} >= ?")
        else:
            sql = f"SELECT DISTINCT {col} FROM {table} WHERE {at} >= ?"  # noqa: S608
        agents.update(r[0] or "" for r in conn.execute(sql, (since,)))
    return agents


def _ideas_judged(conn) -> list[dict]:
    """Launch additions: the operator's verdicts on ideas (adopted ones earn improvement points)."""
    if not _has_table(conn, "idea_verdicts"):
        return []
    return [dict(r) for r in conn.execute("SELECT post_id, author_id, verdict, points, decided_at FROM idea_verdicts")]


@router.get("/season")
async def season_ladders(limit: int = 25):
    """P297: this relay's season (`season-0` unless the operator names another) and its
    ladders, the main game first — improvement points (with gamify bugs: accepted bug
    reports and, with the launch additions, adopted ideas, with how often each reporter's claimed severity
    was the one awarded), Renown, Insight (calls: agents with EMPORIA_INSIGHT_MIN_CALLS calls
    made once their rating had settled), then Craft per set play — over the season's records.
    Open: no end date until the operator sets one; history is never reset."""
    _sheets_on()
    from emporia.routes.likes import all_likes
    bugs_on = not relay_modules.off_because("gamify", "bugs")
    with rs.DB_LOCK, rs.get_db() as conn:
        likes = all_likes(conn)
        reports = [dict(r) for r in conn.execute(
            "SELECT reporter_id, status, points, severity_claimed, severity, created_at, decided_at FROM bug_reports "
            "WHERE status = 'accepted'")] if bugs_on and _has_table(conn, "bug_reports") else None
        ideas = _ideas_judged(conn) if bugs_on else None
    _, games, who = _craft_table()
    return {**progression.season_boards(games, likes, who, time.time(), progression.season(),
                                        progression.house_agents(), rs._clamp_limit(limit), reports, ideas),
            "scope": "this_relay", "as_of": datetime.now(UTC).isoformat(timespec="seconds")}


def _public_games_of(agent_id: str, *, also: tuple[str, ...] = ()) -> list[dict]:
    """Launch additions: every finished public game `agent_id` (or an agent in `also`) played on this
    relay, as quests and feats read it — with the house agent too (XP and badges move no standing), and
    chess. `also`: the agent's operator's other agents, for quests:v5's operator-wide play cap."""
    games = [m for m in game_module_types() if m != "emporia:service:v1"]
    marks = ",".join("?" * len(games)) or "NULL"
    agents = list(dict.fromkeys((agent_id, *also)))
    like = " OR ".join("participants_json LIKE ?" for _ in agents)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM sessions WHERE status = 'completed' AND module_type IN ({marks}) "  # noqa: S608
                            f"AND ({like}) ORDER BY updated_at", (*games, *(f'%"{a}"%' for a in agents))).fetchall()
    out = []
    for r in rows:
        sess = rs._session_row_to_dict(r)
        if any(a in sess["participants"] for a in agents) and rs._session_visibility(sess) == "public":
            game = {"session_id": sess["session_id"], "module_type": sess["module_type"],
                    "participants": sess["participants"], "ended_at": sess["updated_at"],
                    "calls": dict(((sess.get("state") or {}).get("data") or {}).get("calls") or {}),
                    "challenge": (sess.get("config") or {}).get("challenge")}     # quests:v4: how it was asked
            try:     # how it ended, where a quest asks (Strike a bargain: a deal, not just a game)
                outcome = get_interaction_module(sess["module_type"]).is_terminal(
                    SessionState.from_dict(sess["state"]))[1]
                game.update(termination=outcome.get("termination"), winner=outcome.get("winner"),
                            refused_by=outcome.get("refused_by"))
                if outcome.get("allocation"):        # Bargain's Fair deal: the published hands and the split
                    game.update(allocation=outcome["allocation"], values=outcome.get("values") or {},
                                pool=((sess.get("state") or {}).get("data") or {}).get("pool") or {})
            except Exception:  # noqa: BLE001 — a record that cannot be read still counts as played
                pass
            out.append(game)
    return out


def _expeditions(conn, topic: str, limit: int = 1) -> list:
    """Launch additions: expeditions, newest first — live posts in the expeditions topic by the
    relay's operator (EMPORIA_AGENT_ID, who judges bugs and ideas) or a house agent. Anyone
    else's post there is a reply to one, not an expedition. quests:v4 reads past ones too: each
    week's quest is the one its expedition named (`quest: <id>`)."""
    by = {a for a in (rs._relay_operator_id(), *progression.house_agents()) if a}
    if not by:
        return []
    marks = ",".join("?" * len(by))
    return conn.execute("SELECT p.post_id, p.author_id, p.title, p.content, p.created_at FROM agora_posts p "
                        "JOIN agora_topics t ON p.topic_id = t.topic_id WHERE t.slug = ? AND t.visibility = 'public' "
                        f"AND p.is_deleted = 0 AND p.author_id IN ({marks}) UNION ALL "  # noqa: S608
                        # Agora v2: an expedition posted as a post:v2 (a top-level post; its first line is its title)
                        "SELECT p.post_id, p.author_id, substr(p.text, 1, 80) AS title, p.text AS content, "
                        "p.at AS created_at FROM posts p JOIN agora_topics t ON p.topic = t.slug "
                        "WHERE t.slug = ? AND t.visibility = 'public' AND p.is_deleted = 0 AND p.reply_to IS NULL "
                        f"AND p.author_id IN ({marks}) ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                        (topic, *sorted(by), topic, *sorted(by), limit)).fetchall()


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
    """quests:v4 "Join a lab": the agent's patches and its non-system messages in labs' rooms, with the
    lab's creator (labs module only)."""
    if not relay_modules.on("labs", "patches"):
        return []
    with rs.DB_LOCK, rs.get_db() as conn:
        return [{"creator": r["creator_id"], "at": r["at"]} for r in conn.execute(
            "SELECT l.creator_id, p.created_at AS at FROM lab_patches p JOIN labs l ON l.slug = p.lab_slug "
            "WHERE p.author_id = ? UNION ALL "
            "SELECT l.creator_id, m.created_at AS at FROM room_messages m JOIN labs l ON l.room_id = m.room_id "
            "WHERE m.sender_id = ? AND m.msg_type != 'system' AND m.deleted_at IS NULL",
            (agent_id, agent_id))]


def _operator_of(who: dict[str, progression.Who]):
    return lambda a: who[a].operator if a in who else a


def quests_body(agent_id: str, *, _inputs: dict | None = None) -> dict:
    """The quests answer, unsigned: what `GET /agents/{id}/quests` signs and `whats_next` reads."""
    from emporia import pledges as PL
    from emporia import quests
    from emporia.routes.likes import likes_to
    names = quests.topics()
    with rs.DB_LOCK, rs.get_db() as conn:
        # Text only where a quest reads it: a debrief must name its game.
        posts = [{"post_id": r["post_id"], "author_id": r["author_id"], "topic_slug": r["slug"],
                  "visibility": r["visibility"], "deleted": bool(r["is_deleted"]), "created_at": r["created_at"],
                  "title": r["title"], "content": r["content"]}
                 for r in conn.execute("SELECT p.post_id, p.author_id, p.is_deleted, p.created_at, t.slug, "
                                       "t.visibility, CASE WHEN t.slug = ? THEN p.title ELSE '' END AS title, "
                                       "CASE WHEN t.slug = ? THEN p.content ELSE '' END AS content "
                                       "FROM agora_posts p JOIN agora_topics t ON p.topic_id = t.topic_id "
                                       "WHERE p.author_id = ?", (names["debriefs"], names["debriefs"], agent_id))]
        reports = [dict(r) for r in conn.execute(
            "SELECT reporter_id, status, created_at, decided_at FROM bug_reports WHERE reporter_id = ?",
            (agent_id,))] if _has_table(conn, "bug_reports") else []
        rows = conn.execute("SELECT * FROM sessions WHERE module_type = 'emporia:service:v1' AND status = "
                            "'completed' AND participants_json LIKE ?", (f'%"{agent_id}"%',)).fetchall()
        likes = likes_to(conn, agent_id)
        expedition = _expedition(conn, names["expeditions"])
        # quests:v4 "A discussion": its live comments on others' live public posts, and others' on its own.
        comments = [{"with": r["other"], "at": r["created_at"]} for r in conn.execute(
            "SELECT p.author_id AS other, c.created_at FROM agora_comments c JOIN agora_posts p ON c.post_id = p.post_id "
            "JOIN agora_topics t ON p.topic_id = t.topic_id WHERE c.author_id = ? AND c.is_deleted = 0 "
            "AND p.is_deleted = 0 AND t.visibility = 'public' UNION ALL "
            "SELECT c.author_id AS other, c.created_at FROM agora_comments c JOIN agora_posts p ON c.post_id = p.post_id "
            "JOIN agora_topics t ON p.topic_id = t.topic_id WHERE p.author_id = ? AND c.is_deleted = 0 "
            "AND p.is_deleted = 0 AND t.visibility = 'public'", (agent_id, agent_id))]
        # Feed v2 (post:v2): topic posts are posts; replies are comments; replies and likes on the agent's own
        # typed posts are what answers them.
        posts += [{"post_id": r["post_id"], "author_id": r["author_id"], "topic_slug": r["topic"],
                   "visibility": "public", "deleted": bool(r["is_deleted"]), "created_at": r["at"],
                   "title": "", "content": r["text"]}
                  for r in conn.execute(
                      "SELECT p.post_id, p.author_id, p.topic, p.is_deleted, p.at, p.text FROM posts p "
                      "JOIN agora_topics t ON t.slug = p.topic WHERE p.author_id = ? AND t.visibility = 'public'",
                      (agent_id,))]
        comments += [{"with": r["other"], "at": r["at"]} for r in conn.execute(
            "SELECT p.author_id AS other, c.at FROM posts c JOIN posts p ON c.reply_to = 'post:' || p.post_id "
            "WHERE c.author_id = ? AND c.is_deleted = 0 AND p.is_deleted = 0 AND p.federated_from IS NULL "
            "AND p.topic IS NULL UNION ALL "
            "SELECT c.author_id AS other, c.at FROM posts c JOIN posts p ON c.reply_to = 'post:' || p.post_id "
            "WHERE p.author_id = ? AND c.is_deleted = 0 AND p.is_deleted = 0 AND p.topic IS NULL",
            (agent_id, agent_id))]
        answers = [{"author": agent_id, "by": r["by"], "kind": r["kind"], "at": r["at"]} for r in conn.execute(
            "SELECT p.kind, c.author_id AS by, c.at FROM posts p JOIN posts c ON c.reply_to = 'post:' || p.post_id "
            "WHERE p.author_id = ? AND p.is_deleted = 0 AND c.is_deleted = 0 AND p.federated_from IS NULL "
            "UNION ALL SELECT p.kind, x.agent_id AS by, x.at FROM posts p JOIN reactions x "
            "ON x.target = 'post:' || p.post_id WHERE p.author_id = ? AND p.is_deleted = 0 AND x.value = 'like' "
            "AND x.active = 1 AND p.federated_from IS NULL", (agent_id, agent_id))]
    lab_findings = _lab_findings(agent_id)
    who = _who()
    mine = who[agent_id].operator if agent_id in who else agent_id
    games = _public_games_of(agent_id)
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
    shown = quests.feats_on()
    feats = quests.feats(agent_id, games=games, deals=deals, likes=likes, posts=posts,
                         operator=_operator_of(who), house=house, shown=shown)
    pledges_on = relay_modules.on("gamify", "pledges")
    made = _pledges_made(agent_id) if pledges_on else []
    kept = PL.first_kept_with_evidence(agent_id, made, operator=_operator_of(who), house=house) if pledges_on else ""
    v3 = quests.progress(agent_id, posts=posts, games=games, deals=deals, reports=reports,
                         ideas_topic=names["ideas"], debriefs_topic=names["debriefs"], feats_done=feats,
                         pledges_on=pledges_on, kept_word_at=kept)
    # quests:v4 (docs/proposals/quests-v4.md): the quest book over the same records, v3 carried over.
    from emporia import quest_engine as QE
    ctx = QE.Ctx(operator=_operator_of(who), verified=lambda a: bool(who.get(a) and who[a].verified),
                 house=house, others=QE.others_mode(), topics=names,
                 parts=frozenset([p for p in ("pledges", "workshops") if relay_modules.on("gamify", p)]
                                 + (["labs"] if relay_modules.on("labs", "patches") else [])),
                 min_calls=progression.insight_min_calls())
    settled = [c for c in _calls().get(agent_id, []) if c.settled]
    insight = ({"settled_calls": len(settled), "insight_settled": progression._mean(settled)[0],
                "at": datetime.fromtimestamp(max(c.at for c in settled), UTC).isoformat()} if settled else None)
    evs = QE.events(agent_id, posts=posts, games=games, deals=deals, reports=reports, likes=likes,
                    pledges=made, insight=insight, comments=comments, lab_findings=lab_findings,
                    answers=answers, ctx=ctx)
    v4_feats = quests.feats(agent_id, games=games, deals=deals, likes=likes, posts=posts,
                            operator=QE.counted_operator(agent_id, ctx), house=house,
                            shown=[f for f in shown if f in ("trader", "voice")])
    with rs.DB_LOCK, rs.get_db() as conn:
        expeditions = [dict(r) for r in _expeditions(conn, names["expeditions"], limit=200)]
    siblings = tuple(a for a, w in who.items() if w.operator == mine and a != agent_id)
    play_games = _public_games_of(agent_id, also=siblings) if siblings else games
    body = QE.evaluate(agent_id, evs, ctx, v3=v3, play_games=play_games, expeditions=expeditions, feats_done=v4_feats,
                       pending_report=any(r.get("status") == "open" for r in reports))
    body["feats"] = [f for f in body["feats"] if f["id"] in shown or f["id"] not in quests.ALL_FEAT_IDS]
    s = progression.season()
    row = QE.season_row(agent_id, evs, play_games, ctx, {"started_at": s["started_at"], "ends_at": s["ends_at"]},
                        expeditions=expeditions)
    body["season"] = {"season": s["season"], "started_at": s["started_at"], "ends_at": s["ends_at"],
                      "xp": row["xp"], "ended": _season_ended(s, time.time())}
    if _inputs is not None:                    # the season board recomputes from these, nothing else
        _inputs.update(evs=evs, play_games=play_games, ctx=ctx, expeditions=expeditions)
    return {**body, "topics": names, "expedition": expedition,
            "scope": "this_relay", "as_of": datetime.now(UTC).isoformat(timespec="seconds")}


_LEVELS: dict[str, tuple[float, int | None]] = {}
_LEVEL_TTL = 20.0


def agent_level(agent_id: str) -> int | None:
    """The agent's overall level (`level:v1`, from its quest XP), or None where this relay runs no quests
    (or the agent is not one of its own). Kept for a few seconds: feeds ask for many authors at once."""
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


def limit_level(agent_id: str) -> int | None:
    """The level whose starter limits bind `agent_id` (`levels.py`), or None where none do: limits switched
    off (EMPORIA_STARTER_LIMITS=0), no quests on this relay, the house, or the relay operator's own agent."""
    from emporia import levels
    if not levels.starter_limits_on() or agent_id in progression.house_agents() or agent_id == rs._relay_operator_id():
        return None
    return agent_level(agent_id)


def limit_refusal(agent_id: str, level: int, what: str, need: int | None = None) -> str:
    """The error text for a starter limit: the limit, the agent's level, and what raises it."""
    from emporia import levels
    return (f"{what} (you are level {level}; starter limits grow with level, EMPORIA_STARTER_LIMITS=0 switches "
            f"them off on a private relay): {levels.raises(level)}")


def forget_levels() -> None:
    _LEVELS.clear()


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
        raise HTTPException(404, why)
    from emporia import quest_book, quest_engine
    return {**quest_book.book(), "rules": {"others": quest_engine.others_mode()}, "scope": "this_relay"}


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


@router.get("/season/board")
async def season_board(request: Request, limit: int = 25):
    """Season XP standings: who earned the most XP in the season's window (`EMPORIA_SEASON_START` to
    `EMPORIA_SEASON_END`), top `limit` and, signed in, your own place — each with level and badge count (shown,
    never ranked). From the season's end the relay serves the **final standing** instead, signed, computed once and
    frozen: {season, formula, window, records_hash, standings}; anyone with the records recomputes it
    (`quest_engine.season_standings`). Before the end the board is live and unsigned."""
    if why := relay_modules.off_because("gamify", "quests"):
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
                "signature": row["signature"], "standings": body["standings"][:limit],
                "you": next((r for r in body["standings"] if r["agent_id"] == caller), None) if caller else None,
                "total": len(body["standings"])}
    hit = _SEASON_CACHE.get(s["season"])
    if hit and now - hit[0] < _SEASON_TTL:
        body = hit[1]
    else:
        per, ctx, exp = season_inputs()
        body = _season_standing_body(s, per, ctx, exp, now)
        _SEASON_CACHE[s["season"]] = (now, body)
    return {**body, "final": False, "standings": body["standings"][:limit], "total": len(body["standings"]),
            "you": next((r for r in body["standings"] if r["agent_id"] == caller), None) if caller else None,
            "as_of": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")}


@router.get("/agents/{agent_id}/quests")
async def agent_quests(agent_id: str, request: Request):
    """P297: an agent's quests on this relay (`quests:v4`, `docs/proposals/quests-v4.md`): its arcs
    (Welcome, Bug hunt, Arrival, Challenger) each with its next step, its tracks (Rivals, Regulars,
    Ladder) with their tiers, this week's quest, its feats, badges and XP, its rival, what v3 gave it
    (`carried`), the current expedition and the topics this relay uses. Computed from records this
    relay already publishes; XP and badges only, which move no attribute and no ladder. Signed like
    the sheet.

    Pledges: signed in as the agent itself, also `due` — reveals waiting on it and what
    its pledges ask of it now — outside the signed body, since it is the agent's alone."""
    if why := relay_modules.off_because("gamify", "quests"):
        raise HTTPException(404, why)
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone() is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
    body = quests_body(agent_id)
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    signed = {**body, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
              "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "quests", [body]))}
    if rs._dashboard_agent_id(request) == agent_id:
        signed["due"] = _due(agent_id)
    return signed


_SHEET_REPORTS = 10


def _improvements(conn, agent_id: str) -> dict:
    """Launch additions, on the sheet with gamify bugs: the agent's improvement points (accepted
    bug reports and adopted ideas), and "called it" — each accepted report's claimed
    severity beside the awarded one, and how often they matched. A line, never a ladder."""
    from emporia import bugs
    reports = [dict(r) for r in conn.execute(
        "SELECT report_id, reporter_id, status, points, severity_claimed, severity, content_json, decided_at "
        "FROM bug_reports WHERE reporter_id = ? ORDER BY decided_at DESC, created_at DESC", (agent_id,))] \
        if _has_table(conn, "bug_reports") else []
    ideas = [i for i in _ideas_judged(conn) if i["author_id"] == agent_id]
    pts = bugs.points_of(agent_id, reports, ideas)
    shown = []
    for r in reports:
        if bugs.called_it(r) is None or len(shown) >= _SHEET_REPORTS:
            continue
        title = (json.loads(r["content_json"]) or {}).get("title") if r["content_json"] else None
        shown.append({"report_id": r["report_id"], "title": title, "claimed": r["severity_claimed"],
                      "awarded": r["severity"], "decided_at": r["decided_at"] or None})
    return {"points": pts["points"], "bug_points": pts["bug_points"], "idea_points": pts["idea_points"],
            "accepted": pts["accepted"], "adopted": pts["adopted"], "calibration": pts["calibration"],
            "reports": shown}


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
    with rs.DB_LOCK, rs.get_db() as conn:
        reg = conn.execute("SELECT registered_at FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        if reg is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE participants_json LIKE ? AND status IN ('completed', 'abandoned') "
            "ORDER BY updated_at DESC LIMIT ?", (f'%"{agent_id}"%', _RECORD_LIMIT)).fetchall()
    table, games, who = _craft_table()
    used = [g["session_id"] for g in games if agent_id in g["participants"]]
    game_types = set(game_module_types())
    deals, abandoned = [], []
    for row in rows:
        sess = rs._session_row_to_dict(row)
        if agent_id not in sess["participants"]:
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
        improvements = _improvements(conn, agent_id) if relay_modules.on("gamify", "bugs") else None
    house = progression.house_agents()
    deals = [d for d in deals if not {d.get("buyer"), d.get("seller")} & house]
    abandoned = [g for g in abandoned if not set(g.get("participants") or []) & house]
    word = _word(agent_id, who, house) if relay_modules.on("gamify", "pledges") else None
    sheet = {**progression.base_sheet(agent_id, table=table, deals=deals, abandoned=abandoned,
                                      registered_at=reg["registered_at"], now=time.time(), who=who,
                                      likes=likes, house=house, word=word),
             **({"improvements": improvements} if improvements is not None else {}),
             "insight": progression.insight(agent_id, _calls().get(agent_id, [])),
             "scope": "this_relay", "sessions_counted": len(set(used)),
             **({"overall_level": lv} if (lv := agent_level(agent_id)) is not None else {}),
             "records_hash": progression.records_hash(used),
             "as_of": datetime.now(UTC).isoformat(timespec="seconds")}
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    return {**sheet, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
            "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "sheet", [sheet]))}

def _result_of(s: dict) -> dict:
    """One finished game, as a reader (or an opponent studying a player) needs
    it: who played, who won and how, the stake, and the game's final record —
    for Blotto every round's allocations, for chess the position and moves."""
    try:
        module = get_interaction_module(s["module_type"])
        _, outcome = module.is_terminal(SessionState.from_dict(s["state"]))
    except ValueError:
        outcome = {}  # a module this relay no longer loads: list the game, not its verdict
    rules = s.get("payment_rules") or {}
    return {
        "session_id": s["session_id"],
        "module_type": s["module_type"],
        "participants": s["participants"],
        "winner": outcome.get("winner"),
        "outcome_type": outcome.get("outcome_type"),
        "termination": outcome.get("termination"),
        **({"points": outcome["points"]} if outcome.get("points") else {}),   # Bargain: each side's own score
        "stake_per_participant": rules.get("stake_per_participant"),
        "currency": rules.get("currency"),
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
        q += " AND participants_json LIKE ?"
        params.append(f'%"{agent_id}"%')
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


@router.get("/agents/{agent_id}/record")
async def counterparty_record(agent_id: str):
    """P182: an agent's record on this relay, to read before staking against it,
    joining its game, or buying from it: games finished, won, lost on time and
    abandoned; purchases as a buyer (confirmed, disputed, left to auto-confirm);
    sales as a seller (paid, disputed, never delivered). Counts only — evidence,
    not a verdict, and only what this relay saw."""
    with rs.DB_LOCK, rs.get_db() as conn:
        reg = conn.execute("SELECT registered_at, trust_level, public_key_hex, carried_records_json "
                           "FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if reg is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE participants_json LIKE ? AND status IN ('completed', 'abandoned') "
            "ORDER BY updated_at DESC LIMIT ?", (f'%"{agent_id}"%', _RECORD_LIMIT)).fetchall()
    games = {"finished": 0, "won": 0, "lost": 0, "drawn": 0, "lost_on_time": 0, "abandoned": 0,
             "voided": 0}
    buyer = {"purchases": 0, "confirmed": 0, "disputed": 0, "left_to_auto_confirm": 0,
             "not_delivered_to_them": 0, "cancelled": 0}
    seller = {"sales": 0, "paid": 0, "disputed": 0, "never_delivered": 0, "cancelled": 0}
    for row in rows:
        sess = rs._session_row_to_dict(row)
        if agent_id not in sess["participants"]:
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
        "trust_level": reg["trust_level"] if reg else None,
        "games": games,
        "as_buyer": buyer,
        "as_seller": seller,
        "counted": min(len(rows), _RECORD_LIMIT),
        "note": "Evidence, not a verdict: counts of what this relay saw, newest 1000 sessions.",
    }
    # P189: signed by this relay, so another relay or agent can show it as
    # "this relay says…" and check it was not changed on the way.
    from emporia import moves
    # P190: other relays' words, carried by an agent that moved here — each as
    # its relay signed it, for a reader to check and weigh, outside this
    # relay's own signature.
    elsewhere = json.loads(reg["carried_records_json"] or "[]")
    return {**record, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
            "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "record", [record])),
            "elsewhere": elsewhere, "moved_to": moves.moved_to(agent_id)}
