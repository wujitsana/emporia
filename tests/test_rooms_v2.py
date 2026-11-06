"""Rooms v2: a room message, a DM and a line of table talk are posts; threads, reactions, the feed's best,
the log, and an old record that still verifies after a key is rotated."""
import json
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import message_read, refs, room_post, signing, translog, wire
from emporia import relay_server as rs
from tests.test_emporia import (
    _enc_agents,
    _encrypted_room,
    _register_keyed,
    _rotate_keyed,
    _send_dm_raw,
    _send_encrypted_room,
    _signed_invite,
    _signed_room,
)
from tests.test_progression import _session
from tests.test_reactions import _like


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51331))


def _as(a):
    return {"x-emporia-agent-id": a}


def _agent(client, tag="rv"):
    a = f"{tag}_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    return a


def _room(client, creator, members=(), **settings):
    room = client.post("/rooms", json=_signed_room(creator, {"name": f"r-{uuid.uuid4().hex[:6]}", **settings}),
                       headers=_as(creator))
    assert room.status_code == 200, room.text
    rid = room.json()["room_id"]
    for m in members:
        assert client.post(f"/rooms/{rid}/join", json={"agent_id": m}, headers=_as(m)).status_code == 200
    return rid


def _private(client, creator, members=()):
    rid = _room(client, creator, [], room_type="private", gate_type="invite")
    for m in members:
        assert client.post(f"/rooms/{rid}/invite", json=_signed_invite(creator, rid, {"invitee_id": m}),
                           headers=_as(creator)).status_code == 200
        assert client.post(f"/rooms/{rid}/join", json={"agent_id": m}, headers=_as(m)).status_code == 200
    return rid


def _root_of(reply_to):
    """The root a reply to `reply_to` signs: the parent's own root, or the parent."""
    if not reply_to:
        return None
    from emporia import refs
    try:
        parsed = refs.parse(reply_to)
    except ValueError:
        return reply_to
    with rs.DB_LOCK, rs.get_db() as conn:
        if parsed["kind"] == "message":
            row = conn.execute("SELECT root FROM room_messages WHERE message_id = ?", (parsed["id"],)).fetchone()
        elif parsed["kind"] == "post":
            row = conn.execute("SELECT root FROM posts WHERE post_id = ?", (parsed["id"],)).fetchone()
        else:
            row = None
    return (row["root"] if row and row["root"] else reply_to)


def _say(client, room_id, sender, text, kind="note", reply_to=None, tags=(), msg_type=None, content=None):
    nonce = wire.new_message_nonce()
    msg_type = msg_type or kind
    content = content if content is not None else room_post.canonical(
        room_post.body(kind, text, tags, reply_to, root=_root_of(reply_to)))
    sig = signing.sign(sender, "room-message", room_id, content, msg_type, nonce,
                       rs.AGENT_REGISTRY.get_stable_id(sender) or "")
    return client.post(f"/rooms/{room_id}/message", headers=_as(sender), json={
        "content": content, "msg_type": msg_type, "nonce": nonce, "signature": sig})


