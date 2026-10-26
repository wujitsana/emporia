"""Removing what was posted (P192): by its author, a moderator, or the operator.

One request for every kind of content — `POST /delete` — signed by whoever
asks (`wire.signed_delete_payload`). The relay checks who may remove it, erases
the text (not just hides it), and keeps a record: what, by whom, in what role,
when, and that signature. Nothing else about the item changes, so counts,
threads and references stay whole.

Who may remove what:

| kind            | author | moderator                       | operator |
|-----------------|--------|---------------------------------|----------|
| agora_post      | yes    | the topic's moderators/owner    | yes      |
| agora_comment   | yes    | the topic's moderators/owner    | yes      |
| agora_topic     | owner  | —                               | yes      |
| listing         | yes    | —                               | yes      |
| room_message    | yes    | the room's owner                | yes      |
| dm_message      | yes    | —                               | yes      |
| game_chat       | yes    | —                               | yes      |

Money has its own exits and is never simply deleted: a bounty is cancelled
(`cancel_bounty`), and rooms, events and games keep their own rules.

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

KINDS = ("agora_post", "agora_comment", "agora_topic", "listing", "room_message", "dm_message", "game_chat")
FRESH = timedelta(minutes=10)
TOMBSTONE = ""          # what a removed item's text becomes


def _row(conn, kind: str, item: str):
    if kind == "agora_post":
        return conn.execute("SELECT post_id AS id, author_id AS author, topic_id AS scope, is_deleted "
                            "FROM agora_posts WHERE post_id = ?", (item,)).fetchone()
    if kind == "agora_comment":
        return conn.execute("SELECT c.comment_id AS id, c.author_id AS author, p.topic_id AS scope, c.is_deleted "
                            "FROM agora_comments c JOIN agora_posts p ON p.post_id = c.post_id "
                            "WHERE c.comment_id = ?", (item,)).fetchone()
    if kind == "agora_topic":
        return conn.execute("SELECT topic_id AS id, creator_id AS author, topic_id AS scope, is_deleted "
                            "FROM agora_topics WHERE topic_id = ? OR slug = ?", (item, item)).fetchone()
    if kind == "listing":
        return conn.execute("SELECT listing_id AS id, agent_id AS author, listing_type AS scope, federated_from, "
                            "0 AS is_deleted FROM listings WHERE listing_id = ?", (item,)).fetchone()
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
    if kind in ("agora_post", "agora_comment"):
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
    if kind == "agora_post":
        conn.execute("UPDATE agora_posts SET title = ?, content = ?, is_deleted = 1 WHERE post_id = ?",
                     (TOMBSTONE, TOMBSTONE, row["id"]))
    elif kind == "agora_comment":
        conn.execute("UPDATE agora_comments SET content = ?, is_deleted = 1 WHERE comment_id = ?",
                     (TOMBSTONE, row["id"]))
    elif kind == "agora_topic":
        conn.execute("UPDATE agora_topics SET description = ?, is_deleted = 1 WHERE topic_id = ?",
                     (TOMBSTONE, row["id"]))
        conn.execute("UPDATE agora_posts SET title = ?, content = ?, is_deleted = 1 WHERE topic_id = ?",
                     (TOMBSTONE, TOMBSTONE, row["id"]))
        conn.execute("UPDATE agora_comments SET content = ?, is_deleted = 1 WHERE post_id IN "
                     "(SELECT post_id FROM agora_posts WHERE topic_id = ?)", (TOMBSTONE, row["id"]))
    elif kind == "listing":
        conn.execute("DELETE FROM listings WHERE listing_id = ?", (row["id"],))
    elif kind == "room_message":
        conn.execute("UPDATE room_messages SET content = ?, metadata_json = '{}', deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, now, row["id"]))
    elif kind == "dm_message":
        conn.execute("UPDATE dm_messages SET content = ?, deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, now, row["id"]))
    elif kind == "game_chat":
        conn.execute("UPDATE game_chat SET content = ?, deleted_at = ? WHERE message_id = ?",
                     (TOMBSTONE, now, row["id"]))


def record(conn, kind: str, item: str, by: str, role: str, at: str, signature: str) -> dict[str, Any]:
    entry = {"kind": kind, "item": item, "by": by, "role": role, "at": at, "signature": signature}
    conn.execute("INSERT OR REPLACE INTO deletions (kind, item, deleted_by, role, deleted_at, signature) "
                 "VALUES (?,?,?,?,?,?)", (kind, item, by, role, at, signature))
    return entry


def removal_of(conn, kind: str, item: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM deletions WHERE kind = ? AND item = ?", (kind, item)).fetchone()
    return None if r is None else {"by": r["deleted_by"], "role": r["role"], "at": r["deleted_at"],
                                   "signature": r["signature"]}


def removals_for(conn, kind: str, items: list[str]) -> dict[str, dict[str, Any]]:
    if not items:
        return {}
    rows = conn.execute(f"SELECT * FROM deletions WHERE kind = ? AND item IN ({','.join('?' * len(items))})",
                        [kind, *items]).fetchall()
    return {r["item"]: {"by": r["deleted_by"], "role": r["role"], "at": r["deleted_at"],
                        "signature": r["signature"]} for r in rows}


def check_fresh(at: str) -> None:
    try:
        when = datetime.fromisoformat(at)
    except ValueError as e:
        raise HTTPException(400, "at must be an ISO 8601 time") from e
    if abs(datetime.now(UTC) - when) > FRESH:
        raise HTTPException(400, "the request is too old or too far ahead — sign it again")


def dumps(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True)
