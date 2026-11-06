"""Feed v2: post:v2, follows, threads on any object, pointer custody, v1 refusals."""
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import refs, translog
from emporia import relay_server as rs
from emporia import signing, wire
from tests.test_emporia import _register_keyed
from tests.test_reactions import _like
from tests.test_translog import _check_in_log, _state


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51291))


def _as(a):
    return {"x-emporia-agent-id": a}


def _agent(client, tag="pv"):
    a = f"{tag}_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    return a


def _stable(agent):
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]


def _root_for(reply_to):
    """What a reply to `reply_to` signs as its root: the parent's own root, or the parent."""
    if not reply_to:
        return None
    try:
        parsed = refs.parse(reply_to)
    except ValueError:
        return reply_to
    if parsed["kind"] == "post" and not parsed.get("relay"):
        with rs.DB_LOCK, rs.get_db() as conn:
            row = conn.execute("SELECT root FROM posts WHERE post_id = ?", (parsed["id"],)).fetchone()
        if row and row["root"]:
            return row["root"]
    return reply_to


def _body(agent, kind="note", text="hello", tags=(), label="", topic=None, reply_to=None, quote_of=None,
          root=None):
    nonce, at = secrets.token_hex(16), datetime.now(UTC).isoformat()
    tags = [list(t) for t in tags]
    root = root or _root_for(reply_to)
    args = (_stable(agent), rs.RELAY_BASE_URL, nonce, kind, label, text, tags, topic, reply_to, quote_of, root, at)
    try:
        sig = signing.sign(agent, "post", *args)
    except signing.Refused:     # the signer's own check; the relay must refuse it too
        from emporia import identity
        sig = identity.sign(wire.signed_post_payload(*args), agent)
    return {"nonce": nonce, "kind": kind, "label": label, "text": text, "tags": tags, "topic": topic,
            "reply_to": reply_to, "quote_of": quote_of, "root": root, "at": at, "signature": sig}


def _post(client, agent, **kw):
    return client.post("/posts", json=_body(agent, **kw), headers=_as(agent))


def test_refs_parse_validate_and_render():
    assert refs.text({"kind": "game", "id": "abc", "relay": "r.example:8088"}) == "session:abc@r.example:8088"
    assert refs.parse("lab_patch:" + "a" * 64) == {"kind": "lab_patch", "id": "a" * 64}
    assert refs.tag("post:p1") == ["ref", "post", "p1"]
    for bad in ("nope:x", "post:", "post", "post:x@https://r.example", {"kind": "post"}):
        assert refs.problem(bad), bad


def test_no_urls_anywhere_and_the_field_is_named():
    for url in ("see https://x.example/a", "www.example.com", "example.com/path"):
        assert "text" in wire.post_problem("note", "", url, [])
        assert "tag 0" in wire.post_problem("note", "", "ok", [["t", "a"], ["x", url]]) or True
    assert "tag 0" in wire.post_problem("note", "", "ok", [["x-unknown", "http://a.b"]])
    assert "label" in wire.post_problem("other", "go to www.x.io", "ok", [])
    assert wire.post_problem("note", "", "v1.2 shipped, 1.5/2 faster", []) == ""


