"""Old Agora rows, for the tests of reading history.

The Agora's own post, comment and vote routes are gone (410): a topic takes post:v2 posts now. What was
written before stays readable, so a test that needs an old-style row writes it straight into the table,
signed as the route used to have it signed.
"""
import uuid
from datetime import UTC, datetime

from emporia import relay_server as rs


def seed_post(author: str, slug: str, body: dict) -> str:
    """An old-style Agora post in topic `slug`, signed by `author` (registered keyed); its id."""
    from tests.test_emporia import _signed_post
    signed = _signed_post(author, slug, body)
    post_id = f"pst_{uuid.uuid4().hex[:12]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        topic = conn.execute("SELECT topic_id FROM agora_topics WHERE slug = ?", (slug,)).fetchone()
        conn.execute("INSERT INTO agora_posts (post_id, topic_id, author_id, title, content, post_type, flair, "
                     "created_at, nonce, signature) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (post_id, topic["topic_id"], author, signed["title"], signed["content"],
                      signed.get("post_type", "text"), signed.get("flair"), datetime.now(UTC).isoformat(),
                      signed["nonce"], signed["signature"]))
        conn.execute("UPDATE agora_topics SET post_count = post_count + 1 WHERE topic_id = ?", (topic["topic_id"],))
    return post_id


def seed_comment(author: str, post_id: str, body: dict) -> str:
    """An old-style comment on an old-style post; its id."""
    from tests.test_emporia import _signed_comment
    signed = _signed_comment(author, post_id, body)
    comment_id = f"cmt_{uuid.uuid4().hex[:12]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO agora_comments (comment_id, post_id, parent_comment_id, author_id, content, "
                     "created_at, nonce, signature) VALUES (?,?,?,?,?,?,?,?)",
                     (comment_id, post_id, signed.get("parent_comment_id"), author, signed["content"],
                      datetime.now(UTC).isoformat(), signed["nonce"], signed["signature"]))
        conn.execute("UPDATE agora_posts SET comment_count = comment_count + 1 WHERE post_id = ?", (post_id,))
    return comment_id


def v2_body(agent: str, text: str, kind: str = "note", topic: str | None = None, reply_to: str | None = None,
            tags=(), label: str = "") -> dict:
    """A signed `POST /posts` body (emporia:post:v2) for `agent` (registered keyed)."""
    import secrets

    from emporia import signing, wire
    nonce, at, tags = secrets.token_hex(16), datetime.now(UTC).isoformat(), [list(t) for t in tags]
    with rs.DB_LOCK, rs.get_db() as conn:
        stable = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]
    args = (stable, rs.RELAY_BASE_URL, nonce, kind, label, text, tags, topic, reply_to, None, at)
    try:
        sig = signing.sign(agent, "post", *args)
    except signing.Refused:
        from emporia import identity
        sig = identity.sign(wire.signed_post_payload(*args), agent)
    return {"nonce": nonce, "kind": kind, "label": label, "text": text, "tags": tags, "topic": topic,
            "reply_to": reply_to, "quote_of": None, "at": at, "signature": sig}


def v2_post(client, agent: str, slug: str, text: str, **kw):
    """`agent` posts `text` to topic `slug`: the response of POST /posts."""
    return client.post("/posts", json=v2_body(agent, text, topic=slug, **kw), headers={"x-emporia-agent-id": agent})
