"""P141: only a session's players may link a room to it — the session's
visibility follows the linked room, so an outsider's public room made a
private session readable by everyone."""
import uuid

from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_room


def test_an_outsider_cannot_make_a_private_session_public():
    client = TestClient(app, client=("127.0.0.1", 51223))
    buyer, outsider = (f"rl{i}_{uuid.uuid4().hex[:6]}" for i in range(2))
    for a in (buyer, outsider):
        _register_keyed(client, a)
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1", "creator_agent_id": buyer,
                                         "config": {"description": "private work"}},
                      headers={"x-emporia-agent-id": buyer}).json()["session_id"]
    assert client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": outsider}).status_code == 403
    r = client.post("/rooms", json=_signed_room(outsider, {"name": "peek", "room_type": "public", "linked_session_id": sid}),
                    headers={"x-emporia-agent-id": outsider})
    assert r.status_code == 403, r.text
    assert client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": outsider}).status_code == 403
    ok = client.post("/rooms", json=_signed_room(buyer, {"name": "our table", "room_type": "private", "linked_session_id": sid}),
                     headers={"x-emporia-agent-id": buyer})
    assert ok.status_code == 200, "a player may link a room to their own game"


def test_a_rooms_card_does_not_say_which_game_it_is_tied_to():
    """P277: a private room's card (an invitee's, or anyone's for an advertised
    room) carries the creator's signed settings, which named the linked game
    before; now they hold a salted commitment, checked by members against the
    id and salt they are served."""
    import json

    from emporia import identity, payee
    from tests.test_emporia import _signed_invite
    client = TestClient(app, client=("127.0.0.1", 51277))
    host, guest = (f"lk{i}_{uuid.uuid4().hex[:6]}" for i in range(2))
    for a in (host, guest):
        _register_keyed(client, a)
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1", "creator_agent_id": host,
                                         "config": {"description": "private work"}},
                      headers={"x-emporia-agent-id": host}).json()["session_id"]
    unsalted = _signed_room(host, {"name": "t", "room_type": "private", "gate_type": "invite", "linked_session_id": sid})
    unsalted.pop("linked_session_salt")
    assert client.post("/rooms", json=unsalted, headers={"x-emporia-agent-id": host}).status_code == 400
    room_id = client.post("/rooms", json=_signed_room(host, {
        "name": "our table", "room_type": "private", "gate_type": "invite", "linked_session_id": sid}),
        headers={"x-emporia-agent-id": host}).json()["room_id"]
    assert client.post(f"/rooms/{room_id}/invite", json=_signed_invite(host, room_id, {
        "invitee_id": guest, "invited_by": host}), headers={"x-emporia-agent-id": host}).status_code == 200

    card = client.get(f"/rooms/{room_id}", headers={"x-emporia-agent-id": guest}).json()
    assert card["card_only"] and sid not in json.dumps(card)
    assert card["signed_acts"][0]["payload"]["type"] == "emporia:room:v2"
    key = identity.get_public_key_hex(host)
    assert payee.room_terms(card, key)[0] == {host}

    full = client.get(f"/rooms/{room_id}", headers={"x-emporia-agent-id": host}).json()
    assert full["linked_session_id"] == sid and full["linked_session_salt"]
    assert identity.verify_room_acts(full, key)["settings"] is True
    assert identity.verify_room_acts({**full, "linked_session_id": "sess_other"}, key)["settings"] is False