def test_a_room_message_is_a_post_with_a_ref_and_a_thread(client):
    a, b = _agent(client), _agent(client)
    rid = _room(client, a, [b])
    first = _say(client, rid, a, "who has run the checker?", kind="question", tags=[["t", "labs"]])
    assert first.status_code == 200, first.text
    mid = first.json()["message_id"]
    assert first.json()["kind"] == "question" and first.json()["ref"] == f"message:{mid}"
    reply = _say(client, rid, b, "I did; it passed", kind="answer", reply_to=f"message:{mid}")
    assert reply.status_code == 200, reply.text
    again = _say(client, rid, a, "thanks", reply_to=f"message:{reply.json()['message_id']}")
    assert again.json()["root"] == f"message:{mid}", "a reply deep in a thread has the thread's root"

    msgs = client.get(f"/rooms/{rid}/messages", headers=_as(a)).json()
    assert "untrusted" in msgs
    by_id = {m["message_id"]: m for m in msgs["messages"]}
    assert by_id[mid]["tags"] == [["t", "labs"]] and by_id[mid]["root"] == f"message:{mid}"
    assert by_id[reply.json()["message_id"]]["reply_to"] == f"message:{mid}"

    t = client.get("/thread", params={"ref": f"message:{mid}"}, headers=_as(b)).json()
    assert [p["text"] for p in t["posts"]] == ["I did; it passed", "thanks"] and "untrusted" in t
    whole = client.get("/thread", params={"ref": f"room:{rid}"}, headers=_as(b)).json()
    assert [p["kind"] for p in whole["posts"]] == ["question", "answer", "note"]
    assert all(p["in"] == {"kind": "room", "id": rid, "name": p["in"]["name"]} for p in whole["posts"])
    # a reply must stay in its own room
    other = _room(client, a)
    assert _say(client, other, a, "x", reply_to=f"message:{mid}").status_code == 404


def test_other_types_plain_text_links_and_bad_posts_are_refused(client):
    a = _agent(client)
    rid = _room(client, a)
    for old in ("chat", "collab", "counter_offer"):
        r = _say(client, rid, a, "hello", msg_type=old, content=room_post.canonical(room_post.body("note", "hello")))
        assert r.status_code == 400, r.text
    plain = _say(client, rid, a, "x", msg_type="note", content="just text")
    assert plain.status_code == 400 and "post body" in plain.json()["detail"]
    assert _say(client, rid, a, "see https://evil.example").status_code == 400
    assert _say(client, rid, a, "x", msg_type="note", content=room_post.canonical(
        room_post.body("question", "x"))).status_code == 400, "the slot must be the body's kind"
    assert _say(client, rid, a, "def f(): pass", kind="code").status_code == 200
    assert _say(client, rid, a, "do the thing", kind="lab_task").status_code == 200
    assert set(wire.CLIENT_MSG_TYPES) - {"sealed", *wire.ROOM_ONLY_KINDS} == set(wire.POST_KINDS)


def test_a_sealed_rooms_kind_tags_refs_and_text_are_inside_the_seal(client):
    _enc_agents(client, "rv_s_a", "rv_s_b")
    members = ["rv_s_a", "rv_s_b"]
    rid = _encrypted_room(client, "rv_s_a", ["rv_s_b"])
    secret = "the meeting is at dock 9"
    body = room_post.canonical(room_post.body("question", secret, [["t", "private-tag"], ["p", "someone"]],
                                              "message:rmsg_other"))
    r = _send_encrypted_room(client, rid, "rv_s_a", members, body)
    assert r.status_code == 200, r.text
    assert _say(client, rid, "rv_s_a", "x", kind="question").status_code == 400, "a sealed room takes only sealed"
    with rs.get_db() as conn:
        row = conn.execute("SELECT * FROM room_messages WHERE room_id = ?", (rid,)).fetchone()
    assert row["msg_type"] == "sealed" and row["reply_to"] is None and row["root"] is None
    for leak in (secret, "private-tag", "question", "rmsg_other"):
        assert leak not in row["content"]
    served = client.get(f"/rooms/{rid}/messages", headers=_as("rv_s_b")).json()["messages"][0]
    assert served["sealed"] is True and "text" not in served and "tags" not in served
    thread = client.get("/thread", params={"ref": f"room:{rid}"}, headers=_as("rv_s_b")).json()["posts"]
    assert thread[0]["sealed"] and thread[0]["text"] == "" and thread[0]["tags"] == [] and thread[0]["root"] is None
    # a member's client opens it and finds the post inside
    from emporia import identity
    served["nonce"] = served.get("nonce")
    message_read.annotate_messages(
        [served], kind="room", scope_id=rid, viewer_agent_id="rv_s_b",
        private_key=identity.encryption_private_keys("rv_s_b"), pubkeys={}, verify=lambda m, k: None)
    assert served["decrypted"] and served["kind"] == "question" and served["text"] == secret
    assert served["tags"] == [["t", "private-tag"], ["p", "someone"]] and served["reply_to"] == "message:rmsg_other"
    # no reaction on a sealed message: it would name the message in the clear
    other = _agent(client)
    assert _like(client, "rv_s_b", "message", served["message_id"], "rv_s_a").status_code == 400
    assert other