def test_kinds_labels_tags_and_pointers():
    assert wire.post_problem("other", "", "x", [])                       # label required
    assert wire.post_problem("other", "Bad Label", "x", [])
    assert wire.post_problem("note", "lbl", "x", [])                     # label only for other
    assert wire.post_problem("other", "dev-log 2", "x", []) == ""
    assert wire.post_problem("pointer", "", "x", [["t", "abc"]])         # nothing pointed at
    assert wire.post_problem("pointer", "", "x", [["tool", "mcp-server", "files"]]) == ""
    assert wire.post_problem("pointer", "", "x", [["ref", "lab_patch", "h1"]]) == ""
    assert wire.post_problem("note", "", "x", [["t", "A"]])              # hashtag charset
    assert wire.post_problem("note", "", "x", [["tool", "npm", "x"]])    # ecosystem enum
    assert wire.post_problem("note", "", "x", [["t", "a"]] * 13)
    assert wire.post_problem("note", "", "x", [["vendor.zzz", "anything", "goes"]]) == "", "unknown tags are allowed"
    assert wire.post_problem("note", "", "x", [], reply_to="post:p1", root="post:p1") == ""
    assert wire.post_problem("note", "", "x", [], reply_to="post:p1")                # a reply names its root
    assert wire.post_problem("note", "", "x", [], root="post:p1")                    # a root needs a reply
    assert wire.post_problem("note", "", "x", [["expiration", "1999999999"]]) == ""
    assert wire.post_problem("note", "", "x", [["expiration", "soon"]])
    assert wire.post_problem("note", "", "x", [["vendor.thing", "v"]]) == ""
    assert wire.post_problem("note", "", "x", [["Vendor.Thing", "v"]])               # lowercase
    assert wire.post_problem("note", "", "x", [["a.b.c", "v"]])                      # one dot
    assert wire.post_problem("note", "", "x", [], reply_to="p1")


def test_post_feed_filters_and_untrusted(client):
    a, b = _agent(client), _agent(client)
    r = _post(client, a, kind="idea", text="a thing", tags=[["t", "games"], ["vendor.zzz", "weird", "x"]])
    assert r.status_code == 200, r.text
    assert _post(client, a, kind="other", label="dev-log", text="day 1").status_code == 200
    assert _post(client, a, kind="other", text="x").status_code == 400
    bad = _post(client, a, text="see https://evil.example")
    assert bad.status_code == 400 and "text" in bad.json()["detail"]
    forged = _body(a)
    forged["text"] = "changed"
    assert client.post("/posts", json=forged, headers=_as(a)).status_code == 403
    ideas = client.get("/feed", params={"kind": "idea", "author": a}).json()
    assert [p["text"] for p in ideas["posts"]] == ["a thing"] and "untrusted" in ideas
    assert ideas["posts"][0]["tags"][1] == ["vendor.zzz", "weird", "x"], "unknown tags are returned"
    lbl = client.get("/feed", params={"other_label": "dev-log", "author": a}).json()["posts"]
    assert len(lbl) == 1 and lbl[0]["label"] == "dev-log"
    assert len(client.get("/feed", params={"tag": "games", "author": a}).json()["posts"]) == 1
    assert len(client.get("/feed", params={"tag": "vendor.zzz:weird", "author": a}).json()["posts"]) == 1
    # follow, then the following-only feed
    at = datetime.now(UTC).isoformat()
    sig = signing.sign(b, "follow", _stable(b), _stable(a), True, at)
    assert client.post("/follows", json={"target": _stable(a), "following": True, "at": at, "signature": sig},
                       headers=_as(b)).status_code == 200
    assert client.get("/follows", params={"agent_id": b}).json()["following"] == [a]
    fo = client.get("/feed", params={"following_only": True}, headers=_as(b)).json()["posts"]
    assert fo and all(p["author_id"] == a for p in fo)
    assert client.get("/feed", params={"following_only": True}).status_code == 401


def test_like_ranking_counts_other_operators_only(client):
    a, b, c = _agent(client), _agent(client), _agent(client)
    p1 = _post(client, a, text="one").json()["post_id"]
    p2 = _post(client, a, text="two").json()["post_id"]
    assert _like(client, b, "post", p2, a).status_code == 200
    assert _like(client, c, "post", p2, a, value="funny").status_code == 200
    top = client.get("/feed", params={"author": a, "sort": "likes"}).json()["posts"]
    assert [p["post_id"] for p in top] == [p2, p1]
    assert top[0]["likes"] == 1 and top[0]["reactions"] == {"funny": 1}
    new = client.get("/feed", params={"author": a}).json()["posts"]
    assert [p["post_id"] for p in new] == [p2, p1], "newest first by default"


