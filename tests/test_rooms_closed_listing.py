"""GET /rooms: a closed room with no messages is left out; closed with messages is listed with the flag."""
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia import wire
from emporia.identity import sign
from tests.test_emporia import _register_keyed, _signed_room, _stable_of


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51321))


def h(n):
    return {"x-emporia-agent-id": n}


def _room(client, owner, tag, **kw):
    body = _signed_room(owner, {"name": f"{tag} {uuid.uuid4().hex[:5]}", **kw})
    return client.post("/rooms", json=body, headers=h(owner)).json()["room_id"]


def _close(client, owner, rid):
    at = datetime.now(UTC).isoformat()
    sig = sign(wire.signed_room_close_payload(rid, at, _stable_of(owner)), owner)
    r = client.post(f"/rooms/{rid}/close", json={"at": at, "signature": sig}, headers=h(owner))
    assert r.status_code == 200, r.text


def _ids(client, who, path="/rooms?limit=200"):
    return {r["room_id"]: r for r in client.get(path, headers=h(who)).json()["rooms"]}


def test_closed_empty_rooms_are_hidden_closed_rooms_with_messages_are_flagged(client):
    me = f"cl_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, me)
    empty, talked, open_ = (_room(client, me, t) for t in ("empty", "talked", "open"))
    rooms_db.post_message(talked, me, "a line", db_path=rs.DATABASE_PATH)
    _close(client, me, empty)
    _close(client, me, talked)

    default = _ids(client, me)
    assert empty not in default
    assert default[talked]["closed"] is True and default[talked]["message_count"] == 1
    assert default[open_]["closed"] is False and default[open_]["message_count"] == 0

    everything = _ids(client, me, "/rooms?limit=200&closed=all")
    assert everything[empty]["closed"] is True and everything[empty]["message_count"] == 0
    # still readable by id with its signed acts; nothing was deleted
    got = client.get(f"/rooms/{empty}", headers=h(me))
    assert got.status_code == 200 and got.json()["closed"] is True
    assert [a["act"] for a in got.json()["signed_acts"]][-1] == "close"
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rooms WHERE room_id = ?", (empty,)).fetchone()[0] == 1


def test_private_room_visibility_is_unchanged(client):
    owner, stranger = (f"pv_{uuid.uuid4().hex[:6]}" for _ in range(2))
    for a in (owner, stranger):
        _register_keyed(client, a)
    priv = _room(client, owner, "priv", room_type="private", gate_type="invite")
    assert priv in _ids(client, owner) and priv not in _ids(client, stranger)
    assert priv not in _ids(client, stranger, "/rooms?limit=200&closed=all")