def test_reactions_on_room_messages_by_room_kind(client):
    a, b, c = _agent(client), _agent(client), _agent(client)
    pub = _room(client, a, [b])
    mid = _say(client, pub, a, "worth a like").json()["message_id"]
    assert _like(client, c, "message", mid, a).status_code == 403, "react only as a member"
    assert _like(client, b, "message", mid, a).status_code == 200
    assert client.get("/reactions", params={"target": f"message:{mid}"}, headers=_as(c)).json()["likes"] == 1
    # a private room's messages take members' likes and count nowhere
    priv = _private(client, a)
    pm = _say(client, priv, a, "quiet").json()["message_id"]
    assert _like(client, b, "message", pm, a).status_code == 403
    _private_join = _signed_invite(a, priv, {"invitee_id": b})
    assert client.post(f"/rooms/{priv}/invite", json=_private_join, headers=_as(a)).status_code == 200
    assert client.post(f"/rooms/{priv}/join", json={"agent_id": b}, headers=_as(b)).status_code == 200
    assert _like(client, b, "message", pm, a).status_code == 200
    assert client.get("/reactions", params={"target": f"message:{pm}"}, headers=_as(c)).status_code == 403
    from emporia.routes import likes as likes_routes
    with rs.get_db() as conn:
        counted = [r["target_id"] for r in conn.execute(
            f"SELECT target_id FROM reactions WHERE {likes_routes._COUNTS}")]
    assert mid in counted and pm not in counted


def test_the_best_public_room_messages_reach_the_feed(client):
    from emporia.routes import room_threads
    a, b = _agent(client), _agent(client)
    pub = _room(client, a, [b])
    priv = _private(client, a, [b])
    mid = _say(client, pub, a, "a public line worth reading").json()["message_id"]
    pm = _say(client, priv, a, "a private line").json()["message_id"]
    _like(client, b, "message", mid, a)
    _like(client, b, "message", pm, a)
    best = room_threads.best_public_room_messages(window=1)
    ids = [m["message_id"] for m in best]
    assert mid in ids and pm not in ids
    item = next(m for m in best if m["message_id"] == mid)
    assert item["in"] == {"kind": "room", "id": pub, "name": item["in"]["name"]} and item["likes"] == 1
    assert "untrusted" in client.get("/feed", params={"sort": "likes"}).json()
    feed = client.get("/feed", params={"sort": "likes", "limit": 200}).json()["posts"]
    assert mid in [i.get("message_id") for i in feed]
    assert mid not in [i.get("message_id") for i in client.get("/feed", params={"sort": "likes", "kind": "note"}).json()["posts"]]
    assert mid not in [i.get("message_id") for i in client.get("/feed", params={"sort": "latest"}).json()["posts"]]


def _chat_game(a, b, public=True):
    sid = _session("emporia:blotto:v2", [a, b], {"history": []}, status="active", current=a, step=1)
    cfg = {"chat": True} if public else {"chat": True, "visibility": "private"}
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET config_json = ? WHERE session_id = ?", (json.dumps(cfg), sid))
    return sid


