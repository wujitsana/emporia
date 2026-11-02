"""P188: games waiting for a player, in a signed feed — and scan_relays, which
reads many relays' feeds without joining any."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register, _register_keyed, _signed_listing


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51270))


def _session(client, creator: str, config: dict | None = None) -> str:
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": creator,
                                       "config": config or {}, "payment_rules": {"mode": "free"}},
                    headers={"x-emporia-agent-id": creator})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    if not (config or {}).get("opponent"):    # P241: offered to other relays' agents
        from tests.test_signed_open_sessions import _offer_body
        r = client.post(f"/sessions/{sid}/offer", json=_offer_body(client, creator, sid),
                        headers={"x-emporia-agent-id": creator})
        assert r.status_code == 200, r.text
    return sid


def test_the_feed_lists_open_public_games_only(client):
    _register_keyed(client, "sc_host")
    _register(client, "sc_friend")
    open_game = _session(client, "sc_host")
    named = _session(client, "sc_host", {"opponent": "sc_friend"})
    feed = client.get("/federation/v1/open-sessions").json()
    ids = {x["session_id"] for x in feed["open_sessions"]}
    assert open_game in ids and named not in ids
    mine = next(x for x in feed["open_sessions"] if x["session_id"] == open_game)
    assert mine["creator_address"].startswith("sc_host@") and len(mine["creator_fingerprint"]) == 9
    from emporia.identity import federation_signable_bytes, verify_raw
    assert verify_raw(federation_signable_bytes(feed["origin_relay"], "open_sessions", feed["open_sessions"]),
                      feed["signature"], feed["origin_public_key_hex"])


def test_scan_relays_reads_without_joining_and_skips_what_it_must(client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(client, "sc_seller")
    assert client.post("/listings", json=_signed_listing(client, "sc_seller", "Chess lessons", price_usd="3.00"),
                       headers={"x-emporia-agent-id": "sc_seller"}).status_code == 200
    game = _session(client, "sc_seller")
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://relay-under-test")
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    monkeypatch.setenv("EMPORIA_VISIT_DENY", "https://shunned.example")

    def fake_get(url, timeout=10):
        if url.startswith("http://relay-under-test"):
            return client.get(url[len("http://relay-under-test"):])
        raise ConnectionError("no such relay")

    with patch("httpx.get", fake_get):
        out = mcp_server.scan_relays(relays=["https://shunned.example", "https://gone.example"])
    assert out["success"]
    ids = {x["id"] for x in out["results"]}
    assert game in ids and any(x["title"] == "Chess lessons" and x["price_cents"] == 300 for x in out["results"])
    assert out["skipped"]["https://shunned.example"] == "on this agent's deny list"
    assert "not answering" in out["skipped"]["https://gone.example"]
    with patch("httpx.get", fake_get):
        cheap = mcp_server.scan_relays(max_price_cents=0, kind="game")
    assert all(x["price_cents"] == 0 and x["kind"] in ("game", "purchase") for x in cheap["results"])


def test_scan_skips_a_closed_relay(client, monkeypatch):
    from emporia import mcp_server
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://closed-relay")
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    remote = TestClient(app, client=("203.0.113.9", 51271))
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "closed")
    with patch("httpx.get", lambda url, timeout=10: remote.get(url[len("http://closed-relay"):])):
        out = mcp_server.scan_relays()
    assert out["results"] == [] and out["skipped"]["http://closed-relay"] == "closed — members only"



def test_a_relay_claiming_another_relays_url_is_held_to_the_host_it_was_reached_at(client, monkeypatch):
    """P264: a scanned relay's word for its own URL is believed only from this
    agent's home relay, or when it names the host the agent reached. Here one
    relay is reached at two names: as home it is believed; reached as
    impostor.example and still claiming to be the original, its listings and
    games are refused, not shown as put up there."""
    from emporia import mcp_server
    _register_keyed(client, "sc_origin")
    assert client.post("/listings", json=_signed_listing(client, "sc_origin", "Original lessons"),
                       headers={"x-emporia-agent-id": "sc_origin"}).status_code == 200
    _session(client, "sc_origin")
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])

    def fake_get(url, timeout=10):
        for name in ("http://home-relay", "https://impostor.example"):
            if url.startswith(name):
                return client.get(url[len(name):])
        raise ConnectionError("no such relay")

    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://home-relay")
    with patch("httpx.get", fake_get):
        out = mcp_server.scan_relays(relays=["https://impostor.example"])
    by_relay = {}
    for x in out["results"]:
        by_relay.setdefault(x["relay"], []).append(x["title"])
    assert "Original lessons" in by_relay.get("http://home-relay", [])
    assert "https://impostor.example" not in by_relay
    assert out["refused_unsigned_or_forged"].get("https://impostor.example", 0) >= 2
