"""P182: an agent's record — counts of what this relay saw, read before money moves."""
import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51232))


def _act(client, sid, who, action, payload=None):
    return client.post(f"/sessions/{sid}/action", json={"agent_id": who, "action_type": action,
                                                         "payload": payload or {}},
                       headers={"x-emporia-agent-id": who})


def _purchase(client, buyer, seller, end):
    r = client.post("/sessions", json={"module_type": "emporia:service:v1", "creator_agent_id": buyer,
                                       "config": {"description": "d"}, "payment_rules": {"mode": "free"}},
                    headers={"x-emporia-agent-id": buyer})
    sid = r.json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers={"x-emporia-agent-id": seller})
    return sid


def test_a_new_agent_has_an_empty_record_and_a_stranger_none(client):
    _register(client, "cr_new")
    r = client.get("/agents/cr_new/record")
    assert r.status_code == 200
    body = r.json()
    assert body["games"]["finished"] == 0 and body["as_buyer"]["purchases"] == 0
    assert body["scope"] == "this_relay" and "not a verdict" in body["note"]
    assert client.get("/agents/cr_nobody/record").status_code == 404


def test_an_abandoned_game_counts(client):
    _register(client, "cr_a")
    _register(client, "cr_b")
    r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": "cr_a",
                                       "config": {}}, headers={"x-emporia-agent-id": "cr_a"})
    sid = r.json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": "cr_b"}, headers={"x-emporia-agent-id": "cr_b"})
    r = client.post(f"/sessions/{sid}/abandon", json={}, headers={"x-emporia-agent-id": "cr_a"})
    assert r.status_code == 200, r.text
    assert client.get("/agents/cr_b/record").json()["games"]["abandoned"] == 1


def test_mcp_join_shows_the_creators_record_for_a_purchase(monkeypatch):
    from emporia import mcp_server

    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.headers = code, body, {}

        def json(self):
            return self._b

    def fake(method, url, **kw):
        calls.append(url)
        if url.endswith("/record"):
            return R(200, {"games": {"finished": 3}, "as_buyer": {"disputed": 2}, "as_seller": {}, "note": "n"})
        if url.endswith("/join"):
            return R(200, {"session_id": "s1"})
        return R(200, {"module_type": "emporia:service:v1", "participants": ["buyer1"], "payment_rules": {}})
    monkeypatch.setattr(mcp_server, "_bearer_request", fake)
    monkeypatch.setattr(mcp_server, "_open_games_refusal", lambda aid, mt: None)
    out = mcp_server.join_session("s1", agent_id="seller1")
    assert out["counterparty_records"]["buyer1"]["as_buyer"]["disputed"] == 2


def test_purchases_and_sales_are_counted_by_how_they_ended(client):
    from datetime import UTC, datetime

    from emporia import relay_server as rs
    from emporia.module_sdk import get_interaction_module

    _register(client, "cr_buyer")
    _register(client, "cr_seller")
    module = get_interaction_module("emporia:service:v1")
    now = datetime.now(UTC).isoformat()
    for i, status in enumerate(("confirmed", "disputed", "disputed")):
        state = module.initial_state(["cr_buyer", "cr_seller"], {"description": "d"})
        state.data["status"] = status
        rs.save_session(session_id=f"sess_cr_{i}", module_type="emporia:service:v1", config={},
                        payment_rules={"mode": "free"}, state=state, current_agent="cr_seller",
                        step_number=3, status="completed", participants=["cr_buyer", "cr_seller"],
                        created_at=now, updated_at=now)
    buyer = client.get("/agents/cr_buyer/record").json()["as_buyer"]
    seller = client.get("/agents/cr_seller/record").json()["as_seller"]
    assert (buyer["purchases"], buyer["confirmed"], buyer["disputed"]) == (3, 1, 2)
    assert (seller["sales"], seller["paid"], seller["disputed"]) == (3, 1, 2)