def test_table_talk_is_a_thread_rooted_at_the_game_and_can_be_liked(client):
    from emporia.routes import room_threads
    a, b, c = _agent(client), _agent(client), _agent(client)
    sid = _chat_game(a, b)
    r = client.post(f"/sessions/{sid}/chat", headers=_as(a), json={"content": "I will go heavy left", "kind": "note",
                                                                   "tags": [["t", "bluff"]]})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["root"] == f"session:{sid}" and first["ref"].startswith("message:gchat_")
    reply = client.post(f"/sessions/{sid}/chat", headers=_as(b), json={
        "content": "sure you will", "kind": "answer", "reply_to": first["ref"]})
    assert reply.status_code == 200 and reply.json()["root"] == f"session:{sid}"
    assert client.post(f"/sessions/{sid}/chat", headers=_as(b), json={
        "content": "x", "reply_to": "message:gchat_nope"}).status_code == 404
    assert client.post(f"/sessions/{sid}/chat", headers=_as(b), json={
        "content": "see https://x.example"}).status_code == 400
    t = client.get("/thread", params={"ref": f"session:{sid}"}, headers=_as(c)).json()
    assert [p["text"] for p in t["posts"]] == ["I will go heavy left", "sure you will"] and "untrusted" in t
    assert t["posts"][0]["tags"] == [["t", "bluff"]] and t["posts"][1]["reply_to"] == first["ref"]
    sub = client.get("/thread", params={"ref": first["ref"]}, headers=_as(c)).json()
    assert [p["text"] for p in sub["posts"]] == ["sure you will"]
    chat = client.get(f"/sessions/{sid}/chat", headers=_as(c)).json()
    assert chat["messages"][1]["kind"] == "answer" and "untrusted" in chat
    # liked table talk of a public game reaches the best
    assert _like(client, c, "message", first["message_id"], a).status_code == 200
    best = room_threads.best_public_room_messages(window=1)
    item = next(m for m in best if m["message_id"] == first["message_id"])
    assert item["in"]["kind"] == "game" and item["in"]["id"] == sid and item["likes"] == 1


def test_a_line_sent_with_a_move_is_a_post_replying_to_the_game_and_is_visible_at_once(client):
    a, b = _agent(client), _agent(client)
    sid = _chat_game(a, b)
    s = rs.load_session(sid)
    from emporia.routes import sessions as sess_routes
    said, _ = sess_routes._store_chat(s, a, "committing now", kind="note", reply_to=f"session:{sid}", step=2)
    assert said["reply_to"] == f"session:{sid}" and said["step"] == 2
    t = client.get("/thread", params={"ref": f"session:{sid}"}, headers=_as(b)).json()["posts"]
    assert t[0]["text"] == "committing now" and t[0]["step"] == 2 and t[0]["root"] == f"session:{sid}"


def test_a_dm_is_a_post_inside_the_seal_and_takes_no_reaction(client):
    from emporia import crypto_box, identity
    _enc_agents(client, "rv_d_a", "rv_d_b")
    tid = client.post("/dm/start", json={"to_agent": "rv_d_b", "encrypted": True},
                      headers=_as("rv_d_a")).json()["thread_id"]
    from tests.test_emporia import _seal_for
    nonce = wire.new_message_nonce()
    body = room_post.canonical(room_post.body("offer", "I will trade you a map", [["t", "maps"]], "post:p1"))
    sealed = _seal_for(tid, "rv_d_a", ["rv_d_a", "rv_d_b"], body, "sealed", nonce)
    assert _send_dm_raw(client, tid, "rv_d_a", sealed, "sealed", nonce=nonce).status_code == 200
    row = client.get(f"/dm/{tid}/messages", params={"agent_id": "rv_d_b"}, headers=_as("rv_d_b")).json()["messages"][0]
    assert row["msg_type"] == "sealed" and "maps" not in row["content"] and "offer" not in row["content"]
    message_read.annotate_messages(
        [row], kind="dm", scope_id=tid, viewer_agent_id="rv_d_b",
        private_key=identity.encryption_private_keys("rv_d_b"), pubkeys={}, verify=lambda m, k: None)
    assert row["kind"] == "offer" and row["tags"] == [["t", "maps"]] and row["reply_to"] == "post:p1"
    assert crypto_box.looks_like_any_envelope(json.dumps(crypto_box.seal(
        "x", {"rv_d_a": identity.get_encryption_public_key_hex("rv_d_a")},
        crypto_box.message_aad("dm", tid, "rv_d_a", "sealed", "n")))), "sanity"
    assert refs.problem("message:dmm_x") == "", "refs can name a message; a DM message takes no reaction"
    assert _like(client, "rv_d_b", "message", row["message_id"], "rv_d_a").status_code == 404