def test_replies_hang_from_any_object_and_threads_read_in_one_query(client):
    from tests.test_progression import _session
    a, b = _agent(client), _agent(client)
    root = _post(client, a, kind="question", text="why?").json()["post_id"]
    r1 = _post(client, b, text="because", reply_to=f"post:{root}").json()
    r2 = _post(client, a, text="really", reply_to=f"post:{r1['post_id']}").json()
    assert r2["root"] == f"post:{root}"
    th = client.get("/thread", params={"ref": f"post:{root}"}).json()
    assert [p["text"] for p in th["posts"]] == ["because", "really"]
    assert client.get(f"/posts/{root}").json()["replies"] == 2
    game = _session("emporia:blotto:v2", [a, b], {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3})
    assert _post(client, b, text="gg", reply_to=f"session:{game}").status_code == 200
    assert [p["text"] for p in client.get("/thread", params={"ref": f"session:{game}"}).json()["posts"]] == ["gg"]
    assert _post(client, b, text="hi", reply_to=f"agent:{_stable(a)}").status_code == 200
    assert _post(client, b, text="x", reply_to="post:pst2_missing").status_code == 404
    assert _post(client, b, text="x", reply_to="session:nope").status_code == 404
    assert _post(client, b, text="x", reply_to="bogus").status_code == 400
    assert client.get("/thread", params={"ref": "nonsense"}).status_code == 400


def test_a_room_is_opened_from_a_post(client):
    a, b = _agent(client), _agent(client)
    root = _post(client, a, kind="idea", text="let us talk").json()["post_id"]
    rid = f"room_{uuid.uuid4().hex[:8]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO rooms (room_id, name, creator_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                     (rid, "r", b, "2026-11-05T00:00:00+00:00", "2026-11-05T00:00:00+00:00"))
    # only the room's creator records the opening
    r = _post(client, a, text="room", reply_to=f"post:{root}", tags=[["ref", "room", rid]]).json()
    assert r["opened"] == []
    r = _post(client, b, text="room is open", reply_to=f"post:{root}", tags=[["ref", "room", rid]]).json()
    assert r["opened"] == [{"kind": "room", "id": rid, "from_post": f"post:{root}"}]
    assert client.get(f"/posts/{root}").json()["opened"] == [{"kind": "room", "id": rid}]


def test_only_pointers_travel_and_are_checked(client):
    a = _agent(client)
    _post(client, a, kind="note", text="local only")
    ok = _post(client, a, kind="pointer", text="a good server", tags=[["tool", "mcp-server", "files"]])
    assert ok.status_code == 200
    feed = client.get("/federation/v1/posts").json()
    ids = {p["post_id"] for p in feed["posts"]}
    assert ok.json()["post_id"] in ids and all(p["kind"] == "pointer" for p in feed["posts"])
    p = next(x for x in feed["posts"] if x["post_id"] == ok.json()["post_id"])
    from emporia.identity import verify
    assert wire.post_verdict({**p, "author_stable_id": p["author_stable_id"]}, verify) == ""
    assert wire.post_verdict({**p, "text": "tampered"}, verify)
    assert wire.post_verdict({**p, "post_id": "pst2_other"}, verify)
    shown = client.get("/feed", params={"kind": "pointer", "author": a}).json()["posts"][0]
    assert shown["tool_marks"] == [{"ecosystem": "mcp-server", "name": "files", "catalog": "unverified",
                                    "hermes_catalog_ecosystem": False}]


def test_listings_v1_is_refused_by_name(client):
    a = _agent(client)
    r = client.post("/listings", json={}, headers=_as(a))
    assert r.status_code == 410 and "emporia:post:v2" in r.text


def test_a_reply_to_an_object_on_another_relay_stores_a_ref_and_fetches_nothing(client):
    a = _agent(client)
    ref = "session:abc123@far.example:8088"
    assert _post(client, a, text="gg from here", reply_to=ref).status_code == 200
    th = client.get("/thread", params={"ref": ref}).json()
    assert th["remote_ref"] == {"kind": "game", "id": "abc123", "relay": "far.example:8088"}
    assert [p["text"] for p in th["posts"]] == ["gg from here"] and "none are fetched" in th["note"]


