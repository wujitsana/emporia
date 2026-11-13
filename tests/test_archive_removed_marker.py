"""read_archive says what the relay removed; the archive file stays as it was."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import identity, mcp_server
from emporia import relay_server as rs
from emporia import rooms as rooms_db
from tests.test_emporia import _register_keyed, _signed_room


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51322))


def _h(a):
    return {"x-emporia-agent-id": a}


def test_archive_read_marks_a_message_the_relay_removed_and_leaves_the_file(client, monkeypatch, tmp_path):
    me = f"ax_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, me)
    rid = client.post("/rooms", json=_signed_room(me, {"name": "arch " + uuid.uuid4().hex[:5]}),
                      headers=_h(me)).json()["room_id"]
    keep = rooms_db.post_message(rid, me, "still here", db_path=rs.DATABASE_PATH)
    gone = rooms_db.post_message(rid, me, "text removed", db_path=rs.DATABASE_PATH)
    with rs.DB_LOCK, rs.get_db() as conn:   # as emporia.deletion leaves it: the row stays, deleted_at is set
        conn.execute("UPDATE room_messages SET deleted_at = '2026-01-01T00:00:00+00:00' WHERE message_id = ?",
                     (gone.message_id,))
        conn.commit()
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(identity, "KEY_DIR", keys)
    monkeypatch.setattr(mcp_server, "_base", lambda: "http://testserver")
    monkeypatch.setattr(mcp_server, "_bearer_request",
                        lambda method, url, **kw: getattr(client, method)(
                            "/" + url.split("/", 3)[3], headers=_h(me),
                            **{k: v for k, v in kw.items() if k in ("json", "params")}))
    d = keys / "archive" / "testserver" / "rooms"
    d.mkdir(parents=True)
    ghost = "m_" + uuid.uuid4().hex     # archived once, no longer served at all
    lines = [{"message_id": m, "sender_id": me, "content": c, "created_at": f"2026-01-0{i}T00:00:00+00:00"}
             for i, (m, c) in enumerate([(keep.message_id, "still here"), (gone.message_id, "text removed"),
                                         (ghost, "vanished")], 1)]
    f = d / f"{rid}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    before = f.read_text()

    got = mcp_server.read_archive("room", rid, agent_id=me)
    assert got["success"]
    assert {m["message_id"]: m["on_relay"] for m in got["messages"]} == {
        keep.message_id: "present", gone.message_id: "removed", ghost: "removed"}
    assert f.read_text() == before

    # the relay cannot be asked: unknown, never a guess
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    again = mcp_server.read_archive("room", rid, agent_id=me)
    assert {m["on_relay"] for m in again["messages"]} == {"unknown"}
    assert f.read_text() == before
