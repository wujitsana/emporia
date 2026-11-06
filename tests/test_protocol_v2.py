"""The protocol pass: identities that travel, provable threads, DM metadata, copies, reserved names, session
kinds, and the key-pinned operator, stable-id tokens and bound registration."""
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import crypto_box, identity, refs
from emporia import relay_server as rs
from emporia import wire
from tests.ids import stable_of
from tests.test_emporia import _register_keyed
from tests.test_posts_v2 import _agent, _as, _body, _post, _stable


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51293))


# ── A2: ids are hashes of what was signed; the root is signed and checked ───────────────────────

def test_a_post_id_is_the_hash_of_its_signed_payload(client):
    a = _agent(client)
    body = _body(a, text="provable")
    r = client.post("/posts", json=body, headers=_as(a))
    assert r.status_code == 200, r.text
    signed = wire.signed_post_payload(_stable(a), rs.RELAY_BASE_URL, body["nonce"], "note", "", "provable", [],
                                      None, None, None, None, body["at"])
    assert r.json()["post_id"] == wire.post_id_for(signed) and r.json()["post_id"].startswith("pst_")
    # a changed byte is another id
    assert wire.post_id_for({**signed, "text": "provable."}) != r.json()["post_id"]


def test_the_root_of_a_reply_is_signed_and_the_relay_only_checks_it(client):
    a, b = _agent(client), _agent(client)
    top = _post(client, a, text="start").json()["post_id"]
    reply = _post(client, b, text="re", reply_to=f"post:{top}")
    assert reply.status_code == 200 and reply.json()["root"] == f"post:{top}"
    deep = _post(client, a, text="re re", reply_to=f"post:{reply.json()['post_id']}")
    assert deep.status_code == 200 and deep.json()["root"] == f"post:{top}"
    # a root that is not the thread's: refused, never corrected
    wrong = _body(b, text="x", reply_to=f"post:{reply.json()['post_id']}", root=f"post:{reply.json()['post_id']}")
    r = client.post("/posts", json=wrong, headers=_as(b))
    assert r.status_code == 400 and "root must be" in r.json()["detail"]
    # a reply with no root, and a root with no reply, are not posts
    no_root = _body(b, text="x", reply_to=f"post:{top}")
    no_root.update(root=None)
    assert client.post("/posts", json=no_root, headers=_as(b)).status_code in (400, 403)
    assert wire.post_problem("note", "", "x", [], root="post:abc")
    # the root is inside the signature: moving it breaks the signature
    moved = _body(b, text="y", reply_to=f"post:{top}")
    moved["root"] = f"post:{reply.json()['post_id']}"
    assert client.post("/posts", json=moved, headers=_as(b)).status_code in (400, 403)
    # and the relay says what it would accept
    got = client.get("/roots", params={"ref": f"post:{reply.json()['post_id']}"}, headers=_as(a)).json()
    assert got["root"] == f"post:{top}"


def test_a_ref_carries_an_author_hint_and_threads_ignore_it(client):
    a, b = _agent(client), _agent(client)
    top = _post(client, a, text="start").json()["post_id"]
    hinted = f"post:{top}#{_stable(a)}"
    assert refs.parse(hinted)["author"] == _stable(a) and refs.text(hinted) == hinted
    assert refs.tag(hinted) == ["ref", "post", top, _stable(a)] and refs.parse(refs.tag(hinted)[1:])["author"] == _stable(a)
    assert refs.problem(f"post:{top}#nothex")
    r = _post(client, b, text="hinted reply", reply_to=hinted, root=hinted)
    assert r.status_code == 200, r.text
    thread = client.get("/thread", params={"ref": f"post:{top}"}).json()["posts"]
    assert [p["text"] for p in thread] == ["hinted reply"]
    assert refs.same_object(hinted, f"post:{top}") and not refs.problem({"kind": "agent", "id": _stable(a)})
    assert refs.problem("agent:somename")                # an agent ref is a stable id


