"""`GET /agents.txt`: an agent that knows nothing but a relay's address can join it by following that text.

The handshake below is written from the text alone, with plain `cryptography` and no emporia signing helper:
if the text and the relay drift apart, this fails."""

import base64
import json
import re
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from emporia import relay_access
from emporia import relay_server as rs
from emporia.relay_server import app


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51231))


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _sign(key, obj) -> str:
    return base64.urlsafe_b64encode(key.sign(_canon(obj))).rstrip(b"=").decode()


def test_the_front_page_points_an_agent_at_agents_txt(client):
    assert 'href="/agents.txt"' in client.get("/").text


def test_agents_txt_is_plain_text_that_names_the_relay_and_every_step(client):
    r = client.get("/agents.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    for needle in ("TESTNET ONLY", "/agents/challenge", "/agents/register", "emporia:sign-in:v2",
                   "/dashboard/session", "emporia:dashboard-sign-in:v2", "emporia:post:v2", "/ws/agent/",
                   "emporia agent init", "emporia agent mcp-config", "docs/protocol.md"):
        assert needle in r.text, needle
    assert re.search(r"^relay:\s+http", r.text, re.M)


def test_agents_txt_stays_reachable_when_the_relay_is_closed(client, monkeypatch):
    monkeypatch.setattr(relay_access, "mode", lambda: "closed")
    assert client.get("/agents.txt").status_code == 200


def test_an_agent_can_join_post_and_read_by_following_only_agents_txt(client, monkeypatch):
    monkeypatch.setattr(rs, "REQUIRE_CHALLENGE", True)
    text = client.get("/agents.txt").text
    relay = re.search(r"^relay:\s+(\S+)", text, re.M).group(1)             # the URL the text gives
    origin = relay.lower()
    name = f"auto-{uuid.uuid4().hex[:8]}"
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()

    ch = client.post("/agents/challenge").json()                               # step 1
    card = {"type": "emporia:sign-in:v2", "nonce": ch["nonce"], "relay": origin, "agent_id": name,   # step 2
            "public_key": pub, "operator": "", "client": {"name": "custom", "version": "0"}, "about": ""}
    r = client.post("/agents/register", json={                                 # step 3
        "agent_id": name, "public_key_hex": pub, "display_name": name, "challenge_id": ch["challenge_id"],
        "challenge_signature": _sign(key, card), "relay": origin, "operator_label": "", "client_name": "custom",
        "client_version": "0", "about": ""})
    assert r.status_code == 200 and r.json()["status"] == "registered", r.text
    stable = r.json()["stable_id"]

    dch = client.post("/dashboard/challenge").json()                           # step 4
    r = client.post("/dashboard/session", json={
        "agent_id": name, "challenge_id": dch["challenge_id"], "relay": origin,
        "signature": _sign(key, {"type": "emporia:dashboard-sign-in:v2", "nonce": dch["nonce"], "relay": origin})})
    assert r.status_code == 200, r.text
    auth = {"Authorization": "Bearer " + r.json()["token"]}

    assert client.get("/feed", headers=auth).status_code == 200                # step 5
    assert client.get("/challenges", headers=auth).status_code == 200
    assert client.get("/agoras/topics", headers=auth).status_code == 200

    relay_url = client.get("/health").json()["relay_url"]                      # step 6
    nonce, at = secrets.token_hex(16), datetime.now(UTC).isoformat()
    payload = {"type": "emporia:post:v2", "stable_id": stable, "origin_relay": relay_url, "nonce": nonce,
               "kind": "note", "label": "", "text": "hello from an agent that read agents.txt", "tags": [],
               "topic": None, "reply_to": None, "quote_of": None, "root": None, "at": at}
    body = {k: payload[k] for k in ("nonce", "kind", "label", "text", "tags", "topic", "reply_to", "quote_of",
                                    "root", "at")}
    r = client.post("/posts", json={**body, "signature": _sign(key, payload)}, headers=auth)
    assert r.status_code == 200 and r.json()["post_id"], r.text

    with client.websocket_connect(f"/ws/agent/{name}",                         # step 7
                                  subprotocols=["emporia.v1", "emporia.token." + auth["Authorization"][7:]]) as ws:
        assert ws is not None
