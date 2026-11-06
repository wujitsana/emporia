"""Adopted ideas (launch additions): the operator's signed verdict on a post in the ideas topic.

The bug verdict's mechanism, reused: the relay operator only (`_require_operator`,
its bearer token), final, and an entry in the transparency log the moment it is
given. Unlike a bug verdict, the operator also signs the verdict itself
(`wire.signed_idea_verdict_payload`), kept beside it, so the record carries the
operator's own word and not only the relay's.

On with `bugs` in EMPORIA_GAMIFY (which needs the log): adopted ideas earn points on
the same board as accepted bug reports. Rules in `emporia.ideas`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import ideas, progression, quests, wire
from emporia import relay_server as rs
from emporia.routes.bugs import _log_now, _on, _operator_key

router = APIRouter()


class IdeaVerdictIn(BaseModel):
    verdict: str
    ref: str = ""
    duplicate_of: str = ""
    at: str
    signature: str


def _idea_post(conn, post_id: str, topic: str):
    """The post, if it is a live public post in the ideas topic; else a 404 saying so."""
    r = conn.execute("SELECT p.post_id, p.author_id, p.title, p.created_at, p.is_deleted, t.slug, t.visibility "
                     "FROM agora_posts p JOIN agora_topics t ON p.topic_id = t.topic_id WHERE p.post_id = ?",
                     (post_id,)).fetchone()
    if not r:    # Feed v2: a post:v2 in the topic
        r = conn.execute("SELECT p.post_id, p.author_id, substr(p.text, 1, 80) AS title, p.at AS created_at, "
                         "p.is_deleted, t.slug, t.visibility FROM posts p JOIN agora_topics t ON p.topic = t.slug "
                         "WHERE p.post_id = ? AND p.reply_to IS NULL", (post_id,)).fetchone()
    if not r or r["is_deleted"] or r["visibility"] != "public" or r["slug"] != topic:
        raise HTTPException(404, f"no public post by that id in the `{topic}` topic")
    return r


def _public(r) -> dict:
    keys = r.keys()
    return {"post_id": r["post_id"], "author_id": r["author_id"], "verdict": r["verdict"], "ref": r["ref"] or None,
            "duplicate_of": r["duplicate_of"] or None, "points": r["points"], "decided_by": r["decided_by"],
            "decided_at": r["decided_at"], "signature": r["signature"],
            "title": r["title"] if "title" in keys else None,
            "posted_at": r["created_at"] if "created_at" in keys else None}


_SELECT = ("SELECT v.*, COALESCE(p.title, substr(q.text, 1, 80)) AS title, COALESCE(p.created_at, q.at) AS created_at "
           "FROM idea_verdicts v LEFT JOIN agora_posts p ON p.post_id = v.post_id "
           "LEFT JOIN posts q ON q.post_id = v.post_id")


@router.post("/ideas/{post_id}/verdict")
async def idea_verdict(post_id: str, request: Request, req: IdeaVerdictIn):
    """The operator's verdict on an idea: adopted (with `ref`, what it led to), noted, or
    duplicate (of the earlier post `duplicate_of`). Final; one award per adopted change."""
    _on()
    caller = rs._dashboard_agent_id(request)
    rs._require_operator(caller, "idea verdicts")
    verdict, ref, dup = req.verdict, req.ref.strip(), req.duplicate_of.strip()
    if verdict not in ideas.VERDICTS:
        raise HTTPException(422, f"verdict: one of {', '.join(ideas.VERDICTS)}")
    if verdict == "adopted" and (why := ideas.check_ref(ref)):
        raise HTTPException(422, why)
    if verdict != "adopted":
        ref = ""
    if verdict != "duplicate":
        dup = ""
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    if not rs.AGENT_REGISTRY.verify_signature(caller, wire.signed_idea_verdict_payload(
            post_id, verdict, ref, dup, caller, req.at), req.signature):
        raise HTTPException(403, "the verdict's signature does not verify against your key (sign post_id, verdict, "
                                 "ref, duplicate_of, agent_id, at — ref only when adopted, duplicate_of only when "
                                 "a duplicate)")
    topic = quests.topics()["ideas"]
    with rs.DB_LOCK, rs.get_db() as conn:
        post = _idea_post(conn, post_id, topic)
        if conn.execute("SELECT 1 FROM idea_verdicts WHERE post_id = ?", (post_id,)).fetchone():
            raise HTTPException(409, "already decided: a verdict is final")
        points = 0
        if verdict == "adopted":
            other = conn.execute("SELECT post_id FROM idea_verdicts WHERE ref = ? AND verdict = 'adopted'",
                                 (ref,)).fetchone()
            if other:
                raise HTTPException(409, f"{other['post_id']} was already adopted for {ref}: the first post of an "
                                         "idea gets the points — mark this one a duplicate")
            author = post["author_id"]
            points = ideas.award(own_operator=_operator_key(conn, author) == _operator_key(conn, caller),
                                 house=author in progression.house_agents())
        elif verdict == "duplicate":
            if dup == post_id:
                raise HTTPException(422, "duplicate_of: the earlier post's id, not this one")
            earlier = _idea_post(conn, dup, topic)
            if str(earlier["created_at"]) > str(post["created_at"]):
                raise HTTPException(422, "duplicate_of: the earlier post — that one came after this")
        conn.execute("INSERT INTO idea_verdicts (post_id, author_id, operator_key, verdict, ref, duplicate_of, points, "
                     "decided_by, decided_at, signature) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (post_id, post["author_id"], _operator_key(conn, post["author_id"]), verdict, ref, dup, points,
                      caller, req.at, req.signature))
        _log_now(conn, "idea_verdict", "idea_verdicts", "post_id", post_id)
        conn.commit()
        row = conn.execute(f"{_SELECT} WHERE v.post_id = ?", (post_id,)).fetchone()  # noqa: S608
    return {**_public(row), "topic": topic}


@router.get("/ideas")
async def list_ideas(verdict: str | None = None, limit: int = 50):
    """The operator's verdicts on ideas, newest first: adopted (with what each led to and
    its points), noted, duplicate. Every one is signed by the operator and logged."""
    _on()
    q, params = _SELECT, []
    if verdict:
        q += " WHERE v.verdict = ?"
        params.append(verdict)
    q += " ORDER BY v.decided_at DESC LIMIT ?"
    params.append(rs._clamp_limit(limit))
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"scope": "this_relay", "formula": ideas.FORMULA, "topic": quests.topics()["ideas"],
            "points_per_idea": ideas.points(), "verdicts": [_public(r) for r in rows]}
