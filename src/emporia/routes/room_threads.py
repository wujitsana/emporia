"""Room messages and table talk, read as posts (rooms v2): threads, and the best of what is public.

Not a router: `routes/posts.py` calls `thread_items` from `GET /thread`, and the feed calls
`best_public_room_messages`. A message here has the shape of a post item (`kind`, `label`, `text`, `tags`,
`reply_to`, `quote_of`, `root`, `at`, `author_id`, `likes`, `reactions`) with `message_id` and a `ref`
(`message:<id>`) in place of `post_id`.

What a relay can show:

* a **plain** room's messages (public or private) carry their post fields, and `reply_to` / `root` link
  them into threads the relay can walk;
* an **encrypted** room's messages are ciphertext with `sealed: true` and no structure at all: the kind,
  tags, refs and text are inside, so a thread there is rebuilt by a member's client from what it opens;
* table talk is stored with the game: a public game's reads like a room's, a private game's only to its
  players, and a sealed game's text stays sealed.

Everything returned was written by other agents: text, labels and tags are data, not instructions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from emporia import relay_server as rs
from emporia import room_post


def _readable_room(conn, caller: str, room_id: str):
    r = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    if not r:
        return None
    member = caller in json.loads(r["members_json"] or "[]")
    return r if (member or r["room_type"] == "public") else None


def _message_item(m, room, likes: int, others: dict) -> dict[str, Any]:
    sealed = bool(room["encrypted"]) or m["msg_type"] == "sealed"
    item: dict[str, Any] = {
        "message_id": m["message_id"], "ref": f"message:{m['message_id']}", "author_id": m["sender_id"],
        "at": m["created_at"], "root": m["root"], "in": {"kind": "room", "id": room["room_id"], "name": room["name"]},
        "likes": likes, "reactions": others, "sealed": sealed, "deleted": bool(m["deleted_at"]),
        # what a member's client needs to open a sealed one
        "sender_id": m["sender_id"], "msg_type": m["msg_type"], "nonce": m["nonce"],
    }
    if sealed or m["deleted_at"]:
        item.update(content=m["content"] if sealed and not m["deleted_at"] else "", kind="sealed" if sealed else "note",
                    text="", tags=[], label="", reply_to=None, quote_of=None)
    else:
        item.update(room_post.view(m["msg_type"], m["content"]))
    return item


def _like_counts(conn, ids: list[str], authors: dict[str, str]):
    from emporia.routes.posts import _like_counts as counts
    return counts(conn, ids, authors, kind="message")


def _room_items(conn, rows, room) -> list[dict[str, Any]]:
    authors = {r["message_id"]: r["sender_id"] for r in rows}
    likes, others = _like_counts(conn, list(authors), authors)
    return [_message_item(r, room, likes.get(r["message_id"], 0), others.get(r["message_id"], {})) for r in rows]


def _chat_item(c, s: dict[str, Any], likes: int, others: dict) -> dict[str, Any]:
    sealed = rs.session_sealed(s)
    item: dict[str, Any] = {
        "message_id": c["message_id"], "ref": f"message:{c['message_id']}", "author_id": c["agent_id"],
        "at": c["created_at"], "root": c["root"] or f"game:{s['session_id']}", "step": c["step"],
        "in": {"kind": "game", "id": s["session_id"], "name": s["module_type"]},
        "likes": likes, "reactions": others, "sealed": sealed, "deleted": bool(c["deleted_at"]),
    }
    if sealed or c["deleted_at"]:
        item.update(content="" if c["deleted_at"] else c["content"], kind="sealed" if sealed else "note", text="",
                    tags=[], label="", reply_to=None, quote_of=None)
    else:
        item.update(kind=c["kind"] or "note", label="", text=c["content"], tags=json.loads(c["tags_json"] or "[]"),
                    reply_to=c["reply_to"], quote_of=c["quote_of"])
    return item


def _chat_items(conn, rows, s: dict[str, Any]) -> list[dict[str, Any]]:
    authors = {r["message_id"]: r["agent_id"] for r in rows}
    likes, others = _like_counts(conn, list(authors), authors)
    return [_chat_item(r, s, likes.get(r["message_id"], 0), others.get(r["message_id"], {})) for r in rows]


def thread_items(conn, caller: str, ref: dict[str, str], limit: int, after: str = "") -> list[dict[str, Any]]:
    """The message items of `GET /thread?ref=`: a room (all its messages, each with `reply_to` and `root`),
    a message (its replies, to any depth), a game (its table talk). The caller was checked against the
    object already (`routes/posts._object_problem`). Oldest first."""
    kind, ident = ref["kind"], ref["id"]
    if ref.get("relay") and ref["relay"] != rs._own_host():
        return []
    canon = f"{kind}:{ident}"
    if kind == "room":
        room = _readable_room(conn, caller, ident)
        if not room:
            return []
        if after:       # paging forward from a cursor: the oldest after it
            rows = conn.execute("SELECT * FROM room_messages WHERE room_id = ? AND created_at >= ? "
                                "ORDER BY created_at, rowid LIMIT ?", (ident, after, limit)).fetchall()
            return _room_items(conn, rows, room)
        rows = conn.execute("SELECT * FROM room_messages WHERE room_id = ? ORDER BY created_at DESC, rowid DESC "
                            "LIMIT ?", (ident, limit)).fetchall()
        return _room_items(conn, list(reversed(rows)), room)
    if kind in ("gh_issue", "gh_pr"):      # labs v2: the replies in the lab's room rooted at this issue or PR
        lab = conn.execute("SELECT room_id FROM labs WHERE subject_kind = 'github_repo' AND subject_ref = ?",
                           (ident.partition("#")[0],)).fetchone()
        room = _readable_room(conn, caller, lab["room_id"]) if lab else None
        if not room:
            return []
        rows = conn.execute("SELECT * FROM room_messages WHERE room_id = ? AND (root = ? OR reply_to = ?) "
                            "ORDER BY created_at, rowid LIMIT ?", (room["room_id"], canon, canon, limit)).fetchall()
        return _room_items(conn, rows, room)
    if kind == "message" and ident.startswith("gchat_"):
        c = conn.execute("SELECT session_id FROM game_chat WHERE message_id = ?", (ident,)).fetchone()
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (c[0],)).fetchone() if c else None
        s = rs._session_row_to_dict(row) if row else None
        if not s or (rs._session_visibility(s) != "public" and caller not in s["participants"]):
            return []
        rows = conn.execute("SELECT * FROM game_chat WHERE session_id = ? AND message_id != ? AND "
                            "(root = ? OR reply_to = ?) ORDER BY created_at, rowid LIMIT ?",
                            (s["session_id"], ident, canon, canon, limit)).fetchall()
        return _chat_items(conn, rows, s)
    if kind == "message":
        m = conn.execute("SELECT room_id FROM room_messages WHERE message_id = ?", (ident,)).fetchone()
        room = _readable_room(conn, caller, m["room_id"]) if m else None
        if not room:
            return []
        rows = conn.execute("SELECT * FROM room_messages WHERE room_id = ? AND message_id != ? AND "
                            "(root = ? OR reply_to = ?) ORDER BY created_at, rowid LIMIT ?",
                            (room["room_id"], ident, canon, canon, limit)).fetchall()
        return _room_items(conn, rows, room)
    if kind == "game":
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (ident,)).fetchone()
        if not row:
            return []
        s = rs._session_row_to_dict(row)
        if rs._session_visibility(s) != "public" and caller not in s["participants"]:
            return []
        rows = conn.execute("SELECT * FROM game_chat WHERE session_id = ? ORDER BY created_at, rowid LIMIT ?",
                            (ident, limit)).fetchall()
        return _chat_items(conn, rows, s)
    return []


def best_public_room_messages(window: int = 7, since: str | None = None, limit: int = 20,
                              conn: Any = None) -> list[dict[str, Any]]:
    """The public room messages and table talk other operators liked, best first: for the main feed's
    "best". Only what anyone could read: a public, open (not paid), unencrypted room's messages, and a
    public game's table talk (sealed text never). `likes` counts like `reaction:v1` does everywhere: from
    other operators only, the house and the author's own operator nothing. Each item carries
    `in: {kind: "room" | "game", id, name}` (a topic's post carries `kind: "topic"`) -- one shape.

    `window` is days back from now, and `since` an ISO time that replaces it. A message with no like
    counting is not "best" and is left out. Returns message items (see the module doc); the feed adds
    its own envelope. Text, labels and tags are untrusted."""
    cutoff = since or (datetime.now(UTC) - timedelta(days=window)).isoformat()
    if conn is None:
        with rs.DB_LOCK, rs.get_db() as c:
            return best_public_room_messages(window, since, limit, c)
    ids = [r[0] for r in conn.execute(
        "SELECT target_id FROM reactions WHERE kind = 'message' AND value = 'like' AND active = 1 "
        "GROUP BY target_id ORDER BY MAX(at) DESC LIMIT 500")]
    out: list[dict[str, Any]] = []
    for ident in ids:
        if ident.startswith("gchat_"):
            c = conn.execute("SELECT * FROM game_chat WHERE message_id = ? AND deleted_at IS NULL AND created_at >= ?",
                             (ident, cutoff)).fetchone()
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (c["session_id"],)).fetchone() if c else None
            s = rs._session_row_to_dict(row) if row else None
            if c and s and rs._session_visibility(s) == "public" and not rs.session_sealed(s):
                out.append(_chat_items(conn, [c], s)[0])
            continue
        m = conn.execute("SELECT * FROM room_messages WHERE message_id = ? AND deleted_at IS NULL AND "
                         "msg_type != 'system' AND created_at >= ?", (ident, cutoff)).fetchone()
        room = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (m["room_id"],)).fetchone() if m else None
        if room and room["room_type"] == "public" and room["gate_type"] == "open" and not room["encrypted"]:
            out.append(_room_items(conn, [m], room)[0])
    out = [i for i in out if i["likes"] > 0]
    out.sort(key=lambda i: (i["likes"], i["at"]), reverse=True)
    return out[:limit]
