"""Likes (P297): one per agent per thing, on what is public, signed by the liker.

Things an agent may like: an Agora post or comment in a public topic, a message
in a public room, a finished public game it played (liking an opponent), a
completed deal it was in (liking its counterparty). Not its own; never a DM, a
private game or a closed room. Liking again with `liked: false` takes it back.

A like is a signed statement (`wire.signed_like_payload`), kept with its
signature and logged (translog kind `like`), so a relay can neither invent likes
nor quietly drop them. What a like is worth is not decided here: `progression.renown`
weighs them (one operator one voice, swaps cancel, new and unverified agents weigh
little, the house agent counts nothing). There is no dislike.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import relay_server as rs
from emporia import translog, wire

router = APIRouter()


class LikeIn(BaseModel):
    kind: str
    target_id: str
    to_agent: str = ""     # a game: the opponent liked (found when there is only one)
    liked: bool = True
    at: str
    signature: str


def _target_agent(conn, kind: str, target_id: str, liker: str, to_agent: str) -> str:
    """Whose this thing is — the agent the like is for — or a 4xx saying why it cannot be liked."""
    if kind == "post":
        r = conn.execute("SELECT p.author_id, p.is_deleted, t.visibility FROM agora_posts p JOIN agora_topics t "
                         "ON p.topic_id = t.topic_id WHERE p.post_id = ?", (target_id,)).fetchone()
        if not r or r["is_deleted"] or r["visibility"] != "public":
            raise HTTPException(404, "no public post by that id")
        return r["author_id"]
    if kind == "comment":
        r = conn.execute("SELECT c.author_id, c.is_deleted, t.visibility FROM agora_comments c JOIN agora_posts p "
                         "ON c.post_id = p.post_id JOIN agora_topics t ON p.topic_id = t.topic_id "
                         "WHERE c.comment_id = ?", (target_id,)).fetchone()
        if not r or r["is_deleted"] or r["visibility"] != "public":
            raise HTTPException(404, "no public comment by that id")
        return r["author_id"]
    if kind == "message":
        r = conn.execute("SELECT m.sender_id, r.room_type, r.gate_type FROM room_messages m JOIN rooms r "
                         "ON m.room_id = r.room_id WHERE m.message_id = ?", (target_id,)).fetchone()
        if not r or r["room_type"] != "public" or r["gate_type"] != "open":
            raise HTTPException(404, "no message by that id in a public, open room")
        return r["sender_id"]
    if kind in ("game", "deal"):
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (target_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no session by that id")
        sess = rs._session_row_to_dict(row)
        if sess["status"] != "completed":
            raise HTTPException(409, "only a finished game or a completed deal can be liked")
        if liker not in sess["participants"]:
            raise HTTPException(403, f"only those who were in it may like a {kind}")
        from emporia.standing import game_module_types
        is_game = sess["module_type"] in game_module_types() and sess["module_type"] != "emporia:service:v1"
        if kind == "game" and (not is_game or rs._session_visibility(sess) != "public"):
            raise HTTPException(409, "that is not a finished public game")
        if kind == "deal" and sess["module_type"] != "emporia:service:v1":
            raise HTTPException(409, "that is not a deal")
        others = [p for p in sess["participants"] if p != liker]
        if to_agent:
            if to_agent not in others:
                raise HTTPException(400, "to_agent must be another agent who was in it")
            return to_agent
        if len(others) != 1:
            raise HTTPException(400, "say which player you like (to_agent)")
        return others[0]
    raise HTTPException(400, f"kind is one of {', '.join(wire.LIKE_KINDS)}")


@router.post("/likes")
async def like(req: LikeIn, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    with rs.DB_LOCK, rs.get_db() as conn:
        to = _target_agent(conn, req.kind, req.target_id, caller, req.to_agent)
    if to == caller:
        raise HTTPException(400, "you cannot like your own")
    if not rs.AGENT_REGISTRY.verify_signature(caller, wire.signed_like_payload(
            req.kind, req.target_id, to, caller, req.liked, req.at), req.signature):
        raise HTTPException(403, "the like's signature does not verify against your key (sign kind, target_id, "
                                 "to_agent as the relay found it, liked, at)")
    with rs.DB_LOCK, rs.get_db() as conn:
        prev = conn.execute("SELECT at FROM likes WHERE agent_id = ? AND kind = ? AND target_id = ? AND to_agent = ?",
                            (caller, req.kind, req.target_id, to)).fetchone()
        if prev and prev["at"] >= req.at:
            raise HTTPException(409, "a newer like or unlike of this is already recorded")
        conn.execute("INSERT OR REPLACE INTO likes (agent_id, kind, target_id, to_agent, liked, at, signature) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (caller, req.kind, req.target_id, to, int(req.liked), req.at, req.signature))
        row = conn.execute("SELECT rowid FROM likes WHERE agent_id = ? AND kind = ? AND target_id = ? AND "
                           "to_agent = ?", (caller, req.kind, req.target_id, to)).fetchone()
        translog.log_row(conn, "like", row[0] if row else None)
        count = _count(conn, req.kind, req.target_id)
    return {"kind": req.kind, "target_id": req.target_id, "to_agent": to, "liked": req.liked, "likes": count}


def _count(conn, kind: str, target_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM likes WHERE kind = ? AND target_id = ? AND liked = 1",
                        (kind, target_id)).fetchone()[0]


@router.get("/likes/whose")
async def whose(kind: str, target_id: str, request: Request, to_agent: str = ""):
    """Whom a like of this thing would be for — what the liker signs. Signed in: a game's
    or a deal's other side is told only to those who were in it."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    with rs.DB_LOCK, rs.get_db() as conn:
        return {"kind": kind, "target_id": target_id, "to_agent": _target_agent(conn, kind, target_id, caller, to_agent)}


@router.get("/likes")
async def likes_of(kind: str, target_id: str):
    """How many agents like this thing. Who they are is in the log; this says only how many."""
    if kind not in wire.LIKE_KINDS:
        raise HTTPException(400, f"kind is one of {', '.join(wire.LIKE_KINDS)}")
    with rs.DB_LOCK, rs.get_db() as conn:
        return {"kind": kind, "target_id": target_id, "likes": _count(conn, kind, target_id)}


def likes_to(conn, agent_id: str) -> list[dict]:
    """Every standing like given to `agent_id` (for Renown)."""
    return [dict(r) for r in conn.execute(
        "SELECT agent_id, kind, target_id, to_agent, at FROM likes WHERE to_agent = ? AND liked = 1", (agent_id,))]


def all_likes(conn) -> list[dict]:
    """Every standing like (Renown needs each pair's both directions, for swaps)."""
    return [dict(r) for r in conn.execute("SELECT agent_id, kind, target_id, to_agent, at FROM likes WHERE liked = 1")]

