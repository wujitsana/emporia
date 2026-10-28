"""P141: only a session's players may link a room to it — the session's
visibility follows the linked room, so an outsider's public room made a
private session readable by everyone."""
import uuid

from fastapi.testclient import TestClient

from emporia import relay_server as rs
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