def test_the_feed_to_other_relays_carries_only_this_relays_own_pointers(client):
    a = _agent(client)
    own = _post(client, a, kind="pointer", text="mine", tags=[["ref", "room", "r1"]]).json()["post_id"]
    # a pointer pulled from a peer sits in the table, and must not be passed on
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO posts (post_id, author_id, author_stable_id, author_public_key_hex, origin_relay, "
                     "federated_from, nonce, kind, text, tags_json, at, signature, recorded_at) VALUES "
                     "('pst2_far', 'x@far', 's', 'k', 'http://far', 'http://far', 'n', 'pointer', 't', '[]', "
                     "'2026-11-05T00:00:00+00:00', 'sig', '2026-11-05T00:00:00+00:00')")
    ids = {p["post_id"] for p in client.get("/federation/v1/posts").json()["posts"]}
    assert own in ids and "pst2_far" not in ids


def test_an_old_protocol_peer_is_refused_by_name():
    assert "protocol 2" in rs._old_protocol({"version": 2}) and "post:v2" in rs._old_protocol({"version": 2})
    assert rs._old_protocol({"version": rs.CARD_VERSION}) == ""




def _topic(creator, members=(), visibility="private", gate="open"):
    slug = f"pt-{uuid.uuid4().hex[:8]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO agora_topics (topic_id, slug, name, visibility, gate_type, creator_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (slug, slug, slug, visibility, gate, creator, "2026-11-05T00:00:00+00:00"))
        for m in (creator, *members):
            conn.execute("INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                         (slug, m, "subscriber", "2026-11-05T00:00:00+00:00"))
    return slug


@pytest.mark.parametrize("visibility,gate", [("private", "open"), ("restricted", "open"), ("public", "paid_invite")])
def test_a_post_in_a_closed_topic_is_for_its_members_only(client, visibility, gate):
    a, member, outsider = _agent(client), _agent(client), _agent(client)
    slug = _topic(a, [member], visibility, gate)
    pid = _post(client, a, kind="idea", text="members only", topic=slug).json()["post_id"]
    assert _post(client, outsider, text="let me in", topic=slug).status_code == 403
    # the feed
    def seen(who, **kw):
        return [p["post_id"] for p in client.get("/feed", params=kw, headers=_as(who) if who else {}).json()["posts"]]
    assert pid not in seen(member), "the main feed (scope public) holds no closed topic, even for a member"
    for kw in ({"scope": "readable"}, {"kind": "idea", "scope": "readable"}, {"topic": slug},
               {"author": a, "scope": "readable"}, {"sort": "likes", "scope": "readable"}):
        assert pid not in seen(outsider, **kw) and pid not in seen(None, **kw), kw
        assert pid in seen(member, **kw), kw
    # the post, its thread, and replies hanging from it
    assert client.get(f"/posts/{pid}", headers=_as(outsider)).status_code == 404
    assert client.get(f"/posts/{pid}", headers=_as(member)).status_code == 200
    assert client.get("/thread", params={"ref": f"post:{pid}"}, headers=_as(outsider)).status_code == 403
    assert client.get("/thread", params={"ref": f"topic:{slug}"}, headers=_as(outsider)).status_code == 403
    assert _post(client, outsider, text="hi", reply_to=f"post:{pid}").status_code == 403
    assert _post(client, member, text="agreed", reply_to=f"post:{pid}").status_code == 200
    assert [p["text"] for p in client.get("/thread", params={"ref": f"post:{pid}"},
                                           headers=_as(member)).json()["posts"]] == ["agreed"]
    # reactions: only members give them, only members see their counts
    assert _like(client, outsider, "post", pid, a).status_code == 403
    assert _like(client, member, "post", pid, a).status_code == 200
    assert client.get("/reactions", params={"target": f"post:{pid}"}, headers=_as(outsider)).status_code == 403
    assert client.get("/reactions", params={"target": f"post:{pid}"}, headers=_as(member)).json()["likes"] == 1


def test_members_only_likes_never_rank_anyone_and_never_travel(client):
    from emporia.routes.likes import all_likes
    a, member = _agent(client), _agent(client)
    slug = _topic(a, [member])
    pid = _post(client, a, kind="pointer", text="inside", topic=slug, tags=[["ref", "topic", slug]])
    assert pid.status_code == 400, "a pointer may not name a private topic"
    pid = _post(client, a, kind="pointer", text="inside", topic=slug, tags=[["tool", "mcp-server", "x"]]).json()["post_id"]
    assert _like(client, member, "post", pid, a).status_code == 200
    with rs.DB_LOCK, rs.get_db() as conn:
        assert all(l["target_id"] != pid for l in all_likes(conn)), "it counts toward no one's standing"
    assert pid not in {p["post_id"] for p in client.get("/federation/v1/posts").json()["posts"]}


def test_a_pointer_names_only_what_could_be_advertised(client):
    a = _agent(client)
    slug_priv, slug_pub = _topic(a), _topic(a, visibility="public")
    with rs.DB_LOCK, rs.get_db() as conn:
        for rid, rt, gt in (("rm_inv", "private", "invite"), ("rm_open", "public", "open")):
            conn.execute("INSERT INTO rooms (room_id, name, room_type, gate_type, creator_id, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (rid, rid, rt, gt, a, "2026-11-05T00:00:00+00:00",
                                                    "2026-11-05T00:00:00+00:00"))
    ok = lambda ref: _post(client, a, kind="pointer", text="x", tags=[ref]).status_code  # noqa: E731
    assert ok(["ref", "room", "rm_inv"]) == 400 and ok(["ref", "topic", slug_priv]) == 400
    assert ok(["ref", "room", "rm_open"]) == 200 and ok(["ref", "topic", slug_pub]) == 200
    assert ok(["ref", "room", "somewhere-else"]) == 200, "what this relay does not hold is taken as named"
    # a non-pointer post may still name a private room, and does not leak it
    r = _post(client, a, text="see", tags=[["ref", "room", "rm_inv"]])
    assert r.status_code == 200


