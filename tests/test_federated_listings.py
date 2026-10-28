"""P184: listings from a linked relay — where the deal happens, whose they are,
and that they go when the relay withdraws them or is unlinked."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _make_peer_batch, _peer_listing, _register

PEER = "https://peer-p184.example"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51240))


def _listing(agent_id: str = "p184_seller") -> dict:
    return _peer_listing(PEER, "Lessons", seller="p184_seller", agent_id=agent_id)


async def _pull(items: list[dict]) -> int:
    batch, _ = _make_peer_batch(PEER, items)
    rs._FEDERATION_PEER_PUBKEYS.pop(PEER, None)
    with patch("emporia.relay_server._fetch_federation_batch", new_callable=AsyncMock, return_value=batch):
        return await rs._pull_federated_listings(PEER)


def _shown(client, listing_id: str) -> dict | None:
    items = client.get("/listings", params={"include_rooms": "false", "limit": 200}).json()["listings"]
    return next((x for x in items if x["listing_id"] == listing_id), None)


async def test_a_federated_listing_says_where_the_deal_happens(client):
    x = _listing()
    assert await _pull([x]) == 1
    shown = _shown(client, x["listing_id"])
    assert shown["deal_on"] == PEER and f"visit_relay('{PEER}')" in shown["how_to_deal"]
    assert shown["agent_public_key_hex"] == x["agent_public_key_hex"] and "name_clash" not in shown


async def test_an_agent_here_with_the_sellers_name_is_flagged(client):
    _register(client, "p184_clash")          # a different key from the seller's
    x = _listing(agent_id="p184_clash")
    assert await _pull([x]) == 1
    assert "different agent" in _shown(client, x["listing_id"])["name_clash"]


async def test_a_listing_the_peer_withdrew_goes(client):
    c, d = _listing(), _listing()
    await _pull([c, d])
    await _pull([d])
    assert _shown(client, c["listing_id"]) is None and _shown(client, d["listing_id"]) is not None


async def test_an_unlinked_relays_listings_go(client, monkeypatch):
    e = _listing()
    await _pull([e])
    monkeypatch.setattr(rs, "FEDERATED_RELAYS", [PEER])
    rs.drop_unlinked_federated_rows()
    assert _shown(client, e["listing_id"]) is not None
    monkeypatch.setattr(rs, "FEDERATED_RELAYS", [])
    rs.drop_unlinked_federated_rows()
    assert _shown(client, e["listing_id"]) is None


def test_the_feed_carries_each_sellers_key_inside_the_signature(client):
    from tests.test_emporia import _register_keyed, _signed_listing
    agent = "p184_local"
    _register_keyed(client, agent)
    r = client.post("/listings", json=_signed_listing(client, agent, "Mine"),
                    headers={"x-emporia-agent-id": agent})
    assert r.status_code == 200, r.text
    feed = client.get("/gaming/v1/federate/listings").json()
    mine = next(x for x in feed["listings"] if x["agent_id"] == agent)
    with rs.DB_LOCK, rs.get_db() as conn:
        key = conn.execute("SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?",
                           (agent,)).fetchone()[0]
    assert mine["agent_public_key_hex"] == key