def test_a_room_message_id_is_the_hash_of_its_signed_payload(client):
    from tests.test_rooms_v2 import _agent as room_agent
    from tests.test_rooms_v2 import _room, _say
    a, b = room_agent(client), room_agent(client)
    rid = _room(client, a, [b])
    first = _say(client, rid, a, "hello room")
    assert first.status_code == 200, first.text
    mid = first.json()["message_id"]
    assert mid.startswith("msg_") and len(mid) == 36
    reply = _say(client, rid, b, "hi", reply_to=f"message:{mid}")
    assert reply.status_code == 200 and reply.json()["root"] == f"message:{mid}"
    from emporia import room_post
    bad = room_post.canonical(room_post.body("note", "x", [], f"message:{mid}", root="message:nope"))
    assert _say(client, rid, b, "x", content=bad).status_code == 400


# ── A4: copies ────────────────────────────────────────────────────────────────────────────────

def _foreign_post(origin="http://far.example", age=timedelta(hours=5), text="from far away"):
    """A post a non-resident agent signed for `origin`, hours ago."""
    name = f"far_{uuid.uuid4().hex[:8]}"
    _, pub = identity.generate_or_load_keypair(name)
    stable = wire.stable_id_for(pub.hex())
    nonce, at = secrets.token_hex(16), (datetime.now(UTC) - age).isoformat()
    signed = wire.signed_post_payload(stable, origin, nonce, "note", "", text, [], None, None, None, None, at)
    return {"post_id": wire.post_id_for(signed), "author_id": name, "author_stable_id": stable,
            "author_public_key_hex": pub.hex(), "origin_relay": origin, "nonce": nonce, "kind": "note", "label": "",
            "text": text, "tags": [], "topic": None, "reply_to": None, "quote_of": None, "root": None, "at": at,
            "signature": identity.sign(signed, name), "key_log": []}


def test_a_copy_of_an_old_signed_post_is_kept_and_flagged(client, monkeypatch):
    me = _agent(client)
    post = _foreign_post()
    monkeypatch.delenv("EMPORIA_ACCEPT_COPIES", raising=False)
    assert client.post("/posts/copy", json={"post": post}, headers=_as(me)).status_code == 403    # off by default
    monkeypatch.setenv("EMPORIA_ACCEPT_COPIES", "1")
    # the freshness window of first submission does not apply: this post is five hours old
    r = client.post("/posts/copy", json={"post": post}, headers=_as(me))
    assert r.status_code == 200 and r.json()["copy"] is True and r.json()["kept"] is True, r.text
    shown = client.get("/feed", params={"elsewhere": "true"}).json()["posts"]
    mine = [p for p in shown if p["post_id"] == post["post_id"]]
    assert mine and mine[0]["copy"] is True and mine[0]["origin_relay"] == "http://far.example" and mine[0]["elsewhere"]
    assert wire.post_verdict(post, rs.verify, copy=True) == ""
    # the same record offered first-hand is refused: first submission has the window
    first_hand = _body(me, text="old")
    first_hand["at"] = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    assert client.post("/posts", json=first_hand, headers=_as(me)).status_code in (400, 403)
    # a changed byte, another key, a resident agent, a topic or a sealed post are not copies
    forged = {**post, "text": "changed"}
    assert client.post("/posts/copy", json={"post": forged}, headers=_as(me)).status_code == 400
    resident = {**_foreign_post(), "author_stable_id": _stable(me)}
    assert client.post("/posts/copy", json={"post": resident}, headers=_as(me)).status_code == 400
    assert client.post("/posts/copy", json={"post": _foreign_post(origin=rs.RELAY_BASE_URL)},
                       headers=_as(me)).status_code == 400
    again = client.post("/posts/copy", json={"post": post}, headers=_as(me)).json()
    assert again["kept"] is False


def test_a_copy_is_checked_against_the_key_the_agent_had_then():
    post = _foreign_post()
    other = identity.generate_or_load_keypair(f"far_{uuid.uuid4().hex[:6]}")[1].hex()
    # a key that is not the stable id's, with no log leading to it
    assert wire.key_verdict_at(post["author_stable_id"], other, [], rs.verify, post["at"])
    assert wire.key_verdict_at(post["author_stable_id"], post["author_public_key_hex"], [], rs.verify, post["at"]) == ""


# ── A5: reserved names ────────────────────────────────────────────────────────────────────────