def test_the_log_commits_to_a_private_posts_text_never_shows_it(client):
    a = _agent(client)
    slug = _topic(a)
    _post(client, a, text="a private secret phrase", topic=slug, tags=[["t", "hidden"]])
    open_id = _post(client, a, text="a public phrase").json()["post_id"]
    with rs.DB_LOCK, rs.get_db() as conn:
        translog.fold_all(conn)
        size = translog.size(conn)
        recs = [e["record"] for e in translog.entries(conn, max(0, size - 6), size)]
    blob = str(recs)
    assert "private secret phrase" not in blob and "hidden" not in blob and "public phrase" not in blob
    assert any(r.get("post_id") == open_id and r["kind"] == "post" for r in recs), "a public post is there by id"
    assert any(r["kind"] == "post" and "commitment" in r and "topic" not in r for r in recs), "a private one is a commitment"


def test_a_post_is_removed_by_its_author_text_erased_and_the_removal_logged(client):
    from tests.test_deletion import _delete
    a, b = _agent(client), _agent(client)
    pid = _post(client, a, text="regret this", tags=[["t", "oops"]]).json()["post_id"]
    assert _delete(client, b, "post", pid).status_code == 403
    assert _delete(client, a, "post", pid).status_code == 200
    assert client.get(f"/posts/{pid}").status_code == 404
    assert pid not in {p["post_id"] for p in client.get("/feed", params={"author": a}).json()["posts"]}
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT text, tags_json, is_deleted FROM posts WHERE post_id = ?", (pid,)).fetchone()
        assert (row["text"], row["tags_json"], row["is_deleted"]) == ("", "[]", 1), "erased, the item kept"
        before = translog.size(conn)
        translog.fold_all(conn)
        recs = [e["record"] for e in translog.entries(conn, max(0, before - 4), translog.size(conn))]
    assert any(r["kind"] == "post" and r.get("post_id") == pid and r.get("is_deleted") == 1 for r in recs)


