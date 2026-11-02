"""P273: leaving, taking back, blocking — and a sender that seals to a room's
members only as its signed acts put them there."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import identity, wire
from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia.relay_server import app
from tests.test_emporia import _publish_enc_key, _register_keyed, _signed_room

RELAY = "http://testserver"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51273))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _keyed(client, *agents: str) -> None:
    for a in agents:
        _register_keyed(client, a)
        _publish_enc_key(client, a)


def _room(client, creator: str, **settings) -> str:
    r = client.post("/rooms", json=_signed_room(creator, {"name": f"{creator} room", **settings}), headers=_as(creator))
    assert r.status_code == 200, r.text
    return r.json()["room_id"]


def _signed(make, ident: str, agent: str, **extra) -> dict:
    at = datetime.now(UTC).isoformat()
    return {"at": at, "signature": identity.sign(make(ident, agent, at), agent), **extra}


def _recipients(client, room_id: str, me: str) -> dict:
    room = client.get(f"/rooms/{room_id}", headers=_as(me)).json()
    bundle = client.get(f"/rooms/{room_id}/encryption-keys", headers=_as(me)).json()["keys"]
    return identity.room_recipients(room, bundle, me, me, RELAY)


def test_a_room_join_is_signed_by_the_joining_agent(client):
    from conftest import header_shim_off, signed_room_join_body
    from tests.test_emporia import bearer_for
    with header_shim_off():
        c = TestClient(app, client=("127.0.0.1", 51274))
        _register_keyed(c, "rj273_host")
        _register_keyed(c, "rj273_guest")
        room_id = c.post("/rooms", json=_signed_room("rj273_host", {"name": "rj273"}),
                         headers=bearer_for(c, "rj273_host")).json()["room_id"]
        h = bearer_for(c, "rj273_guest")
        assert c.post(f"/rooms/{room_id}/join", json={"agent_id": "rj273_guest"}, headers=h).status_code == 401
        forged = signed_room_join_body(room_id, "rj273_host") | {"agent_id": "rj273_guest"}
        assert c.post(f"/rooms/{room_id}/join", json=forged, headers=h).status_code == 403
        other = signed_room_join_body("room_elsewhere", "rj273_guest")
        assert c.post(f"/rooms/{room_id}/join", json=other, headers=h).status_code == 403
        assert c.post(f"/rooms/{room_id}/join", json=signed_room_join_body(room_id, "rj273_guest"),
                      headers=h).status_code == 200
    acts = client.get(f"/rooms/{room_id}", headers=_as("rj273_host")).json()["signed_acts"]
    assert [(a["act"], a["target"]) for a in acts] == [("create", None), ("join", "rj273_guest")]


def test_a_sender_refuses_a_member_the_relay_added_on_its_own(client):
    _keyed(client, "rm273_host", "rm273_guest", "rm273_puppet")
    room_id = _room(client, "rm273_host", room_type="private", gate_type="invite", encrypted=True)
    at = datetime.now(UTC).isoformat()
    assert client.post(f"/rooms/{room_id}/invite", headers=_as("rm273_host"), json={
        "invitee_id": "rm273_guest", "at": at,
        "signature": identity.sign(wire.signed_room_invite_payload(room_id, "rm273_guest", at), "rm273_host"),
    }).status_code == 200
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "rm273_guest"},
                       headers=_as("rm273_guest")).status_code == 200
    assert set(_recipients(client, room_id, "rm273_host")) == {"rm273_host", "rm273_guest"}

    # the relay writes a member in with no invite and no join of its own
    rooms_db.add_member(room_id, "rm273_puppet", db_path=rs.DATABASE_PATH)
    with pytest.raises(identity.RoomMembershipUnproven, match="rm273_puppet"):
        _recipients(client, room_id, "rm273_guest")
    check = client.get(f"/rooms/{room_id}", headers=_as("rm273_host")).json()
    keys = {a: identity.get_public_key_hex(a) for a in ("rm273_host", "rm273_guest", "rm273_puppet")}
    assert identity.verify_room_acts(check, keys["rm273_host"], keys.get)["unproven_members"] == ["rm273_puppet"]

    # its own signed join is not enough in an invite room: the creator never invited it
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO room_acts (act_id, room_id, act, target, by_agent, at, payload_json, signature) "
                     "VALUES ('act_puppet273', ?, 'join', 'rm273_puppet', 'rm273_puppet', ?, ?, ?)",
                     (room_id, at := datetime.now(UTC).isoformat(),
                      __import__("json").dumps(wire.signed_room_join_payload(room_id, "rm273_puppet", at)),
                      identity.sign(wire.signed_room_join_payload(room_id, "rm273_puppet", at), "rm273_puppet")))
    with pytest.raises(identity.RoomMembershipUnproven, match="rm273_puppet"):
        _recipients(client, room_id, "rm273_host")


def test_leaving_a_room_and_coming_back(client):
    _keyed(client, "lr273_host", "lr273_guest")
    room_id = _room(client, "lr273_host", encrypted=True)
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "lr273_guest"},
                       headers=_as("lr273_guest")).status_code == 200
    leave = _signed(wire.signed_room_leave_payload, room_id, "lr273_guest")
    assert client.post(f"/rooms/{room_id}/leave", json=leave | {"signature": leave["signature"][::-1]},
                       headers=_as("lr273_guest")).status_code == 403
    assert client.post(f"/rooms/{room_id}/leave", json=leave, headers=_as("lr273_guest")).status_code == 200
    assert client.post(f"/rooms/{room_id}/leave", json=_signed(wire.signed_room_leave_payload, room_id, "lr273_guest"),
                       headers=_as("lr273_guest")).status_code == 404
    assert set(_recipients(client, room_id, "lr273_host")) == {"lr273_host"}
    assert client.post(f"/rooms/{room_id}/leave", json=_signed(wire.signed_room_leave_payload, room_id, "lr273_host"),
                       headers=_as("lr273_host")).status_code == 400          # the creator stays
    # a leave is not a kick: joining again is a new signed join
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "lr273_guest"},
                       headers=_as("lr273_guest")).status_code == 200
    assert set(_recipients(client, room_id, "lr273_host")) == {"lr273_host", "lr273_guest"}


def test_a_member_from_before_signed_joins_signs_one_by_joining_again(client):
    _keyed(client, "old273_host", "old273_guest")
    room_id = _room(client, "old273_host", encrypted=True)
    rooms_db.add_member(room_id, "old273_guest", db_path=rs.DATABASE_PATH)     # as a pre-P273 join left it
    with pytest.raises(identity.RoomMembershipUnproven):
        _recipients(client, room_id, "old273_host")
    r = client.post(f"/rooms/{room_id}/join", json={"agent_id": "old273_guest"}, headers=_as("old273_guest"))
    assert r.json()["status"] == "already_member"
    assert set(_recipients(client, room_id, "old273_host")) == {"old273_host", "old273_guest"}


def test_an_unused_room_invite_can_be_taken_back(client):
    _keyed(client, "ui273_host", "ui273_guest", "ui273_other")
    room_id = _room(client, "ui273_host", room_type="private", gate_type="invite")

    def invite(who):
        at = datetime.now(UTC).isoformat()
        return client.post(f"/rooms/{room_id}/invite", headers=_as("ui273_host"), json={
            "invitee_id": who, "at": at,
            "signature": identity.sign(wire.signed_room_invite_payload(room_id, who, at), "ui273_host")})

    def revoke(who, signer="ui273_host"):
        at = datetime.now(UTC).isoformat()
        return client.post(f"/rooms/{room_id}/invite/revoke", headers=_as(signer), json={
            "invitee_id": who, "at": at,
            "signature": identity.sign(wire.signed_room_uninvite_payload(room_id, who, at), signer)})
    assert invite("ui273_guest").status_code == 200 and invite("ui273_other").status_code == 200
    assert revoke("ui273_guest", "ui273_other").status_code == 403
    assert revoke("ui273_guest").status_code == 200
    assert revoke("ui273_guest").status_code == 404
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "ui273_guest"},
                       headers=_as("ui273_guest")).status_code == 403
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "ui273_other"},
                       headers=_as("ui273_other")).status_code == 200
    assert revoke("ui273_other").status_code == 409                       # joined: that is a kick


def test_a_player_leaves_a_game_not_yet_started(client):
    for a in ("sl273_host", "sl273_a", "sl273_b"):
        _register_keyed(client, a)
    sid = client.post("/sessions", json={"module_type": "emporia:two-thirds:v1", "config": {},
                                         "payment_rules": {"mode": "free"}},
                      headers=_as("sl273_host")).json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": "sl273_a"}, headers=_as("sl273_a")).status_code == 200
    body = _signed(wire.signed_session_leave_payload, sid, "sl273_a")
    assert client.post(f"/sessions/{sid}/leave", json=body, headers=_as("sl273_b")).status_code == 404   # not a player
    assert client.post(f"/sessions/{sid}/leave", json=_signed(wire.signed_session_leave_payload, sid, "sl273_host"),
                       headers=_as("sl273_host")).status_code == 400                                     # abandon it
    assert client.post(f"/sessions/{sid}/leave", json=body | {"signature": body["signature"][::-1]},
                       headers=_as("sl273_a")).status_code == 403
    r = client.post(f"/sessions/{sid}/leave", json=body, headers=_as("sl273_a"))
    assert r.status_code == 200, r.text
    s = client.get(f"/sessions/{sid}", headers=_as("sl273_host")).json()
    assert s["participants"] == ["sl273_host"] and s["status"] == "waiting"
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT signature FROM session_leaves WHERE session_id = ?",
                            (sid,)).fetchone()["signature"] == body["signature"]
        assert not conn.execute("SELECT 1 FROM participants WHERE session_id = ? AND agent_id = 'sl273_a'",
                                (sid,)).fetchone()
    # and it can join again
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": "sl273_a"}, headers=_as("sl273_a")).status_code == 200


def test_a_vote_can_be_taken_back(client):
    _register_keyed(client, "v273_author")
    _register_keyed(client, "v273_voter")
    from tests.test_emporia import _signed_post
    slug = client.post("/agoras/topics", json={"name": "v273 topic", "description": "d", "visibility": "public",
                                               "gate_type": "open", "creator_id": "v273_author"},
                       headers=_as("v273_author")).json()["slug"]
    post = client.post(f"/agoras/topics/{slug}/posts", json=_signed_post("v273_author", slug, {"title": "t", "content": "c"}),
                       headers=_as("v273_author")).json()["post_id"]
    assert client.post(f"/agoras/posts/{post}/vote", json={"value": 1}, headers=_as("v273_voter")).json()["vote_score"] == 1
    assert client.post(f"/agoras/posts/{post}/vote", json={"value": 0}, headers=_as("v273_voter")).json()["vote_score"] == 0
    assert client.post(f"/agoras/posts/{post}/vote", json={"value": 0}, headers=_as("v273_voter")).json()["vote_score"] == 0
    assert client.post(f"/agoras/posts/{post}/vote", json={"value": 2}, headers=_as("v273_voter")).status_code == 400


def test_an_unused_topic_invite_can_be_taken_back(client):
    _register_keyed(client, "ti273_owner")
    _register_keyed(client, "ti273_guest")
    slug = client.post("/agoras/topics", json={"name": "ti273 topic", "description": "d", "visibility": "private",
                                               "gate_type": "invite", "creator_id": "ti273_owner"},
                       headers=_as("ti273_owner")).json()["slug"]
    assert client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": "ti273_guest"},
                       headers=_as("ti273_owner")).status_code == 200
    assert client.delete(f"/agoras/topics/{slug}/invite/ti273_guest", headers=_as("ti273_guest")).status_code == 403
    assert client.delete(f"/agoras/topics/{slug}/invite/ti273_guest", headers=_as("ti273_owner")).status_code == 200
    assert client.delete(f"/agoras/topics/{slug}/invite/ti273_guest", headers=_as("ti273_owner")).status_code == 404
    assert client.post(f"/agoras/topics/{slug}/subscribe", json={"agent_id": "ti273_guest"},
                       headers=_as("ti273_guest")).status_code == 403


def test_a_blocked_sender_never_reaches_the_inbox(client):
    for a in ("b273_me", "b273_pest", "b273_friend"):
        _register_keyed(client, a)
    assert client.post("/agents/b273_me/dm-blocks", json={"agent": "b273_pest"},
                       headers=_as("b273_pest")).status_code == 403          # only its own
    thread = client.post("/dm/start", json={"to_agent": "b273_me", "encrypted": False},
                         headers=_as("b273_pest")).json()["thread_id"]
    assert client.post("/agents/b273_me/dm-blocks", json={"agent": "b273_pest"},
                       headers=_as("b273_me")).status_code == 200
    assert client.post("/agents/b273_me/dm-blocks", json={"agent": "b273_me"},
                       headers=_as("b273_me")).status_code == 400
    assert client.post("/dm/start", json={"to_agent": "b273_me", "encrypted": False},
                       headers=_as("b273_pest")).status_code == 403
    sent = client.post(f"/dm/{thread}/send", json={"content": "hello again", "msg_type": "chat",
                                                    "nonce": wire.new_message_nonce(), "signature": "x"},
                       headers=_as("b273_pest"))
    assert sent.status_code == 403 and "takes no DMs from you" in sent.text
    assert client.post("/dm/start", json={"to_agent": "b273_me", "encrypted": False},
                       headers=_as("b273_friend")).status_code == 200
    assert client.post("/agents/b273_me/dm-blocks", json={"agent": "far@other.example"},
                       headers=_as("b273_me")).status_code == 200
    listed = client.get("/agents/b273_me/dm-blocks", headers=_as("b273_me")).json()["blocked"]
    assert [b["agent"] for b in listed] == ["b273_pest", "far@other.example"]
    from emporia.routes.agents import dm_blocked
    assert dm_blocked("b273_me", "FAR@other.example") and not dm_blocked("b273_me", "far@another.example")
    assert client.delete("/agents/b273_me/dm-blocks/b273_pest", headers=_as("b273_me")).status_code == 200
    assert client.post("/dm/start", json={"to_agent": "b273_me", "encrypted": False},
                       headers=_as("b273_pest")).status_code == 200


def test_an_old_join_replayed_after_a_leave_is_refused(client):
    """P273: a join is good once — replayed after a leave it would put back a
    member (or seat a player) who left."""
    from conftest import signed_join_body, signed_room_join_body
    _keyed(client, "rp273_host", "rp273_guest")
    room_id = _room(client, "rp273_host")
    old = signed_room_join_body(room_id, "rp273_guest")
    assert client.post(f"/rooms/{room_id}/join", json=old, headers=_as("rp273_guest")).status_code == 200
    assert client.post(f"/rooms/{room_id}/leave", json=_signed(wire.signed_room_leave_payload, room_id, "rp273_guest"),
                       headers=_as("rp273_guest")).status_code == 200
    assert client.post(f"/rooms/{room_id}/join", json=old, headers=_as("rp273_guest")).status_code == 409

    _register_keyed(client, "rp273_third")
    sid = client.post("/sessions", json={"module_type": "emporia:two-thirds:v1", "config": {},
                                         "payment_rules": {"mode": "free"}},
                      headers=_as("rp273_host")).json()["session_id"]
    seat = signed_join_body(sid, "rp273_guest")
    assert client.post(f"/sessions/{sid}/join", json=seat, headers=_as("rp273_guest")).status_code == 200
    assert client.post(f"/sessions/{sid}/leave", json=_signed(wire.signed_session_leave_payload, sid, "rp273_guest"),
                       headers=_as("rp273_guest")).status_code == 200
    assert client.post(f"/sessions/{sid}/join", json=seat, headers=_as("rp273_guest")).status_code == 409
