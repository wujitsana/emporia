"""Final LOW findings: FR-4a/4b (operator removal), FR-5a-e."""
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import autonomy, cli
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


def _h(a):
    return {"x-emporia-agent-id": a}


def _agent(client, tag):
    a = f"{tag}_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    return a


def _try_register(client, agent_id):
    from emporia.identity import get_public_key_hex
    return client.post("/agents/register", json={"agent_id": agent_id,
                                                  "public_key_hex": get_public_key_hex(agent_id)})


def _args(name):
    import argparse
    return argparse.Namespace(url=name)


# ---- FR-4a ---------------------------------------------------------------

def test_relay_remove_of_an_agent_that_already_left_still_bars_it(monkeypatch):
    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51801))
    a = _agent(client, "fr4a")
    agent_routes.retire(a)                      # its own leave: no removal row
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, default="": a)
    assert cli.cmd_relay_remove(_args(a)) == 0
    r = _try_register(client, a)
    assert r.status_code >= 400 and "removed" in r.text


# ---- FR-4b ---------------------------------------------------------------

def _open_challenge(client, creator):
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": creator},
                    headers=_h(creator))
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def test_operator_removal_ends_the_agents_open_sessions():
    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51802))
    a = _agent(client, "fr4b")
    sid = _open_challenge(client, a)
    agent_routes.retire(a, operator_removal=True)
    assert rs.load_session(sid)["status"] == "abandoned"
    assert _try_register(client, a).status_code >= 400


def test_an_agents_own_leave_still_waits_for_its_open_sessions():
    from fastapi import HTTPException

    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51803))
    a = _agent(client, "fr4b_own")
    _open_challenge(client, a)
    with pytest.raises(HTTPException) as e:
        agent_routes.retire(a)
    assert e.value.status_code == 409


# ---- FR-5a ---------------------------------------------------------------

@pytest.mark.parametrize("tool", ["kick_from_room", "kick_from_agora_topic"])
def test_kicks_are_refused_in_a_relay_turn(monkeypatch, tmp_path, tool):
    monkeypatch.setattr(autonomy, "store_dir", lambda: tmp_path)
    monkeypatch.setattr(autonomy, "data_dir", lambda: tmp_path)
    monkeypatch.delenv("EMPORIA_UNTRACKED_TURNS", raising=False)

    def turn(origin):
        (tmp_path / "autonomy.json").write_text(json.dumps({"turns": {"t": {
            "origin": origin, "sender": "eve", "at": time.time(), "writes": 0, "new_dms": []}}}))
    turn("relay")
    assert autonomy.guard(tool, {"target_agent_id": "x"}, "me")["refused_by"] == "emporia-autonomy"
    turn("rounds")
    assert autonomy.guard(tool, {"target_agent_id": "x"}, "me") is None


# ---- FR-5b ---------------------------------------------------------------

def test_commitments_file_cannot_leave_its_folder(monkeypatch, tmp_path):
    from emporia import identity, mcp_server
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path / "keys")
    p = mcp_server._commitments_file("../../evil", "sess_1")
    assert p.parent == tmp_path / "keys" / "commitments"
    with pytest.raises(ValueError):
        mcp_server._commitments_file("../..", "s")


# ---- FR-5c ---------------------------------------------------------------

def _token(agent_id, stable_id):
    now = time.time()
    return {"agent_id": agent_id, "stable_id": stable_id, "relay_id": rs.relay_id(), "exp": int(now + 600)}


def test_a_removed_or_unknown_agents_token_counts_as_nobody():
    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51805))
    a = _agent(client, "fr5c")
    stable = client.get(f"/agents/{a}", headers=_h(a)).json()["stable_id"]
    assert rs._token_agent(_token(a, stable)) == a
    agent_routes.retire(a, operator_removal=True)
    assert rs._token_agent(_token(a, stable)) is None
    assert rs._token_agent(_token("fr5c_nobody_" + uuid.uuid4().hex[:6], stable)) is None


def test_a_removed_agents_open_agent_socket_closes_on_its_next_frame():
    from starlette.websockets import WebSocketDisconnect

    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51806))
    a = _agent(client, "fr5c_ws")
    stable = client.get(f"/agents/{a}", headers=_h(a)).json()["stable_id"]
    tok = rs._jwt_encode(_token(a, stable))
    with client.websocket_connect(f"/ws/agent/{a}", subprotocols=["emporia.v1", f"emporia.token.{tok}"]) as ws:
        agent_routes.retire(a, operator_removal=True)
        ws.send_text("hi")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


# ---- FR-5d ---------------------------------------------------------------

def test_unsubscribing_when_not_subscribed_leaves_the_count():
    from conftest import signed_topic_body
    client = TestClient(app, client=("127.0.0.1", 51807))
    owner, stranger = _agent(client, "fr5d_o"), _agent(client, "fr5d_s")
    body = signed_topic_body(owner, {"name": f"t {uuid.uuid4().hex[:6]}", "description": "d",
                                     "visibility": "public", "gate_type": "open"})
    slug = client.post("/agoras/topics", json=body, headers=_h(owner)).json()["slug"]

    member = _agent(client, "fr5d_m")
    assert client.post(f"/agoras/topics/{slug}/subscribe", json={"agent_id": member},
                       headers=_h(member)).status_code == 200

    def count():
        with rs.DB_LOCK, rs.get_db() as conn:
            return conn.execute("SELECT subscriber_count FROM agora_topics WHERE slug=?", (slug,)).fetchone()[0]
    before = count()
    assert before >= 1
    r = client.delete(f"/agoras/topics/{slug}/subscribe", params={"agent_id": stranger}, headers=_h(stranger))
    assert r.status_code == 200, r.text
    assert count() == before


# ---- FR-5e ---------------------------------------------------------------

def test_a_non_json_frame_on_the_session_socket_does_not_end_it():
    client = TestClient(app, client=("127.0.0.1", 51808))
    a = _agent(client, "fr5e")
    sid = _open_challenge(client, a)
    tok = rs._jwt_encode(_token(a, client.get(f"/agents/{a}", headers=_h(a)).json()["stable_id"]))
    with client.websocket_connect(f"/ws/{sid}", subprotocols=["emporia.v1", f"emporia.token.{tok}"]) as ws:
        assert ws.receive_json()["type"] == "init"
        ws.send_text("not json {")
        ws.send_text(json.dumps({"type": "ping"}))
        assert ws.receive_json() == {"type": "pong"}
