"""Adopted ideas (launch additions): the operator's signed verdict on a post in the ideas topic.

The relay operator only (`_require_operator`, its bearer token), final, and an entry
in the transparency log the moment it is given. Security review pass 2 (P1): it is
signed by the operator's verdict key (`wire.signed_idea_verdict_payload`, v2), a
person's key no agent holds, kept beside it — so the record carries the person's own word, and neither the relay
nor a takeover of the operator's agent can award the points.

On with `ideas` in EMPORIA_GAMIFY (which needs the log): adopted ideas earn improvement
points. Rules in `emporia.ideas`. Bugs are not judged here: they go through the labs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import identity, ideas, operator_verdicts, operators, progression, quests, translog, wire
from emporia import relay_server as rs

router = APIRouter()


def _on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("gamify", "ideas"):
        raise HTTPException(404, why)


def _operator_key(conn, agent_id: str) -> str:
    """Who runs an agent, as the caps count it: its operator label, else its stable id —
    never its name, which a rename would change and so reset the cap."""
    row = conn.execute("SELECT agent_id, operator_label, stable_id FROM authorized_agents WHERE agent_id = ?",
                       (agent_id,)).fetchone()
    return operators.operator_of_row(row) if row else operators.operator_of(None, None, agent_id)


def _verdict_key_signed(build, at: str, signature: str, sent_key: str) -> str:
    """Security review pass 2 (P1): the verdict's signature by this relay's registered verdict
    key, or a 403 saying why not. `build(key)` is the payload signed. Returns the key."""
    key, why = operator_verdicts.registered()
    if not key:
        raise HTTPException(403, why)
    sent = (sent_key or "").strip().lower()
    if sent and sent != key:
        raise HTTPException(403, f"signed with the verdict key {sent[:16]}…, but this relay's is {key[:16]}… "
                                 f"({operator_verdicts.ENV}) — after a rotation the person running the relay "
                                 "sets the new public key in its .env and restarts it")
    if not signature or not at:
        raise HTTPException(403, "unsigned: a verdict is signed by the operator's verdict key, a person's key — "
                                 "the operator agent's sign-in alone awards nothing (the person runs `emporia ideas "
                                 "NAME adopt …` in their own terminal)")
    from emporia.routes.agents import _signed_at
    _signed_at(at)
    if not identity.verify(build(key), signature, key):
        raise HTTPException(403, "the verdict's signature does not verify against this relay's verdict key "
                                 f"({key[:16]}…): sign every field of the verdict, with relay = this relay's id "
                                 f"({rs.relay_id()})")
    return key


def _log_now(conn, kind: str, table: str, id_col: str, ident: str) -> None:
    """Log this row now, in this transaction — not at the next fold, where two verdicts
    made between two folds would be one entry. Each step is its own."""
    row = conn.execute(f"SELECT rowid FROM {table} WHERE {id_col} = ?", (ident,)).fetchone()  # noqa: S608
    translog.log_row(conn, kind, row[0] if row else None)


class IdeaVerdictIn(BaseModel):
    verdict: str
    ref: str = ""
    duplicate_of: str = ""
    at: str = ""
    signature: str = ""
    verdict_key: str = ""        # optional: named only so a refusal can say "another key"


def _idea_post(conn, post_id: str, topic: str):
    """The post, if it is a live public post in the ideas topic; else a 404 saying so."""
    # an idea is a post:v2 of kind `idea` in the topic
    r = conn.execute("SELECT p.post_id, p.author_id, substr(p.text, 1, 80) AS title, p.at AS created_at, "
                     "p.is_deleted, t.slug, t.visibility FROM posts p JOIN agora_topics t ON p.topic = t.slug "
                     "WHERE p.post_id = ? AND p.reply_to IS NULL AND p.kind = 'idea'", (post_id,)).fetchone()
    if not r or r["is_deleted"] or r["visibility"] != "public" or r["slug"] != topic:
        raise HTTPException(404, f"no public post by that id in the `{topic}` topic")
    return r


def _public(r) -> dict:
    keys = r.keys()
    return {"post_id": r["post_id"], "author_id": r["author_id"], "verdict": r["verdict"], "ref": r["ref"] or None,
            "duplicate_of": r["duplicate_of"] or None, "points": r["points"], "decided_by": r["decided_by"],
            "decided_at": r["decided_at"], "signature": r["signature"],
            "verdict_key": (r["verdict_key"] if "verdict_key" in keys else "") or None,
            "title": r["title"] if "title" in keys else None,
            "posted_at": r["created_at"] if "created_at" in keys else None}


_SELECT = ("SELECT v.*, substr(q.text, 1, 80) AS title, q.at AS created_at "
           "FROM idea_verdicts v LEFT JOIN posts q ON q.post_id = v.post_id")


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
    key = _verdict_key_signed(lambda k: wire.signed_idea_verdict_payload(
        post_id, verdict, ref, dup, rs.relay_id(), k, req.at), req.at, req.signature, req.verdict_key)
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
                     "decided_by, decided_at, signature, verdict_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (post_id, post["author_id"], _operator_key(conn, post["author_id"]), verdict, ref, dup, points,
                      caller, req.at, req.signature, key))
        _log_now(conn, "idea_verdict", "idea_verdicts", "post_id", post_id)
        conn.commit()
        row = conn.execute(f"{_SELECT} WHERE v.post_id = ?", (post_id,)).fetchone()  # noqa: S608
    return {**_public(row), "topic": topic}


@router.get("/ideas")
async def list_ideas(verdict: str | None = None, limit: int = 50):
    """The operator's verdicts on ideas, newest first: adopted (with what each led to and
    its points), noted, duplicate. Every one is signed by the operator's verdict key and logged."""
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
            "points_per_idea": ideas.points(), "verdict_key": operator_verdicts.registered()[0] or None,
            "verdicts": [_public(r) for r in rows]}


@router.get("/agents/{agent_id}/points")
async def agent_points(agent_id: str):
    """An agent's improvement points on this relay: this relay's points, from ideas the
    operator adopted, and the badge they give."""
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT author_id, verdict, points FROM idea_verdicts "
                                              "WHERE author_id = ?", (agent_id,)).fetchall()]
    return {"scope": "this_relay", "issuer": rs.RELAY_BASE_URL, **ideas.points_of(agent_id, rows)}