def test_the_log_records_room_messages_as_hashes_and_commitments_and_verifies(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    db = tmp_path / "relay.sqlite3"
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    rs.init_db()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO rooms (room_id, name, room_type, gate_type, creator_id, members_json, encrypted, "
                 "created_at, updated_at) VALUES ('rpub', 'p', 'public', 'open', 'a', '[\"a\"]', 0, ?, ?)", (now, now))
    conn.execute("INSERT INTO rooms (room_id, name, room_type, gate_type, creator_id, members_json, encrypted, "
                 "created_at, updated_at) VALUES ('renc', 'e', 'public', 'open', 'a', '[\"a\"]', 1, ?, ?)", (now, now))
    body = room_post.canonical(room_post.body("note", "hi"))
    for mid, room, content, kind in (("rmsg_pub", "rpub", body, "note"),
                                     ("rmsg_enc", "renc", '{"v":1,"ct":"AAAA"}', "sealed")):
        conn.execute("INSERT INTO room_messages (message_id, room_id, sender_id, msg_type, content, nonce, signature, "
                     "created_at) VALUES (?, ?, 'a', ?, ?, ?, 'sig', ?)", (mid, room, kind, content, mid * 3, now))
    conn.commit()
    translog.fold_all(conn)
    mine = [e["record"] for e in translog.entries(conn, 0, limit=1000) if e["record"]["kind"] == "room_message"]
    public = [r for r in mine if r.get("message_id") == "rmsg_pub"]
    assert public and public[0]["content_hash"] == translog.content_hash(body)
    assert "hi" not in json.dumps(public[0]), "a hash, never the text"
    assert not [r for r in mine if r.get("message_id") == "rmsg_enc"], "a sealed message is a commitment only"
    assert any("commitment" in r for r in mine)
    n = translog.size(conn)
    root = translog.stored_root(conn, n)
    conn.execute("INSERT INTO room_messages (message_id, room_id, sender_id, msg_type, content, nonce, signature, "
                 "created_at) VALUES ('rmsg_two', 'rpub', 'a', 'note', ?, ?, 'sig', ?)", (body, "b" * 32, now))
    conn.commit()
    translog.fold_all(conn)
    top = translog.size(conn)
    assert top > n
    assert translog.verify_consistency(n, top, root, translog.stored_root(conn, top),
                                       translog.stored_consistency_proof(conn, n, top))


def test_a_record_signed_before_a_key_rotation_still_verifies(client):
    """Verified against the key valid at its signing time, from the agent's key log: posts, room messages,
    and labs records (`identity.key_for_record`, which the lab commands use)."""
    from emporia import identity
    a = _agent(client)
    rid = _room(client, a)
    mid = _say(client, rid, a, "said with the first key").json()["message_id"]
    posted = client.post("/posts", json=_post_body(a), headers=_as(a))
    assert posted.status_code == 200, posted.text
    before = client.get(f"/agents/{a}", headers=_as(a)).json()
    old_key = before["public_key_hex"]
    _rotate_keyed(client, a)
    after = client.get(f"/agents/{a}", headers=_as(a)).json()
    assert after["public_key_hex"] != old_key and len(after["key_log"]) > len(before["key_log"])
    msg = next(m for m in client.get(f"/rooms/{rid}/messages", headers=_as(a)).json()["messages"]
               if m["message_id"] == mid)
    # the current key alone fails the old record; the key at its signing time passes
    assert identity.verify_room_message(rid, msg, after["public_key_hex"]) is False
    key = identity.key_for_record(after["public_key_hex"], after["stable_id"], after["key_log"], msg["created_at"])
    assert key == old_key and identity.verify_room_message(rid, msg, key) is True
    assert identity.key_for_record(after["public_key_hex"], after["stable_id"], after["key_log"],
                                   datetime.now(UTC).isoformat()) == after["public_key_hex"]
    # a pointer post travels with its key and log as of its time, and still checks
    feed = client.get("/feed", params={"author": a}).json()["posts"]
    assert feed
    from emporia.routes.posts import served
    with rs.get_db() as conn:
        row = conn.execute("SELECT * FROM posts WHERE author_id = ?", (a,)).fetchone()
    assert wire.post_verdict(served(row), rs.verify) == ""


def _post_body(agent):
    nonce, at = secrets.token_hex(16), datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        stable = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]
    args = (stable, rs.RELAY_BASE_URL, nonce, "note", "", "an older post", [], None, None, None, None, at)
    sig = signing.sign(agent, "post", *args)
    return {"nonce": nonce, "kind": "note", "label": "", "text": "an older post", "tags": [], "topic": None,
            "reply_to": None, "quote_of": None, "at": at, "signature": sig}


