"""Signed `post:v2` posts for tests: a body for `POST /posts`, and a post into an Agora topic."""
from datetime import UTC, datetime

from emporia import relay_server as rs


def v2_body(agent: str, text: str, kind: str = "note", topic: str | None = None, reply_to: str | None = None,
            tags=(), label: str = "") -> dict:
    """A signed `POST /posts` body (emporia:post:v2) for `agent` (registered keyed)."""
    import secrets

    from emporia import signing, wire
    nonce, at, tags = secrets.token_hex(16), datetime.now(UTC).isoformat(), [list(t) for t in tags]
    with rs.DB_LOCK, rs.get_db() as conn:
        stable = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]
    from tests.test_posts_v2 import _root_for
    root = _root_for(reply_to)
    args = (stable, rs.RELAY_BASE_URL, nonce, kind, label, text, tags, topic, reply_to, None, root, at)
    try:
        sig = signing.sign(agent, "post", *args)
    except signing.Refused:
        from emporia import identity
        sig = identity.sign(wire.signed_post_payload(*args), agent)
    return {"nonce": nonce, "kind": kind, "label": label, "text": text, "tags": tags, "topic": topic,
            "reply_to": reply_to, "quote_of": None, "root": root, "at": at, "signature": sig}


def v2_post(client, agent: str, slug: str, text: str, **kw):
    """`agent` posts `text` to topic `slug`: the response of POST /posts."""
    return client.post("/posts", json=v2_body(agent, text, topic=slug, **kw), headers={"x-emporia-agent-id": agent})


def seed_post_row(author: str, slug: str, text: str, at: str, kind: str = "note", reply_to: str | None = None,
                  tags: list | None = None) -> str:
    """A `post:v2` row written straight into `posts` (unsigned: for tests of what reads posts, not of signing)."""
    import json
    import uuid
    post_id = f"pst_{uuid.uuid4().hex[:28]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO posts (post_id, author_id, author_stable_id, author_public_key_hex, origin_relay, "
                     "nonce, kind, text, topic, reply_to, at, signature, recorded_at, tags_json) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (post_id, author, "", "", rs.RELAY_BASE_URL, uuid.uuid4().hex, kind, text, slug, reply_to, at,
                      "sig", at, json.dumps(tags or [])))
    return post_id
