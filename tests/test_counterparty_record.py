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
    r = client.post("/sessions", json={"module_type": "emporia:blotto:v2", "creator_agent_id": "cr_a",
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
        for who in ("cr_buyer", "cr_seller"):        # a joined agent is in the participants table (the lookup's index)
            rs.add_participant(f"sess_cr_{i}", who)
    buyer = client.get("/agents/cr_buyer/record").json()["as_buyer"]
    seller = client.get("/agents/cr_seller/record").json()["as_seller"]
    assert (buyer["purchases"], buyer["confirmed"], buyer["disputed"]) == (3, 1, 2)
    assert (seller["sales"], seller["paid"], seller["disputed"]) == (3, 1, 2)


def test_a_record_is_signed_by_the_relay_that_counted_it(client, monkeypatch):
    """P189: readable and checkable on another relay, as "this relay says…"."""
    from emporia import mcp_server
    from emporia.identity import federation_signable_bytes, verify_raw
    _register(client, "cr_signed")
    body = client.get("/agents/cr_signed/record").json()
    from emporia.moves import signed_part
    record = signed_part(body)
    assert verify_raw(federation_signable_bytes(body["relay"], "record", [record]), body["signature"],
                      body["relay_public_key"])
    record["games"]["won"] += 5                                    # a relay or a middleman inflating it
    assert not verify_raw(federation_signable_bytes(body["relay"], "record", [record]), body["signature"],
                          body["relay_public_key"])
    assert body["address"].startswith("cr_signed@") and len(body["fingerprint"]) == 9
    monkeypatch.setattr(mcp_server, "_bearer_request",
                        lambda method, url, **kw: client.get("/" + url.split("/", 3)[3]))
    out = mcp_server.records(kind="counterparty", agent_id="cr_signed")
    assert out["success"] and out["verified"] is True and out["view"] == "counterparty"
    assert not mcp_server.records(kind="counterparty")["success"], "an agent is required"


def test_a_token_goes_only_to_a_relay_this_agent_uses(monkeypatch):
    """Release review 2.1 (delta 2, finding 1): a record lookup at `name@host` sent the home relay's session
    token to that host. A URL on no relay the agent uses now carries no token; one on its home still does."""
    import httpx
    from emporia import mcp_server
    sent = []
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://home.test")
    monkeypatch.setattr(mcp_server, "_visits", lambda: [])
    monkeypatch.setattr(mcp_server, "_backups", lambda: [])
    monkeypatch.setattr(mcp_server, "_dashboard_bearer_headers",
                        lambda relay_url="", agent_id="": {"Authorization": f"Bearer tok-for-{relay_url}"})
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, **kw: sent.append((url, dict(headers or {})))
                        or httpx.Response(200, json={}, request=httpx.Request("GET", url)))
    mcp_server._bearer_request("get", "https://other.example/agents/x/record", agent_id="me")
    mcp_server._bearer_request("get", "http://home.test/agents/x/record", agent_id="me")
    assert "Authorization" not in sent[0][1]
    assert sent[1][1]["Authorization"] == "Bearer tok-for-http://home.test"


def test_another_relays_record_is_read_with_no_token_and_never_inside_the_network(monkeypatch):
    import httpx
    from emporia import fetch, mcp_server
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://home.test")
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: pytest.fail("no token request for an address"))
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)              # the suite's pass-through skips the public check
    monkeypatch.setattr(fetch, "get", lambda *a, **k: pytest.fail("no GET into a private network"))
    out = mcp_server._counterparty_record("x@10.0.0.5:8080")
    assert out["success"] is False
    seen = []
    monkeypatch.setattr(mcp_server, "_relay_get", lambda url, **kw: seen.append(url) or httpx.Response(
        404, json={"detail": "no agent"}, request=httpx.Request("GET", url)))
    assert mcp_server._counterparty_record("x@other.example")["success"] is False
    assert seen == ["https://other.example/agents/x/record"]


def test_joining_a_game_you_are_already_in_makes_no_new_commitment(monkeypatch):
    """Found live 11-21 (sess_6d9713a97d9b): a second join made a fresh secret and seat and saved them over the
    commitment the relay holds, before the relay refused the join."""
    from emporia import mcp_server
    posted = []

    class R:
        status_code, headers = 200, {}

        def json(self):
            return {"module_type": "emporia:spillway:v1", "participants": ["alpha", "beta"], "payment_rules": {}}

    def fake(method, url, **kw):
        posted.append((method, url))
        return R()
    monkeypatch.setattr(mcp_server, "_bearer_request", fake)
    monkeypatch.setattr(mcp_server, "_rand_commit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no new commit")))
    out = mcp_server.join_session("sess_x", agent_id="beta")
    assert out["success"] is True and out["already_seated"] is True
    assert [m for m, _ in posted] == ["get"], posted


def test_a_join_the_relay_refuses_leaves_the_kept_secret_as_it_was(monkeypatch):
    """Fable delta-3 #9: the new commitment is written before the join is sent; a refused join restores the file."""
    from emporia import mcp_server

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.headers = code, body, {}

        def json(self):
            return self._b

    def fake(method, url, **kw):
        if url.endswith("/join"):
            return R(409, {"detail": "no"})
        return R(200, {"module_type": "emporia:spillway:v1", "participants": ["alpha"], "payment_rules": {}})
    monkeypatch.setattr(mcp_server, "_bearer_request", fake)
    monkeypatch.setattr(mcp_server, "_uses_rand", lambda mt: True)
    monkeypatch.setattr(mcp_server, "_open_games_refusal", lambda aid, mt: None)
    monkeypatch.setattr(mcp_server, "_operator_cap_refusal", lambda aid, others: None)
    sid = "sess_keep"
    with mcp_server._commitments(mcp_server._rand_file("beta", sid)) as saved:
        saved.clear()
        saved.update({"s": "aa" * 32, "hash": "1" * 64, "seat": 1})

    def new_commit(aid, session_id, seat):
        with mcp_server._commitments(mcp_server._rand_file(aid, session_id)) as saved:
            saved.update({"s": "bb" * 32, "hash": "2" * 64, "seat": seat})
        return {"hash": "2" * 64, "beacon_round": 1, "at": "x", "signature": "y"}
    monkeypatch.setattr(mcp_server, "_rand_commit", new_commit)
    out = mcp_server.join_session(sid, agent_id="beta")
    assert out["success"] is False
    with mcp_server._commitments(mcp_server._rand_file("beta", sid)) as saved:
        assert saved["hash"] == "1" * 64 and saved["s"] == "aa" * 32
