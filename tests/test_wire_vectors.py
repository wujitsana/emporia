"""The wire vectors the dashboard's tests read (dashboard/src/wire.vectors.json), and the room message shape they
describe, held to a live relay.

A change to wire.py, rooms.py or identity.py's signing bytes fails the first test until the file is regenerated
(`.venv/bin/python scripts/wire_vectors.py > dashboard/src/wire.vectors.json`); the dashboard's vitest then fails
until its TypeScript follows."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emporia import identity, wire
from emporia import relay_server as rs
from tests.test_rooms_v2 import _agent, _room, _say

ROOT = Path(__file__).resolve().parent.parent
VECTORS = ROOT / "dashboard" / "src" / "wire.vectors.json"


def _generator():
    spec = importlib.util.spec_from_file_location("wire_vectors", ROOT / "scripts" / "wire_vectors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51347))


def test_the_committed_vectors_are_what_the_generator_makes_byte_for_byte():
    made = json.dumps(_generator().build(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    assert VECTORS.read_text(encoding="utf-8") == made, (
        "wire.py changed: regenerate with `.venv/bin/python scripts/wire_vectors.py > dashboard/src/wire.vectors.json`")


def test_the_generator_is_deterministic():
    gen = _generator()
    assert gen.build() == gen.build()


def test_every_vector_kind_has_a_valid_and_a_tampered_case_and_one_with_non_ascii():
    v = json.loads(VECTORS.read_text(encoding="utf-8"))
    for kind in ("room_message", "dm_message", "room_chain_head", "dm_chain_head"):
        verdicts = {c["python_verified"] for c in v[kind]}
        assert verdicts == {True, False}, kind
    assert {c["python_ok"] for c in v["chain_hash"]} == {True, False}
    assert any(not c["content"].isascii() for c in v["chain_hash"])
    assert any(not c["message"]["content"].isascii() for c in v["room_message"])
    assert any(not c["message"]["content"].isascii() for c in v["dm_message"])
    assert any(not m["content"].isascii() for c in v["room_chain_head"] for m in c["messages"])
    assert any(c["message"]["sender_stable_id"] == "" and c["python_verified"] for c in v["room_message"])
    assert all(v["removed_chain"]["python_link_ok"])
    assert len({c["message_id"] for c in v["message_id"]}) == len(v["message_id"])
    for c in v["message_id"]:
        assert wire.message_id_for(c["payload"]) == c["message_id"]


def test_a_room_message_read_back_from_the_relay_rebuilds_the_vector_payload_shape(client):
    a, b = _agent(client), _agent(client)
    rid = _room(client, a, [b])
    text = "a line — 世界 🎉"
    sent = _say(client, rid, a, text)
    assert sent.status_code == 200, sent.text
    served = client.get(f"/rooms/{rid}/messages", headers={"x-emporia-agent-id": a}).json()["messages"]
    row = next(m for m in served if m["message_id"] == sent.json()["message_id"])
    pub = identity.get_public_key_hex(a)

    assert row["sender_stable_id"] and row["sender_stable_id"] == wire.stable_id_for(pub)
    assert identity.verify_room_message(rid, row, pub) is True

    # what the dashboard rebuilds from the served row is exactly the vector payload shape, and it verifies
    payload = wire.signed_room_message_payload(rid, row["content"], row["msg_type"], row["nonce"], row["sender_stable_id"])
    assert list(payload) == ["type", "room_id", "stable_id", "content", "msg_type", "nonce"]
    assert payload["type"] == wire.ROOM_MESSAGE_TYPE == "emporia:room-message:v2"
    assert identity.verify(payload, row["signature"], pub)
    vector = json.loads(VECTORS.read_text(encoding="utf-8"))["room_message"][0]
    vector_payload = wire.signed_room_message_payload(
        vector["room_id"], vector["message"]["content"], vector["message"]["msg_type"], vector["message"]["nonce"],
        vector["message"]["sender_stable_id"])
    assert set(payload) == set(vector_payload)
    # the v1 shape (no type, no stable id) does not verify
    v1 = {"room_id": rid, "content": row["content"], "msg_type": row["msg_type"], "nonce": row["nonce"]}
    assert not identity.verify(v1, row["signature"], pub)
