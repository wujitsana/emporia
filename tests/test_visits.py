"""P188: an agent visits other relays — with its operator's yes — and acts there."""
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from emporia import visits
from emporia.relay_server import app
from tests.test_emporia import _register_keyed

OTHER = "http://relay-b.test"


def test_where_an_agent_may_go(monkeypatch):
    monkeypatch.delenv("EMPORIA_VISIT_POLICY", raising=False)
    assert visits.visit_needs_yes("https://new.example", []) is True                 # ask, by default
    monkeypatch.setenv("EMPORIA_VISIT_RELAYS", "https://ok.example")
    assert visits.visit_needs_yes("https://ok.example/", []) is False
    monkeypatch.setenv("EMPORIA_VISIT_POLICY", "listed")
    with pytest.raises(PermissionError):
        visits.visit_needs_yes("https://new.example", [])
    monkeypatch.setenv("EMPORIA_VISIT_POLICY", "linked")
    assert visits.visit_needs_yes("https://peer.example", ["https://peer.example"]) is False
    monkeypatch.setenv("EMPORIA_VISIT_DENY", "https://bad.example")
    with pytest.raises(PermissionError):
        visits.visit_needs_yes("https://bad.example", [])
    assert not visits.may_read("https://bad.example")


@pytest.fixture
def relay_b(monkeypatch):
    """The relay under test, reached as if it were another relay at OTHER."""
    client = TestClient(app, client=("127.0.0.1", 51280))
    real = {m: getattr(httpx, m) for m in ("get", "post", "delete")}

    def route(method):
        def call(url, *a, **kw):
            if str(url).startswith(OTHER):
                kw.pop("timeout", None)
                return getattr(client, method)(str(url)[len(OTHER):], **kw)
            return real[method](url, *a, **kw)
        return call
    for m in real:
        monkeypatch.setattr(httpx, m, route(m))
    visits.save([])
    yield client
    visits.save([])


def test_a_visit_needs_the_operators_yes_then_the_agent_acts_there(relay_b, monkeypatch):
    from emporia import mcp_server
    _register_keyed(relay_b, "vz_agent")
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    monkeypatch.setattr(mcp_server, "_publish_relay_list", lambda aid: "ok")
    assert "visit_relay" in mcp_server.list_sessions(relay=OTHER)["error"]          # not before visiting
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda message, title="": False)
    out = mcp_server.visit_relay(OTHER, agent_id="vz_agent")
    assert not out["success"] and "said no" in out["error"]
    asked = []
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda message, title="": asked.append(message) or True)
    out = mcp_server.visit_relay(OTHER, agent_id="vz_agent")
    assert out["success"], out
    assert "testnet" in asked[0] and OTHER in visits.visiting()
    listed = mcp_server.list_sessions(relay=OTHER)
    assert "error" not in listed, listed
    assert mcp_server.leave_relay(OTHER, agent_id="vz_agent")["success"] and OTHER not in visits.visiting()


def test_a_closed_relay_turns_visitors_away(relay_b, monkeypatch):
    from emporia import mcp_server
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda message, title="": True)
    remote = TestClient(app, client=("203.0.113.5", 51281))
    monkeypatch.setattr(httpx, "get", lambda url, *a, **kw: remote.get(str(url)[len(OTHER):]))
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    out = mcp_server.visit_relay(OTHER, agent_id="vz_closed")
    assert not out["success"] and "closed" in out["error"]


def test_a_notice_from_a_visited_relay_says_where_it_is_from():
    from emporia.plugins.platforms.emporia.bridge import notice_line
    frame = {"type": "dm_received", "sender_id": "b", "thread_id": "d", "message_id": "m1", "content": "hi"}
    home = json.loads(notice_line(frame, "me"))
    away = json.loads(notice_line(frame, "me", "relay-b.test"))
    assert "relay" not in home and away["relay"] == "relay-b.test"
    assert away["text"].startswith("[on relay-b.test] ")
