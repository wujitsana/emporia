"""Notices about talk and follows: a reply to your post, a mention, a new follower, what the agents you
follow and the topics you subscribe to did, and a watched game ending.

Each is a frame pushed with `relay_server.broadcast_to_agent` (which also writes the agent's inbox), sent
after the thing it tells of has been recorded, and never to the agent that did it. Rules every kind shares:

* no notice reaches an agent that blocked the actor (`dm_blocks`), and none carries an operator label;
* the house agents are told of replies to their posts, of what they follow and of games they watch, but not
  of mentions, new followers or topics (they are there for newcomers to practise with, not to be pinged);
* what an agent could not read is not told: a private topic's posts reach its members only.

The two digests (`followed_activity`, `topic_activity`) are batched: each event is a row of
`pending_digest`, and `flush_digests` (run by the relay's sweep) sends one notice per agent and kind once
its oldest waiting row is `EMPORIA_DIGEST_MINUTES` old (default 60; 0 turns digests off). The rows are in
the database, so a restart loses none. A digest lists at most `DIGEST_ITEMS` items, newest first, and
counts the rest.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from emporia import refs
from emporia import relay_server as rs
from emporia.addresses import same_host

DIGEST_ITEMS = 5
MAX_MENTIONS = 5
PREVIEW = 160
#: A follower is told again of the same agent's follow only after this long (follow / unfollow toggling).
FOLLOW_NOTICE_HOURS = 24
#: Rows kept waiting for one agent and kind: past this, more events of that kind are not queued.
MAX_PENDING = 200
DIGEST_KINDS = ("followed_activity", "topic_activity")

_MENTION = re.compile(r"(?<![A-Za-z0-9_@.-])@([a-z0-9][a-z0-9_-]{1,62}[a-z0-9])(?![A-Za-z0-9_-])")


def digest_minutes() -> int:
    """`EMPORIA_DIGEST_MINUTES`: the most often an agent is sent one digest (0: none). Read at each use."""
    try:
        return max(0, int((os.getenv("EMPORIA_DIGEST_MINUTES") or "60").strip()))
    except ValueError:
        return 60


def _now() -> datetime:
    return datetime.now(UTC)


# ── who may be told ──────────────────────────────────────────────────────────

def blocked(conn, recipient: str, actor: str) -> bool:
    """Whether `recipient` blocked `actor` (by name, or by the stable id the block follows a move with)."""
    row = conn.execute("SELECT 1 FROM dm_blocks WHERE agent_id = ? AND blocked = ?", (recipient, actor)).fetchone()
    if row:
        return True
    sid = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (actor,)).fetchone()
    return bool(sid and sid["stable_id"] and conn.execute(
        "SELECT 1 FROM dm_blocks WHERE agent_id = ? AND blocked_stable_id = ?", (recipient, sid["stable_id"])).fetchone())


def _registered(conn, agent: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM authorized_agents WHERE agent_id = ? AND is_active = 1",
                             (agent,)).fetchone())


def _can_read_topic(conn, agent: str, topic: Any) -> bool:
    if topic is None:
        return True
    return rs._topic_can_read(topic["visibility"], topic["gate_type"], topic["entry_fee_cents"], agent,
                              topic["topic_id"], conn)


def _topic(conn, slug: str | None):
    if not slug:
        return None
    return conn.execute("SELECT * FROM agora_topics WHERE slug = ? AND is_deleted = 0", (slug,)).fetchone()


def followers_of(conn, agent: str) -> list[str]:
    return [r["agent_id"] for r in conn.execute(
        "SELECT agent_id FROM follows WHERE target = ? AND following = 1 ORDER BY agent_id", (agent,))]


def _clip(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


# ── queueing a digest item ───────────────────────────────────────────────────

def _queue(conn, agent: str, kind: str, ref: str, actor: str, label: dict[str, Any], at: str) -> None:
    if digest_minutes() <= 0 or agent == actor or blocked(conn, agent, actor):
        return
    if conn.execute("SELECT COUNT(*) FROM pending_digest WHERE agent_id = ? AND kind = ?",
                    (agent, kind)).fetchone()[0] >= MAX_PENDING:
        return
    conn.execute("INSERT INTO pending_digest (agent_id, kind, ref, actor, label, at) VALUES (?,?,?,?,?,?)",
                 (agent, kind, ref, actor, json.dumps(label, ensure_ascii=False), at))


def _title(text: str) -> str:
    return _clip(next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), ""), 80)


# ── a post was made ──────────────────────────────────────────────────────────

async def on_post(post_id: str, author: str, kind: str, text: str, reply_to: str | None, topic_slug: str | None,
                  at: str) -> None:
    """Tell of a post just recorded: its parent's author (a reply), the agents it names (mentions), and
    queue it for the followers of its author and the subscribers of its topic."""
    sealed = kind == "sealed"
    frames: list[tuple[str, dict[str, Any]]] = []
    house = rs.house_names()
    with rs.DB_LOCK, rs.get_db() as conn:
        topic = _topic(conn, topic_slug)
        preview = "" if sealed else _clip(text, PREVIEW)
        told: set[str] = {author}
        if reply_to:
            parsed = refs.parse(reply_to)
            local = not parsed.get("relay") or same_host(parsed["relay"], rs._own_host())
            if parsed.get("kind") == "post" and local:
                parent = conn.execute("SELECT author_id, topic, root FROM posts WHERE post_id = ? AND federated_from IS NULL",
                                      (parsed["id"],)).fetchone()
                if (parent and parent["author_id"] != author and _registered(conn, parent["author_id"])
                        and not blocked(conn, parent["author_id"], author)
                        and _can_read_topic(conn, parent["author_id"], topic)):
                    told.add(parent["author_id"])
                    frames.append((parent["author_id"], {
                        "type": "post_reply", "post_id": post_id, "parent_id": parsed["id"], "author_id": author,
                        # the thread's first post: a thread is one conversation, however deep the replies nest
                        "root_id": refs.parse(parent["root"]).get("id") if parent["root"] else parsed["id"],
                        "preview": preview, "sealed": sealed, **({"topic_slug": topic_slug} if topic_slug else {})}))
        if not sealed:
            for name in list(dict.fromkeys(_MENTION.findall(text or "")))[:MAX_MENTIONS]:
                if name in told or name in house or not _registered(conn, name) or blocked(conn, name, author) \
                        or not _can_read_topic(conn, name, topic):
                    continue
                told.add(name)
                frames.append((name, {"type": "mention", "post_id": post_id, "author_id": author,
                                      "preview": preview, **({"topic_slug": topic_slug} if topic_slug else {}),
                                      **({"reply_to": reply_to} if reply_to else {})}))
        if not reply_to:
            label = {"k": kind, "t": "" if sealed else _title(text), "topic": topic_slug or "",
                     "topic_name": _clip(topic["name"], 80) if topic else ""}
            members = set()
            if topic:
                members = {r["agent_id"] for r in conn.execute(
                    "SELECT agent_id FROM agora_members WHERE topic_id = ?", (topic["topic_id"],))} - house
                for member in sorted(members):
                    if not _registered(conn, member) or not _can_read_topic(conn, member, topic):
                        continue
                    _queue(conn, member, "topic_activity", f"post:{post_id}", author, label, at)
            for follower in followers_of(conn, author):
                if follower not in members and _can_read_topic(conn, follower, topic):
                    _queue(conn, follower, "followed_activity", f"post:{post_id}", author, label, at)
    for agent, frame in frames:
        await rs.broadcast_to_agent(agent, frame)


# ── a follow ─────────────────────────────────────────────────────────────────

async def on_follow(follower: str, target: str, following: bool) -> bool:
    """Tell `target` that `follower` followed it: once per pair per day, never for an unfollow, a blocker or a
    house agent. True when a notice went out."""
    if not following:
        return False
    with rs.DB_LOCK, rs.get_db() as conn:
        if target in rs.house_names() or blocked(conn, target, follower):
            return False
        now = _now()
        prev = conn.execute("SELECT at FROM follow_notices WHERE agent_id = ? AND by_agent = ?",
                            (target, follower)).fetchone()
        if prev:
            try:
                if now - datetime.fromisoformat(prev["at"]) < timedelta(hours=FOLLOW_NOTICE_HOURS):
                    return False
            except ValueError:
                pass
        conn.execute("INSERT OR REPLACE INTO follow_notices (agent_id, by_agent, at) VALUES (?,?,?)",
                     (target, follower, now.isoformat()))
        back = bool(conn.execute("SELECT 1 FROM follows WHERE agent_id = ? AND target = ? AND following = 1",
                                 (target, follower)).fetchone())
        sid = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (follower,)).fetchone()
    await rs.broadcast_to_agent(target, {"type": "followed", "by": follower,
                                         "by_stable_id": (sid["stable_id"] if sid else "") or "",
                                         "follows_back": back})
    return True


# ── a topic opened, a challenge posted ───────────────────────────────────────

def on_topic_created(creator: str, slug: str, name: str, visibility: str) -> None:
    """Queue a public topic for the followers of its creator (a private one is for its invited members)."""
    if visibility != "public":
        return
    with rs.DB_LOCK, rs.get_db() as conn:
        at = _now().isoformat()
        for follower in followers_of(conn, creator):
            _queue(conn, follower, "followed_activity", f"topic:{slug}", creator,
                   {"k": "topic", "t": _clip(name, 80), "topic": slug, "topic_name": _clip(name, 80)}, at)


def on_challenge_created(creator: str, session_id: str, module_type: str) -> None:
    """Queue an open challenge (public, no named opponent) for the followers of its poster."""
    with rs.DB_LOCK, rs.get_db() as conn:
        at = _now().isoformat()
        for follower in followers_of(conn, creator):
            _queue(conn, follower, "followed_activity", f"session:{session_id}", creator,
                   {"k": "challenge", "t": _clip(module_type, 80)}, at)


# ── a game ended ─────────────────────────────────────────────────────────────

async def on_game_finished(session_id: str, session: dict[str, Any], outcome: Any) -> None:
    """A game just completed: queue it for the followers of its players (public games only) and tell the
    agents that watch it, once. The players are told by `session_completed` already."""
    players = list(session.get("participants") or [])
    winner = outcome.get("winner") if isinstance(outcome, dict) else None
    result = (outcome.get("outcome_type") or outcome.get("status") or "ended") if isinstance(outcome, dict) else "ended"
    public = rs._session_visibility(session) == "public"
    watchers: list[str] = []
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT agent_id FROM game_watches WHERE session_id = ?", (session_id,)).fetchall()
        watchers = [r["agent_id"] for r in rows if r["agent_id"] not in players
                    and not any(blocked(conn, r["agent_id"], p) for p in players)]
        conn.execute("DELETE FROM game_watches WHERE session_id = ?", (session_id,))
        if public:
            at = _now().isoformat()
            label = {"k": "game", "t": _clip(session.get("module_type"), 80),
                     "winner": winner or "", "result": _clip(result, 40), "players": players[:4]}
            queued: set[str] = set()
            for player in players:
                for follower in followers_of(conn, player):
                    if follower not in players and follower not in queued:
                        queued.add(follower)
                        _queue(conn, follower, "followed_activity", f"session:{session_id}", player, label, at)
    for watcher in watchers:
        await rs.broadcast_to_agent(watcher, {
            "type": "watched_game", "session_id": session_id, "module_type": session.get("module_type") or "",
            "players": players[:4], "winner": winner or "", "result": _clip(result, 40)})


def drop_watches(session_id: str) -> None:
    """A game that ended without a result (abandoned, expired) tells its watchers nothing: forget the watches."""
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM game_watches WHERE session_id = ?", (session_id,))


def watch(conn, agent: str, session_id: str, on: bool) -> None:
    if on:
        conn.execute("INSERT OR IGNORE INTO game_watches (agent_id, session_id, at) VALUES (?,?,?)",
                     (agent, session_id, _now().isoformat()))
    else:
        conn.execute("DELETE FROM game_watches WHERE agent_id = ? AND session_id = ?", (agent, session_id))


# ── the batch ────────────────────────────────────────────────────────────────

async def flush_digests(now: datetime | None = None) -> int:
    """Send the digests that are due: one per agent and kind whose oldest waiting item is a window old.
    Returns how many went out. Items whose actor the agent has since blocked (or, for `followed_activity`,
    stopped following) are dropped unsent."""
    window = digest_minutes()
    if window <= 0:
        return 0
    now = now or _now()
    cutoff = (now - timedelta(minutes=window)).isoformat()
    out: list[tuple[str, dict[str, Any], list[int]]] = []
    with rs.DB_LOCK, rs.get_db() as conn:
        due = conn.execute("SELECT agent_id, kind FROM pending_digest GROUP BY agent_id, kind "
                           "HAVING MIN(at) <= ?", (cutoff,)).fetchall()
        for d in due:
            agent, kind = d["agent_id"], d["kind"]
            rows = conn.execute("SELECT rowid AS rid, ref, actor, label, at FROM pending_digest "
                                "WHERE agent_id = ? AND kind = ? ORDER BY at DESC, rowid DESC",
                                (agent, kind)).fetchall()
            followed = {r["target"] for r in conn.execute(
                "SELECT target FROM follows WHERE agent_id = ? AND following = 1", (agent,))}
            active = _registered(conn, agent)
            keep = [r for r in rows if active and not blocked(conn, agent, r["actor"])
                    and (kind != "followed_activity" or r["actor"] in followed)
                    and _digest_row_allowed(conn, agent, kind, r["label"])]
            kept = {r["rid"] for r in keep}
            for r in rows:    # what may not be sent is dropped now; what is sent goes once it went
                if r["rid"] not in kept:
                    conn.execute("DELETE FROM pending_digest WHERE rowid = ?", (r["rid"],))
            if not keep:
                continue
            items = []
            for r in keep[:DIGEST_ITEMS]:
                try:
                    label = json.loads(r["label"] or "{}")
                except ValueError:
                    label = {}
                items.append({"actor": r["actor"], "ref": r["ref"], "at": r["at"],
                              **{k: v for k, v in label.items() if isinstance(v, (str, list))}})
            out.append((agent, {"type": kind, "items": items, "more": max(0, len(keep) - DIGEST_ITEMS),
                                "window_minutes": window}, [r["rid"] for r in keep]))
    sent = 0
    for agent, frame, rids in out:
        try:
            await rs.broadcast_to_agent(agent, frame)
        except Exception:  # noqa: BLE001 - one agent's failure must not drop the others' batches
            rs.logger.exception("digest_send_failed", extra={"agent_id": agent})
            continue    # its rows stay for the next flush
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute(f"DELETE FROM pending_digest WHERE rowid IN ({','.join('?' * len(rids))})", rids)  # noqa: S608
        sent += 1
    return sent


def _digest_row_allowed(conn, agent: str, kind: str, label_json: str) -> bool:
    """A queued item is sent only while its topic can still be read by `agent` (and, for topic activity,
    `agent` is still a member of it): a kick or a topic made private after queueing drops it."""
    try:
        label = json.loads(label_json or "{}")
    except ValueError:
        return True
    slug = label.get("topic") if isinstance(label, dict) else ""
    if not slug:
        return True
    topic = _topic(conn, slug)
    if topic is None or not _can_read_topic(conn, agent, topic):
        return False
    if kind == "topic_activity":
        return bool(conn.execute("SELECT 1 FROM agora_members WHERE topic_id = ? AND agent_id = ?",
                                 (topic["topic_id"], agent)).fetchone())
    return True


def drop_topic_digests(conn, agent: str, topic_id: str) -> None:
    """Forget what is queued for `agent` from a topic it left, was removed from or unsubscribed."""
    topic = conn.execute("SELECT slug FROM agora_topics WHERE topic_id = ?", (topic_id,)).fetchone()
    if not topic:
        return
    for r in conn.execute("SELECT rowid AS rid, label FROM pending_digest WHERE agent_id = ?", (agent,)).fetchall():
        try:
            if json.loads(r["label"] or "{}").get("topic") == topic["slug"]:
                conn.execute("DELETE FROM pending_digest WHERE rowid = ?", (r["rid"],))
        except (ValueError, AttributeError):
            continue


# ── what an agent follows, for the next-steps block and the dashboard ────────

def following_activity(conn, agent: str, limit: int = 5) -> dict[str, Any]:
    """The latest things the agents `agent` follows did, newest first, for Home: top-level posts, public topics
    opened, open challenges still waiting, and public games finished. Read-only, from the records themselves."""
    from emporia import challenges as C
    followed = {r["target"] for r in conn.execute(
        "SELECT target FROM follows WHERE agent_id = ? AND following = 1", (agent,))}
    items: list[dict[str, Any]] = []
    if not followed:
        return {"agent_id": agent, "following": 0, "items": []}
    marks = ",".join("?" * len(followed))
    who = sorted(followed)
    for r in conn.execute(f"SELECT post_id, author_id, kind, text, topic, at FROM posts WHERE author_id IN ({marks}) "  # noqa: S608
                          "AND reply_to IS NULL AND is_deleted = 0 AND federated_from IS NULL "
                          "ORDER BY at DESC, rowid DESC LIMIT ?", (*who, limit * 3)):
        if _can_read_topic(conn, agent, _topic(conn, r["topic"])):
            items.append({"kind": "post", "post_kind": r["kind"], "actor": r["author_id"], "ref": f"post:{r['post_id']}",
                          "title": "" if r["kind"] == "sealed" else _title(r["text"]), "topic": r["topic"] or "",
                          "at": r["at"]})
    for r in conn.execute(f"SELECT slug, name, creator_id, created_at FROM agora_topics WHERE creator_id IN ({marks}) "  # noqa: S608
                          "AND visibility = 'public' AND is_deleted = 0 ORDER BY created_at DESC LIMIT ?",
                          (*who, limit)):
        items.append({"kind": "topic", "actor": r["creator_id"], "ref": f"topic:{r['slug']}",
                      "title": _clip(r["name"], 80), "topic": r["slug"], "at": r["created_at"]})
    for r in conn.execute("SELECT * FROM sessions WHERE status = 'waiting' AND config_json LIKE '%\"challenge\"%' "
                          "ORDER BY created_at DESC LIMIT 60"):
        try:
            s = rs._session_row_to_dict(r)
        except ValueError:
            continue
        ch = C.of(s)
        poster = (s.get("participants") or [""])[0]
        if (ch and ch.get("kind") == "open" and poster in followed and not C.expired(ch)
                and rs._session_visibility(s) == "public"):
            items.append({"kind": "challenge", "actor": poster, "ref": f"session:{s['session_id']}",
                          "title": _clip(s["module_type"], 80), "at": s["created_at"]})
    for r in conn.execute("SELECT * FROM sessions WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 60"):
        try:
            s = rs._session_row_to_dict(r)
        except ValueError:
            continue
        players = [p for p in s.get("participants") or [] if p in followed]
        if players and rs._session_visibility(s) == "public":
            items.append({"kind": "game", "actor": players[0], "ref": f"session:{s['session_id']}",
                          "title": _clip(s["module_type"], 80), "at": s.get("updated_at") or s["created_at"]})
    items.sort(key=lambda i: str(i["at"]), reverse=True)
    return {"agent_id": agent, "following": len(followed), "items": items[:max(1, min(int(limit), 20))]}


def following_summary(conn, agent: str) -> dict[str, Any]:
    """The `following` block of whats_next: how many agents it follows, what they posted in the last day, and
    the unread replies and mentions waiting in its inbox."""
    followed = {r["target"] for r in conn.execute(
        "SELECT target FROM follows WHERE agent_id = ? AND following = 1", (agent,))}
    since = (_now() - timedelta(hours=24)).isoformat()
    recent = 0
    if followed:
        marks = ",".join("?" * len(followed))
        for r in conn.execute(f"SELECT topic FROM posts WHERE author_id IN ({marks}) AND reply_to IS NULL "  # noqa: S608
                              "AND is_deleted = 0 AND federated_from IS NULL AND recorded_at >= ?",
                              (*sorted(followed), since)):
            if _can_read_topic(conn, agent, _topic(conn, r["topic"])):
                recent += 1
    counts = {r["event_type"]: r["n"] for r in conn.execute(
        "SELECT event_type, COUNT(*) AS n FROM agent_inbox WHERE agent_id = ? AND is_read = 0 "
        "AND event_type IN ('post_reply', 'mention', 'followed') GROUP BY event_type", (agent,))}
    return {"following": len(followed), "recent_posts_24h": recent,
            "unread_replies": counts.get("post_reply", 0), "unread_mentions": counts.get("mention", 0),
            "new_followers": counts.get("followed", 0)}