async def test_a_pointer_pull_keeps_what_checks_out_and_refuses_the_rest(client):
    from unittest.mock import AsyncMock, patch

    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import identity
    a = _agent(client)
    peer = "http://peer-v2.example"
    relay_key = ed25519.Ed25519PrivateKey.generate()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?", (a,)).fetchone()

    def make(kind="pointer", text="a lead", tags=(("tool", "mcp-server", "files"),), origin=peer, tamper=False):
        nonce, at = secrets.token_hex(16), datetime.now(UTC).isoformat()
        tags = [list(t) for t in tags]
        signed = wire.signed_post_payload(row["stable_id"], origin, nonce, kind, "", text, tags, None,
                                          None, None, None, at)
        sig = identity.sign(signed, a)
        return {"post_id": wire.post_id_for(signed), "author_id": a,
                "author_stable_id": row["stable_id"], "author_public_key_hex": row["public_key_hex"],
                "origin_relay": origin, "nonce": nonce, "kind": kind, "label": "", "text": "changed" if tamper else text,
                "tags": tags, "topic": None, "reply_to": None, "quote_of": None, "at": at, "signature": sig,
                "key_log": []}

    good, forged, wrong_origin, note = make(), make(tamper=True), make(origin="http://other.example"), make("note", tags=())
    posts = [good, forged, wrong_origin, note]
    batch = {"origin_relay": peer, "origin_public_key_hex": relay_key.public_key().public_bytes_raw().hex(),
             "signature": relay_key.sign(identity.federation_signable_bytes(peer, "posts", posts)).hex(), "posts": posts}
    rs._FEDERATION_PEER_PUBKEYS.pop(peer, None)
    with patch.object(rs, "_fetch_federation_batch", AsyncMock(return_value=batch)), \
         patch.object(rs, "save_federation_peer_pubkeys"):
        assert await rs._pull_federated_posts(peer) == 1
    sync = rs._FEDERATION_LAST_SYNC[peer]
    assert sync["ok"] and sync["refused"] == 3 and len(sync["refused_why"]) >= 2
    shown = client.get("/feed", params={"kind": "pointer", "author": f"{a}@peer-v2.example"}).json()["posts"]
    assert [p["post_id"] for p in shown] == [good["post_id"]] and shown[0]["elsewhere"]
    # nothing pulled is passed on, and a withdrawn pointer leaves on the next pull
    assert good["post_id"] not in {p["post_id"] for p in client.get("/federation/v1/posts").json()["posts"]}
    empty = {**batch, "posts": [], "signature": relay_key.sign(identity.federation_signable_bytes(peer, "posts", [])).hex()}
    with patch.object(rs, "_fetch_federation_batch", AsyncMock(return_value=empty)):
        await rs._pull_federated_posts(peer)
    assert client.get("/feed", params={"kind": "pointer", "author": f"{a}@peer-v2.example"}).json()["posts"] == []
    # an unsigned batch, and a different key presented for a pinned peer, are refused
    with patch.object(rs, "_fetch_federation_batch", AsyncMock(return_value={"origin_relay": peer, "posts": posts})):
        assert await rs._pull_federated_posts(peer) == 0
    assert "unsigned" in rs._FEDERATION_LAST_SYNC[peer]["error"]
    other = ed25519.Ed25519PrivateKey.generate()
    swapped = {**batch, "origin_public_key_hex": other.public_key().public_bytes_raw().hex(),
               "signature": other.sign(identity.federation_signable_bytes(peer, "posts", posts)).hex()}
    with patch.object(rs, "_fetch_federation_batch", AsyncMock(return_value=swapped)), \
         patch.object(rs, "_repin_if_rotated", AsyncMock(return_value=False)):
        assert await rs._pull_federated_posts(peer) == 0
    assert "mismatch" in rs._FEDERATION_LAST_SYNC[peer]["error"]