def test_expiration_is_reserved_and_an_expired_post_leaves_the_reads(client):
    a = _agent(client)
    past = str(int(time.time()) - 60)
    gone = _post(client, a, text="expired offer", tags=[["expiration", past]])
    live = _post(client, a, text="live offer", tags=[["expiration", str(int(time.time()) + 3600)]])
    assert gone.status_code == 200 and live.status_code == 200
    texts = [p["text"] for p in client.get("/feed", params={"author": a}).json()["posts"]]
    assert texts == ["live offer"]
    assert wire.expiration_of([["t", "x"], ["expiration", "5"]]) == 5 and wire.expiration_of([["t", "x"]]) is None


def test_third_party_tag_names_are_vendor_dot_name_and_core_names_are_never_dotted():
    ok = ["t", "ref", "tool", "p", "expiration", "acme.score", "a-b.c_d"]
    assert all(wire.tag_name_problem(n) == "" for n in ok)
    for bad in ("Acme.score", "acme.Score", "acme.x.y", "score", ".x", "x.", "", "a b"):
        assert wire.tag_name_problem(bad), bad
    assert "." not in "".join(wire.CORE_TAGS)
    assert wire.post_problem("other", "acme.review", "x", []) == "" and wire.post_problem("other", "Acme.Review", "x", [])


# ── A3: padding ───────────────────────────────────────────────────────────────────────────────

def test_a_sealed_envelope_is_padded_to_a_size_bucket():
    priv = identity.generate_or_load_keypair  # noqa: F841 - keys come from identity below
    pub = identity.get_encryption_public_key_hex("pad_agent")
    sizes = set()
    for n in (1, 40, 200, 700, 1500, 5000):
        env = crypto_box.seal("x" * n, {"pad_agent": pub}, b"aad")
        size = len(json.dumps(env).encode())
        assert size == crypto_box.padded_size(size) and size >= crypto_box.PAD_MIN
        sizes.add(size)
        assert crypto_box.parse_envelope(json.dumps(env))      # the relay still takes it
    assert len(sizes) < 6, "short messages share a bucket"
    assert crypto_box.padded_size(1025) == 2048 and crypto_box.padded_size(10) == 1024
    from emporia.wire import MAX_CONTENT_BYTES
    assert crypto_box.padded_size(MAX_CONTENT_BYTES - 5) == MAX_CONTENT_BYTES


def test_a_sealed_rooms_messages_are_stored_at_the_minute(client):
    from tests.test_rooms_v2 import _agent as room_agent
    from tests.test_rooms_v2 import _room
    a, b = room_agent(client), room_agent(client)
    _room(client, a, [b])
    from emporia import rooms as rooms_db
    assert rooms_db.coarse_minute(datetime(2026, 11, 6, 12, 34, 56, 789, tzinfo=UTC)) == "2026-11-06T12:34:00+00:00"


# ── A4: relay list v2 ─────────────────────────────────────────────────────────────────────────

def test_a_relay_list_names_the_agent_by_stable_id_and_may_give_roles(client):
    a = _agent(client)
    stamp = datetime.now(UTC).isoformat()
    home, backup = "http://127.0.0.1:8088", "https://backup.example"
    roles = {backup: ["backup", "read"], home: ["home", "read", "write"]}
    payload = wire.signed_relay_list_payload(_stable(a), home, [], "", stamp, [backup], roles)
    assert payload["type"] == "emporia:relay-list:v2" and payload["stable_id"] == _stable(a)
    body = {"home": home, "visits": [], "backups": [backup], "roles": roles, "a2a_url": "", "updated_at": stamp,
            "signature": identity.sign(payload, a)}
    r = client.post(f"/agents/{a}/relay-list", json=body, headers=_as(a))
    assert r.status_code == 200 and r.json()["relay_list"]["roles"][backup] == ["backup", "read"], r.text
    for bad in ({home: ["owner"]}, {"https://elsewhere.example": ["read"]}, {backup: ["home"]}, {home: ["backup"]}):
        assert wire.relay_roles_problem(home, [], [backup], bad), bad
    assert wire.relay_roles_problem(home, [], [backup], None) == ""
    later = datetime.now(UTC).isoformat()
    unsigned_roles = wire.signed_relay_list_payload(_stable(a), home, [], "", later, [backup])
    r = client.post(f"/agents/{a}/relay-list", headers=_as(a), json={
        "home": home, "visits": [], "backups": [backup], "roles": roles, "a2a_url": "", "updated_at": later,
        "signature": identity.sign(unsigned_roles, a)})
    assert r.status_code == 403, "roles are inside the signature"


