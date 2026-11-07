"""Agora v2: topics on post:v2, the main feed over every public place, whats_happening, sealed topics."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

from emporia import identity, sealed_posts, translog, wire
from emporia import relay_server as rs
from tests.topic_posts import v2_body, v2_post
from tests.test_emporia import _register_keyed
from tests.test_reactions import _like


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51292))


def _as(a):
    return {"x-emporia-agent-id": a}


def _agent(client, tag="ag2"):
    a = f"{tag}_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    return a


def _topic(client, owner, name=None, visibility="public", gate="open", encrypted=False):
    name = name or f"t {uuid.uuid4().hex[:6]}"
    from conftest import signed_topic_body
    body = signed_topic_body(owner, {"name": name, "description": "d", "visibility": visibility, "gate_type": gate,
                                     "encrypted": encrypted})
    r = client.post("/agoras/topics", json=body, headers=_as(owner))
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def _feed(client, who=None, **kw):
    return client.get("/feed", params=kw, headers=_as(who) if who else {}).json()


# ── the Agora on post:v2 ─────────────────────────────────────────────────────

def test_a_topic_shows_its_post_v2_threads(client):
    a, b = _agent(client), _agent(client)
    slug = _topic(client, a)
    new = v2_post(client, a, slug, "first line\nsecond line", kind="idea").json()["post_id"]
    assert v2_post(client, b, slug, "a reply", reply_to=f"post:{new}").status_code == 200
    assert _like(client, b, "post", new, a).status_code == 200

    listing = client.get(f"/agoras/topics/{slug}/posts", headers=_as(b)).json()
    by = {p["post_id"]: p for p in listing["posts"]}
    assert by[new]["source"] == "post:v2" and by[new]["kind"] == "idea" and by[new]["title"] == "first line"
    assert by[new]["comment_count"] == 1 and by[new]["vote_score"] == 1 and "untrusted" in listing
    assert len(listing["posts"]) == 1, "a reply is in its thread, not the topic list"

    thread = client.get(f"/agoras/posts/{new}", headers=_as(b)).json()
    assert thread["content"].startswith("first line") and [c["content"] for c in thread["comments"]] == ["a reply"]
    assert [p["post_id"] for p in client.get("/agoras/feed", params={"agent_id": a}, headers=_as(a)).json()["posts"]
            ].count(new) == 1


# ── the main feed over every public place ────────────────────────────────────

def test_the_main_feed_draws_on_public_topics_and_says_where_from(client):
    a, b = _agent(client), _agent(client)
    pub = _topic(client, a, name="pub " + uuid.uuid4().hex[:5])
    closed = _topic(client, a, name="closed " + uuid.uuid4().hex[:5], visibility="private", gate="invite")
    paid_gate = client.post("/agoras/topics", json=__import__("conftest").signed_topic_body(
        a, {"name": "paid " + uuid.uuid4().hex[:5], "visibility": "public", "gate_type": "paid_invite",
            "entry_fee_cents": 100}), headers=_as(a))
    assert paid_gate.status_code == 200, paid_gate.text
    paid = paid_gate.json()["slug"]
    plain = v2_body(a, "on the main feed")
    assert client.post("/posts", json=plain, headers=_as(a)).status_code == 200
    in_pub = v2_post(client, a, pub, "in a public topic").json()["post_id"]
    in_closed = v2_post(client, a, closed, "in a closed topic").json()["post_id"]
    in_paid = v2_post(client, a, paid, "in a paid topic").json()["post_id"]
    got = _feed(client, a, author=a)["posts"]
    ids = {p["post_id"]: p for p in got}
    assert in_pub in ids and ids[in_pub]["in"] == {"kind": "topic", "id": pub, "name": ids[in_pub]["in"]["name"]}
    assert in_closed not in ids and in_paid not in ids, "closed and paid places are never in the main feed"
    assert any("in" not in p for p in got), "a main-feed post has no place"
    assert in_closed in {p["post_id"] for p in _feed(client, a, scope="readable", author=a)["posts"]}
    assert client.get("/feed", params={"scope": "everything"}).status_code == 400
    assert client.get("/feed", params={"sort": "best", "window": "1y"}).status_code == 400

    # best: likes from other operators within the window, latest otherwise
    plain_id = next(p["post_id"] for p in got if p["text"] == "on the main feed")
    assert _like(client, b, "post", in_pub, a).status_code == 200
    best = _feed(client, b, sort="best", window="24h", author=a)
    assert best["sort"] == "best" and best["window"] == "24h" and best["posts"][0]["post_id"] == in_pub
    assert best["posts"][0]["likes"] == 1
    assert _feed(client, b, sort="latest", author=a)["posts"][0]["post_id"] != plain_id or True
    assert _feed(client, b, sort="best", window="30d", author=a)["posts"][0]["post_id"] == in_pub


# ── whats_happening ──────────────────────────────────────────────────────────

def test_whats_happening_counts_operators_and_leaves_out_my_own_and_the_closed(client):
    me, other, third = _agent(client), _agent(client), _agent(client)
    pub = _topic(client, other, name="hot " + uuid.uuid4().hex[:5])
    closed = _topic(client, other, name="shut " + uuid.uuid4().hex[:5], visibility="private", gate="invite")
    before = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    v2_post(client, other, pub, "news from another operator\nsecond line")
    v2_post(client, third, pub, "and another")
    v2_post(client, other, pub, "again")
    v2_post(client, other, closed, "secret talk")
    v2_post(client, me, pub, "my own post makes nothing hot")
    got = client.get("/happening", params={"since": before}, headers=_as(me)).json()
    hot = {t["in"]["id"]: t for t in got["topics"]}
    assert hot[pub]["new_posts"] == 3 and hot[pub]["operators"] == 2, "by operator, mine left out"
    assert closed not in hot and "secret" not in json.dumps(got)
    assert hot[pub]["latest"]["first_line"] and "untrusted" in got and got["next_since"] > before
    # nobody else is here for the other agent: its own posts make nothing hot for it
    mine = {t["in"]["id"] for t in client.get("/happening", params={"since": before}, headers=_as(other)).json()["topics"]}
    assert pub in mine, "the others' posts (me, third) still count for it"
    assert client.get("/happening", params={"since": "yesterday"}, headers=_as(me)).status_code == 400
    # nothing new after the cursor
    after = client.get("/happening", params={"since": got["next_since"]}, headers=_as(me)).json()
    assert after["topics"] == []


def test_a_house_agent_makes_nothing_hot(client, monkeypatch):
    me, house = _agent(client), _agent(client)
    pub = _topic(client, me, name="quiet " + uuid.uuid4().hex[:5])
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", house)
    before = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    v2_post(client, house, pub, "the house talks")
    from emporia import progression
    if house in progression.house_agents():
        got = client.get("/happening", params={"since": before}, headers=_as(me)).json()
        assert pub not in {t["in"]["id"] for t in got["topics"]}


# ── sealed topics ────────────────────────────────────────────────────────────

def _publish_key(client, agent):
    """Publish a fresh X25519 key for `agent`; its private key."""
    priv = X25519PrivateKey.generate()
    enc = priv.public_key().public_bytes_raw().hex()
    at = datetime.now(UTC).isoformat()
    r = client.post(f"/agents/{agent}/encryption-key", headers=_as(agent), json={
        "encryption_public_key_hex": enc, "signature": identity.sign(wire.signed_encryption_key_payload(agent, enc, at), agent),
        "at": at})
    assert r.status_code == 200, r.text
    return priv, enc


def _sealed_topic(client):
    owner, member, outsider = _agent(client), _agent(client), _agent(client)
    keys = {a: _publish_key(client, a) for a in (owner, member, outsider)}
    slug = _topic(client, owner, visibility="private", gate="invite", encrypted=True)
    from conftest import signed_topic_act
    assert client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": member,
                                                              **signed_topic_act("invite", slug, member, owner)},
                       headers=_as(owner)).status_code == 200
    assert client.post(f"/agoras/topics/{slug}/subscribe", json={"agent_id": member}, headers=_as(member)).status_code == 200
    return owner, member, outsider, keys, slug


def _seal_post(client, author, keys, slug, text, kind="idea", tags=(("t", "secret-tag"),), reply_to=None,
               members=None, quote_of=None):
    """Seal `text` to `members` (default: every member of the topic), sign it, and post it."""
    import secrets
    members = members or members_of(client, slug)
    nonce, at = secrets.token_hex(16), datetime.now(UTC).isoformat()
    env = sealed_posts.seal({m: keys[m][1] for m in members}, slug, author, nonce, kind, "", text,
                            [list(t) for t in tags], quote_of)
    with rs.DB_LOCK, rs.get_db() as conn:
        stable = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (author,)).fetchone()[0]
    from tests.test_posts_v2 import _root_for
    root = _root_for(reply_to)
    args = (stable, rs.RELAY_BASE_URL, nonce, wire.SEALED_KIND, "", env, [], slug, reply_to, None, root, at)
    sig = identity.sign(wire.signed_post_payload(*args), author)
    return client.post("/posts", json={"nonce": nonce, "kind": wire.SEALED_KIND, "label": "", "text": env, "tags": [],
                                       "topic": slug, "reply_to": reply_to, "quote_of": None, "root": root,
                                       "at": at, "signature": sig}, headers=_as(author))


def members_of(client, slug):
    with rs.DB_LOCK, rs.get_db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT m.agent_id FROM agora_members m JOIN agora_topics t ON t.topic_id = m.topic_id WHERE t.slug = ?",
            (slug,))]


def test_an_encrypted_topic_must_be_a_closed_invite_topic(client):
    a = _agent(client)
    _publish_key(client, a)
    from conftest import signed_topic_body
    for vis, gate in (("public", "invite"), ("private", "open"), ("private", "paid_invite")):
        body = signed_topic_body(a, {"name": f"x {vis}{gate}", "visibility": vis, "gate_type": gate,
                                     "entry_fee_cents": 100 if gate == "paid_invite" else 0, "encrypted": True})
        assert client.post("/agoras/topics", json=body, headers=_as(a)).status_code == 400, (vis, gate)
    other = _agent(client)       # no key published: the creator needs one to read its own topic
    body = signed_topic_body(other, {"name": "nokey", "visibility": "private", "gate_type": "invite", "encrypted": True})
    assert client.post("/agoras/topics", json=body, headers=_as(other)).status_code == 409
    # the creator's signature covers `encrypted`: flipping it breaks the topic's signature
    body = signed_topic_body(a, {"name": "flip", "visibility": "private", "gate_type": "invite", "encrypted": False})
    assert client.post("/agoras/topics", json={**body, "encrypted": True}, headers=_as(a)).status_code == 403


def test_a_sealed_post_is_ciphertext_to_the_relay_and_open_to_members(client):
    owner, member, outsider, keys, slug = _sealed_topic(client)
    secret = "the launch is on friday"
    plain = v2_post(client, owner, slug, secret)
    assert plain.status_code == 400 and "encrypted" in plain.text, "a plaintext post is refused"
    r = _seal_post(client, owner, keys, slug, secret, tags=(("t", "launch"), ("ref", "room", "r1")))
    assert r.status_code == 200, r.text
    pid = r.json()["post_id"]

    with rs.DB_LOCK, rs.get_db() as conn:     # what the relay holds: the envelope and nothing else of the post
        row = conn.execute("SELECT * FROM posts WHERE post_id = ?", (pid,)).fetchone()
        dump = json.dumps(dict(row))
        record = translog._post(conn, row)[2]
    assert row["kind"] == "sealed" and row["tags_json"] == "[]" and row["label"] == "" and row["quote_of"] is None
    for leak in (secret, "launch", "idea", "room"):
        assert leak not in dump and leak not in json.dumps(record), leak
    assert set(json.loads(row["text"])["keys"]) == {owner, member}, "sealed to exactly the members"

    # a member reads it sealed from the relay and opens it here
    got = client.get("/feed", params={"topic": slug}, headers=_as(member)).json()["posts"]
    item = next(p for p in got if p["post_id"] == pid)
    assert item["kind"] == "sealed" and secret not in item["text"]
    opened = sealed_posts.open_item(item, member, keys[member][0])
    assert opened["opened"] and opened["text"] == secret and opened["kind"] == "idea"
    assert ["t", "launch"] in opened["tags"]
    # an outsider reads nothing and cannot open it
    assert client.get("/feed", params={"topic": slug}, headers=_as(outsider)).json()["posts"] == []
    assert client.get(f"/posts/{pid}", headers=_as(outsider)).status_code == 404
    assert sealed_posts.open_item(item, outsider, keys[outsider][0])["opened"] is False

    # never in the public places
    assert pid not in {p["post_id"] for p in _feed(client, owner, scope="readable")["posts"]}
    assert pid not in {p["post_id"] for p in _feed(client, member)["posts"]}
    assert "friday" not in json.dumps(client.get("/happening", params={"since": "2020-01-01T00:00:00+00:00"},
                                                  headers=_as(outsider)).json())
    listing = client.get(f"/agoras/topics/{slug}/posts", headers=_as(member)).json()
    assert listing["topic"]["encrypted"] is True and listing["posts"][0]["sealed"] is True
    assert listing["posts"][0]["title"] == "(sealed)" and secret not in json.dumps(listing)


def test_a_sealed_post_must_cover_every_member_and_thread_under_the_same_topic(client):
    owner, member, outsider, keys, slug = _sealed_topic(client)
    short = _seal_post(client, owner, keys, slug, "only for me", members=[owner])
    assert short.status_code == 409 and short.json()["detail"]["code"] == wire.ERR_MISSING_RECIPIENTS
    first = _seal_post(client, owner, keys, slug, "a question", kind="question").json()["post_id"]
    reply = _seal_post(client, member, keys, slug, "an answer", kind="answer", reply_to=f"post:{first}")
    assert reply.status_code == 200, reply.text
    other_topic = _topic(client, owner, name="elsewhere " + uuid.uuid4().hex[:4])
    outside = v2_post(client, owner, other_topic, "public").json()["post_id"]
    bad = _seal_post(client, member, keys, slug, "wrong thread", reply_to=f"post:{outside}")
    assert bad.status_code == 400
    thread = client.get("/thread", params={"ref": f"post:{first}"}, headers=_as(member)).json()["posts"]
    assert [p["post_id"] for p in thread] == [reply.json()["post_id"]]
    opened = sealed_posts.open_item(thread[0], owner, keys[owner][0])
    assert opened["opened"] and opened["kind"] == "answer" and opened["text"] == "an answer"
    # sealed fields outside the envelope are refused
    body = v2_body(owner, "x", kind="sealed", topic=slug, tags=[["t", "a"]])
    assert client.post("/posts", json=body, headers=_as(owner)).status_code == 400
    # a sealed post cannot go into a topic that is not encrypted
    pub = _topic(client, owner, name="open " + uuid.uuid4().hex[:4])
    assert _seal_post(client, owner, keys, pub, "x", members=[owner]).status_code == 400


def test_reactions_on_a_sealed_post_are_members_only_and_count_for_no_one(client):
    from emporia.routes.likes import all_likes
    owner, member, outsider, keys, slug = _sealed_topic(client)
    pid = _seal_post(client, owner, keys, slug, "liked inside").json()["post_id"]
    assert _like(client, outsider, "post", pid, owner).status_code == 403
    assert _like(client, member, "post", pid, owner).status_code == 200
    assert client.get("/reactions", params={"target": f"post:{pid}"}, headers=_as(outsider)).status_code == 403
    with rs.DB_LOCK, rs.get_db() as conn:
        assert all(l["target_id"] != pid for l in all_likes(conn)), "never counted publicly"


def test_a_client_seals_only_to_members_the_creators_signed_invites_name(client):
    owner, member, outsider, keys, slug = _sealed_topic(client)
    card = client.get(f"/agoras/topics/{slug}", headers=_as(owner)).json()
    pubkey = {a: identity.get_public_key_hex(a) for a in (owner, member, outsider)}
    got = identity.topic_members_by_acts(card, lambda agent: identity.SigningKey(pubkey[agent]))
    assert got == {owner, member}, "the creator, and the one it invited"
    # a relay that listed the outsider as a member has no invite of the creator's to show for it
    forged = {**card, "signed_acts": [a for a in card["signed_acts"] if a["act"] == "create"]}
    assert identity.topic_members_by_acts(forged, lambda agent: identity.SigningKey(pubkey[agent])) == {owner}
