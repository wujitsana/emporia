"""The public read surface: what the unauthenticated socket is told, sockets that outlive their authority,
a lapsed room member's reads, the lab proof fetch budget, the beacon cache route, topic slugs, and no-store
on signed-in answers."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from emporia import relay_server as rs
from tests.test_emporia import _register_keyed, _signed_invite, _signed_kick, _signed_room, rooms_db_path  # noqa: F401
from tests.test_posts_v2 import _post


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51871))


def _as(a):
    return {"x-emporia-agent-id": a}


def _agent(client, tag="ps"):
    a = f"{tag}_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    return a


def _stable(agent):
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]


def _token(agent):
    return rs._jwt_encode({"agent_id": agent, "stable_id": _stable(agent), "relay_id": rs.relay_id(),
                           "exp": int(time.time() + 600), "jti": uuid.uuid4().hex, "issued": time.time()})


def _subproto(tok):
    return ["emporia.v1", f"emporia.token.{tok}"]


def _private_room(client, creator, members=()):
    r = client.post("/rooms", json=_signed_room(creator, {"name": f"r-{uuid.uuid4().hex[:6]}",
                                                          "room_type": "private", "gate_type": "invite"}),
                    headers=_as(creator))
    assert r.status_code == 200, r.text
    rid = r.json()["room_id"]
    for m in members:
        assert client.post(f"/rooms/{rid}/invite", json=_signed_invite(creator, rid, {"invitee_id": m}),
                           headers=_as(creator)).status_code == 200
        assert client.post(f"/rooms/{rid}/join", json={"agent_id": m}, headers=_as(m)).status_code == 200
    return rid


def _topic(client, creator, **kw):
    from conftest import signed_topic_body
    body = signed_topic_body(creator, {"name": f"t {uuid.uuid4().hex[:6]}", "visibility": "public", **kw})
    r = client.post("/agoras/topics", json=body, headers=_as(creator))
    assert r.status_code == 200, r.text
    return r.json()["slug"]


@pytest.fixture
def global_events(monkeypatch):
    events = []

    async def record(event):
        events.append(event)
    monkeypatch.setattr(rs, "broadcast_global", record)
    return events


# ---- M1 ----------------------------------------------------------------------

def test_m1_a_public_topic_post_is_still_broadcast(client, global_events):
    a = _agent(client)
    slug = _topic(client, a)
    assert _post(client, a, topic=slug, text="open words").status_code == 200
    assert [e["preview"] for e in global_events if e["type"] == "post_created"] == ["open words"]


def test_m1_a_paid_topics_post_is_not_broadcast_to_the_anonymous_socket(client, global_events):
    a = _agent(client)
    slug = _topic(client, a)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE agora_topics SET gate_type = 'paid_invite', entry_fee_cents = 100 WHERE slug = ?",
                     (slug,))
    r = _post(client, a, topic=slug, text="members only secret")
    assert r.status_code == 200, r.text
    assert not [e for e in global_events if e["type"] == "post_created"]


def test_m1_a_post_replying_into_a_private_room_is_not_broadcast(client, global_events):
    a = _agent(client)
    rid = _private_room(client, a)
    r = _post(client, a, text="reply into the room", reply_to=f"room:{rid}")
    assert r.status_code == 200, r.text
    assert not [e for e in global_events if e["type"] == "post_created"]


# ---- M2 ----------------------------------------------------------------------

def test_m2_a_kicked_member_loses_its_room_socket(client):
    owner, guest = _agent(client, "own"), _agent(client, "gst")
    rid = _private_room(client, owner, [guest])
    with client.websocket_connect(f"/ws/rooms/{rid}", subprotocols=_subproto(_token(guest))) as ws:
        assert ws.receive_json()["type"] == "room_init"
        assert client.post(f"/rooms/{rid}/kick", json=_signed_kick(owner, rid, {"target_agent_id": guest}),
                           headers=_as(owner)).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_m2_a_revoked_agent_socket_gets_nothing_more(client):
    owner, guest = _agent(client, "own"), _agent(client, "gst")
    rid = _private_room(client, owner, [guest])
    with client.websocket_connect(f"/ws/agent/{guest}", subprotocols=_subproto(_token(guest))) as ws:
        rs.revoke_all_tokens(_stable(guest))        # sign-out everywhere / key recovery
        assert client.post(f"/rooms/{rid}/kick", json=_signed_kick(owner, rid, {"target_agent_id": guest}),
                           headers=_as(owner)).status_code == 200      # pushes `kicked_from_room` to the guest
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# ---- M3 ----------------------------------------------------------------------

def test_m3_an_expired_paid_room_member_may_not_read(rooms_db_path):  # noqa: F811
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from emporia import rooms
    from emporia.entitlements import grant
    room = rooms.create_room("paid", "owner_x", room_type="private", gate_type="paid", entry_fee_cents=100,
                             access_duration_days=1, db_path=rooms_db_path)
    rooms.add_member(room.room_id, "payer_x", db_path=rooms_db_path)
    grant("room", room.room_id, "payer_x", "pi_x", duration_days=1, rail="x402", db_path=rooms_db_path)

    def row():
        c = sqlite3.connect(rooms_db_path)
        c.row_factory = sqlite3.Row
        try:
            return c.execute("SELECT * FROM rooms WHERE room_id = ?", (room.room_id,)).fetchone()
        finally:
            c.close()
    assert rooms.may_read(row(), "payer_x", rooms_db_path)
    c = sqlite3.connect(rooms_db_path)
    c.execute("UPDATE access_entitlements SET expires_at = ? WHERE resource_type='room' AND resource_id=?",
              ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), room.room_id))
    c.commit()
    c.close()
    assert not rooms.may_read(row(), "payer_x", rooms_db_path)
    assert rooms.may_read(row(), "owner_x", rooms_db_path), "the creator never lapses"


def test_m3_the_fan_out_skips_a_lapsed_member(client, monkeypatch):
    from emporia import rooms
    owner, payer = _agent(client, "own"), _agent(client, "pay")
    rid = _private_room(client, owner, [payer])
    pushed = []

    async def record(agent_id, event):
        pushed.append(agent_id)
    monkeypatch.setattr(rs, "broadcast_to_agent", record)
    monkeypatch.setattr(rooms, "is_member", lambda room_id, agent_id, db_path=None: agent_id != payer)
    from tests.test_emporia import _send_room_raw
    assert _send_room_raw(client, rid, owner, "hello").status_code == 200
    assert payer not in pushed


# ---- M4 ----------------------------------------------------------------------

def test_m4_lab_proof_fetches_are_capped_per_agent_per_day(monkeypatch, tmp_path):
    from emporia import lab_proof
    from emporia.routes import labs as lab_routes
    from tests.test_labs import Lab, post_lab, stable_of
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.setenv("EMPORIA_LAB_HOSTS", "file")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    lab_client = TestClient(rs.app, client=("127.0.0.1", 51872))
    lb = Lab(lab_client, tmp_path, monkeypatch)
    calls = []

    def fake_check(*a, **k):
        calls.append(a)
        return {"represents": False, "why": "stubbed", "unreachable": False}
    monkeypatch.setattr(lab_proof, "check", fake_check)
    codes = []
    for i in range(lab_routes.LAB_PROOFS_PER_DAY + 1):
        room = lb.room if i == 0 else __import__("emporia.rooms", fromlist=["x"]).create_room(
            f"x{i}", lb.creator, db_path=rs.DATABASE_PATH)
        r = post_lab(lab_client, lb.creator, f"{lb.slug}{i}", lb.repo, lb.commit, room.room_id,
                     [stable_of(lb.maint)], subject_ref=f"{lb.subject}{i}")
        codes.append(r.status_code)
    assert codes[:-1] == [422] * lab_routes.LAB_PROOFS_PER_DAY and codes[-1] == 429, codes
    assert len(calls) == lab_routes.LAB_PROOFS_PER_DAY


def test_m4_the_fetch_is_filtered_and_follows_no_redirects():
    from emporia import lab_proof
    assert "http.followRedirects=false" in lab_proof.git_flags(False)
    assert lab_proof.FETCH_BLOB_LIMIT == "--filter=blob:limit=1m"


# ---- M5 ----------------------------------------------------------------------

def test_m5_the_beacon_route_will_not_look_up_an_old_uncached_round(client, monkeypatch):
    from emporia.routes import beacon as beacon_route
    looked = []
    monkeypatch.setattr(beacon_route, "signature", lambda n: looked.append(n) or "ab" * 48)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM beacon_rounds")
    assert client.get("/beacon/12345").status_code == 404
    assert looked == []
    with rs.DB_LOCK, rs.get_db() as conn:     # a round a game stored is served whatever its age
        conn.execute("INSERT INTO beacon_rounds (round, signature, fetched_at) VALUES (12345, ?, 'x')", ("cd" * 48,))
    assert client.get("/beacon/12345").json()["signature"] == "cd" * 48


def test_m5_recent_rounds_are_looked_up_within_a_global_budget(client, monkeypatch):
    from emporia import rand
    from emporia.routes import beacon as beacon_route
    monkeypatch.setattr(beacon_route, "signature", lambda n: "ab" * 48)
    monkeypatch.setattr(beacon_route, "_lookups", [])
    monkeypatch.setattr(beacon_route, "LOOKUPS_PER_MINUTE", 3)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM beacon_rounds")
    now_round = rand.round_at(time.time()) - 5
    codes = [client.get(f"/beacon/{now_round - i}").status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


# ---- L5 ----------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["tpc_abc123", "has space", "a/b", "q?x", "A-b", "-lead", "x" * 65])
def test_l5_a_given_topic_slug_must_be_plain(client, slug):
    from emporia import identity, wire
    a = _agent(client)
    nonce = wire.new_message_nonce()
    sig = identity.sign(wire.signed_agora_topic_payload(nonce, slug, "n", "", "public", "open", 0, [], None, False), a)
    r = client.post("/agoras/topics", json={"name": "n", "slug": slug, "visibility": "public", "nonce": nonce,
                                            "signature": sig}, headers=_as(a))
    assert r.status_code == 400 and "slug" in r.text


def test_l5_a_generated_slug_is_accepted(client):
    assert _topic(client, _agent(client), name="Hello, World! " * 8)


# ---- L7 ----------------------------------------------------------------------

def test_l7_a_signed_in_answer_is_no_store_and_an_anonymous_one_is_not(client):
    a = _agent(client)
    tok = _token(a)
    plain = TestClient(rs.app, client=("127.0.0.1", 51873))
    assert plain.get("/health", headers={"Authorization": f"Bearer {tok}"}).headers["cache-control"] == "no-store"
    assert plain.get("/health", headers={"X-Emporia-Authorization": f"Bearer {tok}"}).headers[
        "cache-control"] == "no-store"
    assert plain.get("/health").headers.get("cache-control") != "no-store"
