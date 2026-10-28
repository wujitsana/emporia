"""P243: a room's settings, invites and kicks are its creator's signed acts, so
a relay cannot change a room's terms or add or remove a member on its own."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.identity import get_public_key_hex, sign, verify_room_acts
from emporia.relay_server import app
from emporia.wire import signed_room_invite_payload
from tests.test_emporia import _register_keyed, _signed_invite, _signed_kick, _signed_room


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51430))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _room(client, creator: str, **settings) -> str:
    r = client.post("/rooms", json=_signed_room(creator, {"name": f"{creator} room", **settings}),
                    headers=_as(creator))
    assert r.status_code == 200, r.text
    return r.json()["room_id"]


def test_a_room_must_be_signed_over_its_settings(client):
    _register_keyed(client, "sr_creator")
    assert client.post("/rooms", json={"name": "unsigned"}, headers=_as("sr_creator")).status_code == 422
    body = _signed_room("sr_creator", {"name": "Free room"})
    body["entry_fee_cents"] = 500                  # changed after signing
    assert client.post("/rooms", json=body, headers=_as("sr_creator")).status_code == 403
    body = _signed_room("sr_creator", {"name": "Once"})
    assert client.post("/rooms", json=body, headers=_as("sr_creator")).status_code == 200
    assert client.post("/rooms", json=body, headers=_as("sr_creator")).status_code == 409   # nonce used once


def test_a_member_can_tell_when_the_relay_changed_a_rooms_settings(client):
    _register_keyed(client, "sr_honest")
    room_id = _room(client, "sr_honest", room_type="private", gate_type="invite", encrypted=False)
    key = get_public_key_hex("sr_honest")
    room = client.get(f"/rooms/{room_id}", headers=_as("sr_honest")).json()
    assert verify_room_acts(room, key)["settings"] is True
    with rs.DB_LOCK, rs.get_db() as conn:          # the relay quietly makes it a paid room
        conn.execute("UPDATE rooms SET gate_type = 'paid', entry_fee_cents = 900 WHERE room_id = ?", (room_id,))
    room = client.get(f"/rooms/{room_id}", headers=_as("sr_honest")).json()
    assert verify_room_acts(room, key)["settings"] is False


def test_invites_and_kicks_are_the_creators_signed_acts(client):
    for a in ("sr_owner", "sr_guest"):
        _register_keyed(client, a)
    room_id = _room(client, "sr_owner", room_type="private", gate_type="invite")
    # Signed by the wrong agent, or too long ago: refused.
    assert client.post(f"/rooms/{room_id}/invite", json=_signed_invite("sr_guest", room_id, {"invitee_id": "sr_guest"}),
                       headers=_as("sr_owner")).status_code == 403
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert client.post(f"/rooms/{room_id}/invite", json={
        "invitee_id": "sr_guest", "at": stale,
        "signature": sign(signed_room_invite_payload(room_id, "sr_guest", stale), "sr_owner")},
        headers=_as("sr_owner")).status_code == 400

    assert client.post(f"/rooms/{room_id}/invite", json=_signed_invite("sr_owner", room_id, {"invitee_id": "sr_guest"}),
                       headers=_as("sr_owner")).status_code == 200
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "sr_guest"}, headers=_as("sr_guest")).status_code == 200
    assert client.post(f"/rooms/{room_id}/kick", json=_signed_kick("sr_owner", room_id, {"target_agent_id": "sr_guest"}),
                       headers=_as("sr_owner")).status_code == 200

    room = client.get(f"/rooms/{room_id}", headers=_as("sr_owner")).json()
    check = verify_room_acts(room, get_public_key_hex("sr_owner"))
    assert check["settings"] is True
    assert [(a["act"], a["target"], a["verified"]) for a in check["acts"]] == [
        ("invite", "sr_guest", True), ("kick", "sr_guest", True)]


def test_the_check_room_tool_and_the_signing_tools(client, monkeypatch):
    from emporia import mcp_server
    for a in ("sr_mcp", "sr_mcp_guest"):
        _register_keyed(client, a)
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda method, url, agent_id="", **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers=_as(agent_id or "sr_mcp"),
        **{k: v for k, v in kw.items() if k in ("json", "params")}))
    made = mcp_server.create_room("MCP room", "sr_mcp", room_type="private", gate_type="invite")
    assert made["success"], made
    room_id = made["room"]["room_id"]
    assert mcp_server.invite_to_room(room_id, "sr_mcp_guest", "sr_mcp")["success"]
    checked = mcp_server.check_room(room_id, agent_id="sr_mcp")
    assert checked["success"] and checked["settings"] is True
    assert [a["verified"] for a in checked["acts"]] == [True]
