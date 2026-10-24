"""P149: a private session's free text is sealed to its players — the relay
stores and relays ciphertext only, as with DMs."""
import json
import uuid

from fastapi.testclient import TestClient

from emporia import crypto_box, identity
from emporia import mcp_server as m
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _enc_pair, _register_keyed, _signed_action_body


def _seal(sid, field, text, players):
    return json.dumps(crypto_box.seal(
        text, {p: identity.get_encryption_public_key_hex(p) for p in players},
        crypto_box.session_aad(sid, field)))


def _purchase(client):
    buyer, seller = f"pb_{uuid.uuid4().hex[:6]}", f"ps_{uuid.uuid4().hex[:6]}"
    _enc_pair(client, buyer, seller)
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1", "creator_agent_id": buyer,
                                         "config": {"description": "write the report"},
                                         "payment_rules": {"mode": "free", "stake_per_participant": "0"}},
                      headers={"x-emporia-agent-id": buyer}).json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": seller},
                       headers={"x-emporia-agent-id": seller}).status_code == 200
    return sid, buyer, seller


def _act(client, sid, who, action, payload):
    return client.post(f"/sessions/{sid}/action",
                       json=_signed_action_body(client, sid, who, action, payload, "Doing the job as agreed"))


def test_a_private_purchase_is_sealed_and_the_relay_holds_no_plaintext():
    client = TestClient(app, client=("127.0.0.1", 51225))
    sid, buyer, seller = _purchase(client)
    s = client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": buyer}).json()
    assert s["sealed"] is True and "deliverable" in s["private_text_fields"]
    assert _act(client, sid, seller, "accept", {}).status_code == 200

    plain = _act(client, sid, seller, "deliver", {"deliverable": "The secret report."})
    assert plain.status_code == 400 and "must be sealed" in plain.text

    sealed = _seal(sid, "deliverable", "The secret report.", [buyer, seller])
    assert _act(client, sid, seller, "deliver", {"deliverable": sealed}).status_code == 200
    stored = json.dumps(rs.load_session(sid)) + json.dumps(
        client.get(f"/sessions/{sid}/actions", headers={"x-emporia-agent-id": buyer}).json())
    assert "The secret report." not in stored, "the relay holds ciphertext only"

    # The buyer's tools open it, on the buyer's machine.
    opened = m._open_session(buyer, client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": buyer}).json())
    assert opened["state"]["data"]["deliverable"] == "The secret report."
    assert "deliverable" in opened["opened_fields"]


def test_sealed_for_one_player_only_is_refused():
    client = TestClient(app, client=("127.0.0.1", 51226))
    sid, buyer, seller = _purchase(client)
    _act(client, sid, seller, "accept", {})
    only_me = _seal(sid, "deliverable", "x", [seller])
    r = _act(client, sid, seller, "deliver", {"deliverable": only_me})
    assert r.status_code == 409 and buyer in r.text, "the buyer could never read it"


def test_a_public_game_is_not_sealed():
    client = TestClient(app, client=("127.0.0.1", 51227))
    a, b = f"pg_{uuid.uuid4().hex[:6]}", f"ph_{uuid.uuid4().hex[:6]}"
    _enc_pair(client, a, b)
    sid = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a},
                      headers={"x-emporia-agent-id": a}).json()["session_id"]
    assert client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": a}).json()["sealed"] is False


def test_without_published_keys_a_private_session_stays_open():
    """Like DMs (P122): sealing needs every player's key; until then, plaintext."""
    client = TestClient(app, client=("127.0.0.1", 51228))
    buyer, seller = f"pk_{uuid.uuid4().hex[:6]}", f"pl_{uuid.uuid4().hex[:6]}"
    for x in (buyer, seller):
        _register_keyed(client, x)
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1", "creator_agent_id": buyer},
                      headers={"x-emporia-agent-id": buyer}).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers={"x-emporia-agent-id": seller})
    assert client.get(f"/sessions/{sid}", headers={"x-emporia-agent-id": buyer}).json()["sealed"] is False


def test_a_dispute_reason_never_reaches_the_public_rationale():
    from emporia.wire import SEALED_DISPUTE_RATIONALE
    assert "reason" not in SEALED_DISPUTE_RATIONALE.split("the reason")[0], "names no reason text"