# ── A6: session kind ──────────────────────────────────────────────────────────────────────────

def test_every_module_is_a_game_session_and_refs_say_session():
    from emporia.module_sdk import MODULE_REGISTRY, list_available_modules, session_kind
    assert MODULE_REGISTRY and all(m["kind"] == "game" for m in list_available_modules())
    assert session_kind("emporia:chess:v1") == "game" and session_kind("nobody:nothing:v1") == "game"
    assert refs.text("game:abc") == "session:abc" and refs.parse("session:abc")["kind"] == "session"
    from emporia.module_sdk import SESSION_KIND_RE
    assert SESSION_KIND_RE.fullmatch("game") and SESSION_KIND_RE.fullmatch("acme.workshop")
    assert not SESSION_KIND_RE.fullmatch("Game")


def test_a_session_record_carries_its_kind(client):
    with rs.DB_LOCK, rs.get_db() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "kind" in cols


# ── A7: the operator is a key; tokens carry the stable id; registration is bound ────────────────

def test_a_squatter_on_the_operators_name_gets_no_operator_powers(client, monkeypatch):
    op_name = f"op_{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv("EMPORIA_AGENT_ID", op_name)
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(op_name))
    # someone else asks for the name on a fresh relay: refused, the name is reserved for the operator's key
    squat_key = f"squat_{uuid.uuid4().hex[:6]}"
    _, pub = identity.generate_or_load_keypair(squat_key)
    ch = client.post("/agents/challenge").json()
    relay = wire.relay_origin(rs.RELAY_BASE_URL)
    sig = identity.sign(wire.signed_registration_payload(ch["nonce"], relay, op_name, pub.hex()), squat_key)
    r = client.post("/agents/register", json={"agent_id": op_name, "public_key_hex": pub.hex(),
                                              "challenge_id": ch["challenge_id"], "challenge_signature": sig,
                                              "relay": relay})
    assert r.status_code == 403 and "reserved" in r.json()["detail"], r.text
    # the operator's own key takes it, and is the operator
    _register_keyed(client, op_name)
    assert rs._relay_operator_id() == op_name
    # a renamed or other agent with the same *name* is nothing: the check is the stable id
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(squat_key))
    assert rs._relay_operator_id() == ""      # the squatter's key is not registered, so nobody is the operator


def test_house_names_are_reserved_too(client, monkeypatch):
    name = f"house_{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", name)
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", "ef" * 32)       # house names are reserved once an operator is pinned
    assert "house name" in rs.reserved_name_problem(name, "ab" * 32)
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", "ab" * 32)
    assert rs.reserved_name_problem(name, "ab" * 32) == ""
    assert rs.reserved_name_problem("anyone_else", "cd" * 32) == ""


def test_a_token_dies_with_its_name(client):
    name = f"tok_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, name)
    now = time.time()
    token = rs._jwt_encode({"agent_id": name, "stable_id": rs.AGENT_REGISTRY.get_stable_id(name),
                            "relay_id": rs.relay_id(), "iat": int(now), "issued": now, "exp": int(now + 600)})
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/operator/whoami", headers=auth).status_code == 200
    # the name is released and another key claims it: the old token is refused
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET agent_id = ? WHERE agent_id = ?", (name + "_renamed", name))
    other = f"{name}_k2"
    _register_keyed(client, other)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET agent_id = ? WHERE agent_id = ?", (name, other))
    assert client.get("/operator/whoami", headers=auth).status_code == 401
    # a token with no stable id is refused for a registered name
    bare = rs._jwt_encode({"agent_id": name, "relay_id": rs.relay_id(), "iat": int(now), "issued": now,
                           "exp": int(now + 600)})
    assert client.get("/operator/whoami", headers={"Authorization": f"Bearer {bare}"}).status_code == 401


