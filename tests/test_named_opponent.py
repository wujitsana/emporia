"""P181: a game (or purchase) only one named agent can join."""
import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register

BLOTTO = "emporia:blotto:v1"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51231))


def _create(client, creator, config):
    return client.post("/sessions", json={"module_type": BLOTTO, "creator_agent_id": creator, "config": config},
                       headers={"x-emporia-agent-id": creator})


def test_only_the_named_opponent_may_join(client):
    for a in ("no_a", "no_b", "no_c"):
        _register(client, a)
    r = _create(client, "no_a", {"opponent": "no_b"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": "no_c"}, headers={"x-emporia-agent-id": "no_c"})
    assert r.status_code == 403 and "no_b only" in r.text
    inbox = client.get("/agents/no_b/inbox", headers={"x-emporia-agent-id": "no_b"}).json()
    assert any(i.get("event_type") == "session_invite" for i in inbox.get("items", inbox.get("events", [])))
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": "no_b"}, headers={"x-emporia-agent-id": "no_b"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("opponent,why", [
    ("no_a2", "other agent"), ("nobody_here", "not registered"), ("", "other agent"), (7, "other agent"),
])
def test_a_named_opponent_must_be_another_registered_agent(client, opponent, why):
    _register(client, "no_a2")
    r = _create(client, "no_a2", {"opponent": opponent})
    assert r.status_code == 400 and why in r.text


def test_a_named_opponent_is_for_two_player_sessions(client):
    _register(client, "no_g1")
    _register(client, "no_g2")
    r = client.post("/sessions", json={"module_type": "emporia:two-thirds:v1", "creator_agent_id": "no_g1",
                                       "config": {"opponent": "no_g2"}}, headers={"x-emporia-agent-id": "no_g1"})
    assert r.status_code == 400 and "two-player" in r.text
