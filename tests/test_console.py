"""P126: the local console — signs in to the relay as a local agent with that
agent's own key, opens its sealed DMs, and answers only its owner."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from emporia import crypto_box, identity
from emporia.console.agents import discover
from emporia.console.app import COOKIE, Console, build_app
from emporia.relay_server import app as relay_app
from tests.test_emporia import _enc_pair


@pytest.fixture
def client():
    return TestClient(relay_app, client=("127.0.0.1", 51217))


def _console(session_token=None):
    transport = httpx.ASGITransport(app=relay_app, client=("203.0.113.9", 4000))
    return Console([identity.KEY_DIR], "http://relay.test", session_token=session_token,
                   transport=transport)


def test_an_agents_own_profile_keys_win_over_a_stale_copy(tmp_path):
    for profile, agents in (("alpha", ["alpha"]), ("hub", ["alpha", "hub"])):
        d = tmp_path / profile / "home" / ".hermes" / "keys"
        d.mkdir(parents=True)
        for a in agents:
            (d / f"{a}.priv").write_bytes(b"x" * 32)
        (d / "_relay_identity.priv").write_bytes(b"x" * 32)
    found = discover(sorted(tmp_path.glob("*/home/.hermes/keys"), reverse=True))
    assert sorted(found) == ["alpha", "hub"]
    assert found["alpha"].key_dir.parts[-4] == "alpha"


def test_the_console_signs_in_as_the_agent_with_its_own_key(client):
    """From a non-local address and without any agent header: only the agent's
    signed session can make the relay answer as that agent."""
    _enc_pair(client, "p126_a", "p126_b")
    c = TestClient(build_app(_console()))
    r = c.get("/relay/p126_a/agents/p126_a/inbox?unread_only=true",
              headers={"x-emporia-agent-id": "p126_b"})       # ignored, never forwarded
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "p126_a"
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/relay/nobody_here/agents").status_code == 404
    assert "p126_a" in c.get("/agents").json()["agents"]


def test_the_console_opens_a_sealed_dm_for_its_recipient(client):
    _enc_pair(client, "p126_c", "p126_d")
    pubs = {a: identity.get_encryption_public_key_hex(a) for a in ("p126_c", "p126_d")}
    nonce = "a" * 32
    env = crypto_box.seal("for d only", pubs,
                          crypto_box.message_aad("dm", "dm_x", "p126_c", "chat", nonce))
    msgs = [{"message_id": "m1", "sender_id": "p126_c", "msg_type": "chat", "nonce": nonce,
             "content": json.dumps(env)}, {"message_id": "m2", "content": "plain"}]
    c = TestClient(build_app(_console()))
    r = c.post("/dm/p126_d/open", json={"thread_id": "dm_x", "messages": msgs})
    assert r.json() == {"opened": {"m1": "for d only"}}
    other = c.post("/dm/p126_d/open", json={"thread_id": "dm_other", "messages": msgs})
    assert other.json() == {"opened": {"m1": None}}


def test_standalone_answers_only_its_link_holder_on_localhost(client):
    _enc_pair(client, "p126_e", "p126_f")
    c = TestClient(build_app(_console(session_token="s" * 32)), base_url="http://127.0.0.1:8089")
    assert c.get("/agents").status_code == 401
    assert c.get("/session?key=wrong", follow_redirects=False).status_code == 401
    ok = c.get("/session?key=" + "s" * 32, follow_redirects=False)
    assert ok.status_code == 303 and COOKIE in ok.headers["set-cookie"]
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert "samesite=strict" in ok.headers["set-cookie"].lower()
    assert c.get("/agents").status_code == 200
    rebound = TestClient(build_app(_console(session_token="s" * 32)),
                         base_url="http://attacker.example")
    rebound.cookies.set(COOKIE, "s" * 32)
    assert rebound.get("/agents").status_code == 401, "a non-local Host is refused"