def test_a_registration_signature_is_good_for_one_relay_one_name_and_one_key(client):
    name = f"reg_{uuid.uuid4().hex[:6]}"
    _, pub = identity.generate_or_load_keypair(name)
    relay = wire.relay_origin(rs.RELAY_BASE_URL)

    def attempt(signed_relay, signed_name, claimed_name=None):
        ch = client.post("/agents/challenge").json()
        sig = identity.sign(wire.signed_registration_payload(ch["nonce"], signed_relay, signed_name, pub.hex()), name)
        return client.post("/agents/register", json={
            "agent_id": claimed_name or signed_name, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
            "challenge_signature": sig, "relay": signed_relay})

    # a signature made for relay A is refused here
    assert attempt("https://relay-a.example", name).status_code == 403
    # a signature for another agent id is refused for this one
    ch = client.post("/agents/challenge").json()
    sig = identity.sign(wire.signed_registration_payload(ch["nonce"], relay, "someone_else", pub.hex()), name)
    r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": pub.hex(),
                                              "challenge_id": ch["challenge_id"], "challenge_signature": sig,
                                              "relay": relay})
    assert r.status_code == 403
    # the old v1 bytes (a bare prefix and nonce, as hex) are not a registration
    ch = client.post("/agents/challenge").json()
    bare = identity.sign_raw(b"emporia:challenge:v1\n" + ch["nonce"].encode(), name)
    r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": pub.hex(),
                                              "challenge_id": ch["challenge_id"], "challenge_signature": bare,
                                              "relay": relay})
    assert r.status_code == 403
    assert attempt(relay, name).status_code == 200


def test_one_helper_decides_the_operator_and_a_rename_does_not_change_it(client):
    from emporia import operators
    name = f"opr_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, name)
    stable = rs.AGENT_REGISTRY.get_stable_id(name)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT agent_id, nous_user_id, stable_id FROM authorized_agents WHERE agent_id = ?",
                           (name,)).fetchone()
    assert operators.operator_of_row(row) == f"key:{stable}"
    assert operators.operator_of("acct-1", stable, name) == "nous:acct-1" and operators.verified("acct-1")
    assert operators.operator_of(None, stable, "other-name") == f"key:{stable}"      # a rename changes nothing
    from emporia.routes.standing import _who
    assert _who()[name].operator == f"key:{stable}"


def test_model_and_client_are_declared_tags_inside_the_signature(client):
    a = _agent(client)
    tags = [["model", "some-model-1"], ["client", "hermes/1.2"]]
    r = _post(client, a, text="said by a model", tags=tags)
    assert r.status_code == 200, r.text
    got = client.get("/feed", params={"author": a}).json()
    assert got["posts"][0]["tags"] == tags and "untrusted" in got
    assert wire.post_problem("note", "", "x", [["model", "a", "b"]])            # exactly two elements
    assert wire.post_problem("note", "", "x", [["model", "m" * 65]])            # at most 64 characters
    assert wire.post_problem("note", "", "x", [["model", "see https://x.example/m"]])   # plain text, no links
    from emporia import room_post
    assert room_post.problem(room_post.body("note", "hi", tags)) == ""       # a room message or DM takes them too
    forged = _body(a, text="x", tags=tags)
    forged["tags"] = [["model", "another-model"]]
    assert client.post("/posts", json=forged, headers=_as(a)).status_code == 403    # inside the signature


def test_the_signing_rule_for_a_post_takes_the_root_of_a_reply():
    """The signer's own check of a post must know `root`, or no reply could ever be signed through it."""
    from emporia import signing
    name = f"sg_{uuid.uuid4().hex[:6]}"
    args = (wire.stable_id_for(identity.generate_or_load_keypair(name)[1].hex()), "http://relay.example",
            secrets.token_hex(16), "note", "", "a reply", [], None, "post:pst_abc", None, "post:pst_abc",
            datetime.now(UTC).isoformat())
    assert signing.sign(name, "post", *args)
    with pytest.raises(signing.Refused):
        signing.sign(name, "post", *(*args[:10], None, args[11]))      # a reply with no root is not signed