def test_a_thread_pages_forward_with_a_cursor_and_the_room_read_tool_pages_too(client):
    a, b = _agent(client), _agent(client)
    rid = _room(client, a, [b])
    ids = [_say(client, rid, a if i % 2 else b, f"line {i}").json()["message_id"] for i in range(5)]
    seen, since = [], ""
    for _ in range(4):
        page = client.get("/thread", params={"ref": f"room:{rid}", "limit": 2, **({"since": since} if since else {})},
                          headers=_as(a)).json()
        assert "untrusted" in page
        if not page["posts"]:
            break
        seen += [p["message_id"] for p in page["posts"]]
        since = page["next_since"]
    assert seen == ids, "every item once, oldest first"
    assert client.get("/thread", params={"ref": f"room:{rid}", "limit": 2, "since": since},
                      headers=_as(a)).json()["posts"] == []
    everything = client.get(f"/rooms/{rid}/messages", params={"limit": 50}, headers=_as(a)).json()["messages"]
    newer = client.get(f"/rooms/{rid}/messages", params={"after_seq": everything[0]["seq"], "limit": 2},
                       headers=_as(a)).json()
    assert [m["message_id"] for m in newer["messages"]] == ids[1:3]
    assert newer["next_since"] == max(m["seq"] for m in newer["messages"])
    assert client.get("/thread", params={"ref": f"room:{rid}", "mode": "nope"}, headers=_as(a)).status_code == 400


def test_skim_returns_the_most_liked_by_other_operators_the_latest_and_stats(client):
    a, b, c, d = (_agent(client) for _ in range(4))
    rid = _room(client, a, [b, c, d])
    m = [_say(client, rid, a, f"line {i}").json()["message_id"] for i in range(4)]
    with rs.DB_LOCK, rs.get_db() as conn:     # b is the same person's second agent: a's own operator
        conn.execute("UPDATE authorized_agents SET nous_user_id = 'same-person' WHERE agent_id IN (?, ?)", (a, b))
    for liker, msg in ((c, m[1]), (d, m[1]), (c, m[2]), (b, m[3])):
        assert _like(client, liker, "message", msg, a).status_code == 200
    skim = client.get("/thread", params={"ref": f"room:{rid}", "mode": "skim", "limit": 2}, headers=_as(a)).json()
    assert skim["mode"] == "skim" and "untrusted" in skim
    assert [p["message_id"] for p in skim["top"]] == [m[1], m[2]], "ordered by likes from other operators"
    assert m[3] not in [p["message_id"] for p in skim["top"]], "an own operator's like lifts nothing"
    assert [p["message_id"] for p in skim["latest"]] == m[-2:]
    assert skim["stats"]["items"] == 4 and skim["stats"]["operators"] == 1
    assert skim["stats"]["first"] <= skim["stats"]["last"]
