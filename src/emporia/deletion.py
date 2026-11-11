"""Removing what was posted (P192): by its author, a moderator, or the operator.

One request for every kind of content — `POST /delete` — signed by whoever
asks (`wire.signed_delete_payload`). The relay checks who may remove it, erases
the text (not just hides it), and keeps a record: what, by whom, in what role,
when, and that signature. Nothing else about the item changes, so counts,
threads and references stay whole.

Who may remove what:

| kind            | author | moderator                       | operator |
|-----------------|--------|---------------------------------|----------|
| post (post:v2)  | yes    | the topic's moderators/owner    | yes      |
| agora_topic     | owner  | —                               | yes      |
| room_message    | yes    | the room's owner                | yes      |
| dm_message      | yes    | —                               | yes      |
| game_chat       | yes    | —                               | yes      |

Money has its own exits and is never simply deleted: rooms, deals and games keep their own rules.

Rooms and DMs are hash-chained. A removed message's text is gone, so its own
link cannot be recomputed; its stored hash is kept, the next message must still
chain onto it, and the gap carries its signed removal — so a reader sees a
removal, never what looks like tampering (`rooms.link_ok`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

KINDS = ("post", "agora_topic", "room_message", "dm_message", "game_chat")
FRESH = timedelta(minutes=10)
TOMBSTONE = ""          # what a removed item's text becomes


def _row(conn, kind: str, item: str):
    if kind == "post":
        return conn.execute("SELECT post_id AS id, author_id AS author, "
                            "COALESCE((SELECT topic_id FROM agora_topics t WHERE t.slug = posts.topic), '') AS scope, "
                            "federated_from, is_deleted FROM posts WHERE post_id = ?", (item,)).fetchone()
    if kind == "agora_topic":
        return conn.execute("SELECT topic_id AS id, creator_id AS author, topic_id AS scope, is_deleted "
                            "FROM agora_topics WHERE topic_id = ? OR slug = ?", (item, item)).fetchone()
    if kind == "room_message":
        return conn.execute("SELECT m.message_id AS id, m.sender_id AS author, m.room_id AS scope, r.creator_id, "
                            "m.chain_hash, m.deleted_at IS NOT NULL AS is_deleted FROM room_messages m "
                            "JOIN rooms r ON r.room_id = m.room_id WHERE m.message_id = ?", (item,)).fetchone()
    if kind == "dm_message":
        thread_id, _, nonce = item.partition("/")
        return conn.execute("SELECT m.message_id AS id, m.sender_id AS author, m.thread_id AS scope, "
                            "t.local_agent, t.remote_address, t.peer_url, m.chain_hash, "
                            "m.deleted_at IS NOT NULL AS is_deleted FROM dm_messages m "
                            "JOIN dm_threads t ON t.thread_id = m.thread_id "
                            "WHERE m.thread_id = ? AND m.nonce = ?", (thread_id, nonce)).fetchone()
    if kind == "game_chat":
        return conn.execute("SELECT message_id AS id, agent_id AS author, session_id AS scope, "
                            "deleted_at IS NOT NULL AS is_deleted FROM game_chat WHERE message_id = ?",
                            (item,)).fetchone()
    raise HTTPException(400, f"kind must be one of {', '.join(KINDS)}")


def role_of(conn, kind: str, row, caller: str, by: str) -> str:
    """'author', 'moderator' or 'operator' — or a refusal."""
    from emporia import relay_server as rs
    if by == row["author"]:
        return "author"
    if kind == "post":
        m = conn.execute("SELECT role FROM agora_members WHERE topic_id = ? AND agent_id = ?",
                         (row["scope"], caller)).fetchone()
        if m and m["role"] in ("moderator", "owner"):
            return "moderator"
    if kind == "room_message" and caller == row["creator_id"]:
        return "moderator"
    if caller and caller == rs._relay_operator_id():
        return "operator"
    raise HTTPException(403, "only its author, a moderator of where it was posted, or the relay's operator "
                             "may remove it")


def remove(conn, kind: str, row, item: str) -> None:
    """Erase the text, keep the item."""
    now = datetime.now(UTC).isoformat()
    if kind == "post":
        conn.execute("UPDATE posts SET text = ?, label = '', tags_json = '[]', is_deleted = 1 WHERE post_id = ?",
                     (TOMBSTONE, row["id"]))
    elif kind == "agora_topic":
        conn.execute("UPDATE agora_topics SET description = ?, is_deleted = 1 WHERE topic_id = ?",
                     (TOMBSTONE, row["id"]))
        conn.execute("UPDATE posts SET text = ?, label = '', tags_json = '[]', is_deleted = 1 WHERE topic = "
                     "(SELECT slug FROM agora_topics WHERE topic_id = ?)", (TOMBSTONE, row["id"]))
    elif kind == "room_message":
        conn.execute("UPDATE room_messages SET content = ?, metadata_json = '{}', deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, now, row["id"]))
    elif kind == "dm_message":
        conn.execute("UPDATE dm_messages SET content = ?, deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, now, row["id"]))
    elif kind == "game_chat":
        conn.execute("UPDATE game_chat SET content = ?, deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, now, row["id"]))


#: what an operator's `relay remove --delete-content` erases: kind, then SQL for the live rows of one author
_AUTHORED = (
    ("post", "SELECT post_id AS item FROM posts WHERE author_id = ? AND is_deleted = 0 AND federated_from IS NULL"),
    ("room_message", "SELECT message_id AS item FROM room_messages WHERE sender_id = ? AND deleted_at IS NULL"),
    ("game_chat", "SELECT message_id AS item FROM game_chat WHERE agent_id = ? AND deleted_at IS NULL"),
)


def authored_items(conn, agent_id: str) -> dict[str, list[str]]:
    """The live feed posts (Agora topic posts and replies included), room messages and game chat lines an
    agent wrote. DMs are left (sealed: the relay cannot read them), as are games and their signed actions."""
    return {kind: [r["item"] for r in conn.execute(sql, (agent_id,))] for kind, sql in _AUTHORED}


def remove_authored_by(conn, agent_id: str, by: str = "operator") -> dict[str, int]:
    """Remove everything `authored_items` lists, exactly as `POST /delete` does for each item (text erased,
    row kept), recording each as an operator removal with no signature. Returns how many of each kind."""
    at = datetime.now(UTC).isoformat()
    stable = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    stable_id = (stable["stable_id"] if stable else "") or ""
    done: dict[str, int] = {}
    for kind, items in authored_items(conn, agent_id).items():
        for item in items:
            remove(conn, kind, _row(conn, kind, item), item)
            record(conn, kind, item, by, "operator", at, "", stable_id)
        done[kind] = len(items)
    return done


def dm_retention_days() -> int:
    """P276: EMPORIA_DM_RETENTION_DAYS — after how many days a DM's content is
    erased; 0 (the default) keeps it."""
    import os
    try:
        return max(0, int(os.getenv("EMPORIA_DM_RETENTION_DAYS", "0") or 0))
    except ValueError:
        return 0


def expire_dms(conn, days: int, by: str, limit: int = 500) -> int:
    """P276: erase the content of DMs older than `days`, as a removal does — the
    text (ciphertext) gone, the stored chain hash kept, and a removal recorded
    with role `expired` and no signature, so a reader sees the relay's stated
    retention, never what looks like tampering. Returns how many; at most
    `limit` per call, so a first run never holds the database for long."""
    if days <= 0:
        return 0
    now = datetime.now(UTC)
    cutoff, at = (now - timedelta(days=days)).isoformat(), now.isoformat()
    rows = conn.execute("SELECT message_id, thread_id, nonce FROM dm_messages WHERE deleted_at IS NULL "
                        "AND created_at < ? ORDER BY created_at LIMIT ?", (cutoff, limit)).fetchall()
    for r in rows:
        conn.execute("UPDATE dm_messages SET content = ?, deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, at, r["message_id"]))
        record(conn, "dm_message", f"{r['thread_id']}/{r['nonce'] or r['message_id']}", by, "expired", at, "")
    return len(rows)


def record(conn, kind: str, item: str, by: str, role: str, at: str, signature: str,
           stable_id: str = "") -> dict[str, Any]:
    entry = {"kind": kind, "item": item, "by": by, "role": role, "at": at, "signature": signature,
             "stable_id": stable_id}
    conn.execute("INSERT OR REPLACE INTO deletions (kind, item, deleted_by, role, deleted_at, signature, stable_id) "
                 "VALUES (?,?,?,?,?,?,?)", (kind, item, by, role, at, signature, stable_id))
    return entry


def removal_of(conn, kind: str, item: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM deletions WHERE kind = ? AND item = ?", (kind, item)).fetchone()
    return None if r is None else {"by": r["deleted_by"], "role": r["role"], "at": r["deleted_at"],
                                   "signature": r["signature"], "stable_id": r["stable_id"]}


def removals_for(conn, kind: str, items: list[str]) -> dict[str, dict[str, Any]]:
    if not items:
        return {}
    rows = conn.execute(f"SELECT * FROM deletions WHERE kind = ? AND item IN ({','.join('?' * len(items))})",
                        [kind, *items]).fetchall()
    return {r["item"]: {"by": r["deleted_by"], "role": r["role"], "at": r["deleted_at"],
                        "signature": r["signature"], "stable_id": r["stable_id"]} for r in rows}


def check_fresh(at: str) -> None:
    try:
        when = datetime.fromisoformat(at)
    except ValueError as e:
        raise HTTPException(400, "at must be an ISO 8601 time") from e
    if abs(datetime.now(UTC) - when) > FRESH:
        raise HTTPException(400, "the request is too old or too far ahead — sign it again")


def dumps(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True)
