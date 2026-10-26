"""P187: an agent's address, fingerprint, signed relay list and card."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia.addresses import base_url, fingerprint, split_address
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51260))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _publish(client, agent_id: str, when: datetime, *, visits=(), a2a="", signer=None):
    from emporia.identity import sign
    from emporia.wire import signed_relay_list_payload
    stamp = when.isoformat()
    home = "http://127.0.0.1:8088"
    sig = sign(signed_relay_list_payload(agent_id, home, list(visits), a2a, stamp), signer or agent_id)
    return client.post(f"/agents/{agent_id}/relay-list", headers=_as(agent_id),
                       json={"home": home, "visits": list(visits), "a2a_url": a2a, "updated_at": stamp,
                             "signature": sig})


def test_addresses_and_fingerprints():
    assert split_address("bob@Relay.Example:8443") == ("bob", "relay.example:8443")
    assert base_url("relay.example") == "https://relay.example"
    assert base_url("127.0.0.1:8088") == "http://127.0.0.1:8088"
    fp = fingerprint("ab" * 32)
    assert len(fp) == 9 and fp[4] == "·" and fp == fingerprint("AB" * 32)


def test_an_agent_learns_its_address_and_fingerprint(client):
    _register_keyed(client, "ad_alpha")
    profile = client.get("/agents/ad_alpha", headers=_as("ad_alpha")).json()
    host = client.get("/health").json()["host"]
    assert profile["address"] == f"ad_alpha@{host}"
    assert profile["fingerprint"] == fingerprint(profile["public_key_hex"])


def test_a_card_exists_only_once_the_agent_publishes_its_relay_list(client):
    _register_keyed(client, "ad_card")
    assert client.get("/agents/ad_card/card").status_code == 404
    now = datetime.now(UTC)
    r = _publish(client, "ad_card", now, visits=["https://b.example"], a2a="https://ad-card.example/a2a")
    assert r.status_code == 200, r.text
    card = client.get("/agents/ad_card/card").json()["card"]
    assert card["relay_list"]["visits"] == ["https://b.example"] and card["a2a_url"] == "https://ad-card.example/a2a"
    assert _publish(client, "ad_card", now - timedelta(minutes=1)).status_code == 409   # an older list
    _register_keyed(client, "ad_forger")
    assert _publish(client, "ad_card", now + timedelta(minutes=1), signer="ad_forger").status_code == 403


def test_agent_card_checks_every_signature(client):
    from emporia import mcp_server
    _register_keyed(client, "ad_checked")
    assert _publish(client, "ad_checked", datetime.now(UTC)).status_code == 200
    path_of = lambda url: "/" + url.split("/", 3)[3]   # noqa: E731 — the relay under test, whatever its URL
    with patch("httpx.get", lambda url, timeout=10: client.get(path_of(url))):
        out = mcp_server.agent_card("ad_checked")
    assert out["success"] and out["verified"], out
    assert all(v for k, v in out["checks"].items() if k != "relay_key_changed_since_first_seen")
