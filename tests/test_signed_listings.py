"""P240: a listing is its agent's signed word, checked by every relay that
pulls it and by every client that reads it — not taken on the word of the relay
that served it."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia import wire
from emporia.identity import get_public_key_hex, sign, verify
from emporia.relay_server import app
from tests.test_emporia import _make_peer_batch, _peer_listing, _register_keyed, _signed_listing

PEER = "https://peer-p240.example"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51400))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


async def _pull(items: list[dict], origin: str = PEER) -> int:
    batch, _ = _make_peer_batch(origin, items)
    rs._FEDERATION_PEER_PUBKEYS.pop(PEER, None)
    with patch("emporia.relay_server._fetch_federation_batch", new_callable=AsyncMock, return_value=batch):
        return await rs._pull_federated_listings(PEER)


def _stored(listing_id: str):
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT * FROM listings WHERE listing_id = ?", (listing_id,)).fetchone()


# ─── creating one ────────────────────────────────────────────────────────────

def test_an_unsigned_create_is_refused(client):
    _register_keyed(client, "sl_unsigned")
    r = client.post("/listings", json={"title": "x", "listing_type": "service", "expires_in_hours": 24},
                    headers=_as("sl_unsigned"))
    assert r.status_code == 422


def test_a_create_signed_by_another_key_or_for_another_relay_is_refused(client):
    _register_keyed(client, "sl_owner")
    body = _signed_listing(client, "sl_owner", "Mine")
    body["signature"] = sign({"anything": "else"}, "sl_owner")
    assert client.post("/listings", json=body, headers=_as("sl_owner")).status_code == 403

    # Signed for a relay at another URL: it can't be lifted onto this one.
    body = _signed_listing(client, "sl_owner", "Mine")
    signed = wire.signed_listing_payload(
        wire.stable_id_for(get_public_key_hex("sl_owner")), "https://elsewhere.example", body["listing_nonce"],
        body["title"], body["description"], body["listing_type"], body["payment_mode"], body["price_usd"],
        body["module_type"], body["metadata"], body["expires_at"])
    body["signature"] = sign(signed, "sl_owner")
    r = client.post("/listings", json=body, headers=_as("sl_owner"))
    assert r.status_code == 403 and "relay_url" in r.json()["detail"]


def test_a_signed_create_is_stored_as_signed(client):
    _register_keyed(client, "sl_stored")
    body = _signed_listing(client, "sl_stored", "Lessons", price_usd="2.50", metadata={"level": "beginner"})
    r = client.post("/listings", json=body, headers=_as("sl_stored"))
    assert r.status_code == 200, r.text
    key = get_public_key_hex("sl_stored")
    lid = r.json()["listing_id"]
    assert lid == wire.listing_id_for(wire.stable_id_for(key), body["listing_nonce"])
    row = _stored(lid)
    assert (row["signature"], row["listing_nonce"], row["agent_public_key_hex"], row["expires_at"]) == (
        body["signature"], body["listing_nonce"], key, body["expires_at"])
    # The same nonce twice is the same listing, not a second one.
    assert client.post("/listings", json=body, headers=_as("sl_stored")).status_code == 409


def test_what_a_relay_keeps_to_itself_cannot_be_signed_in(client):
    _register_keyed(client, "sl_private")
    body = _signed_listing(client, "sl_private", "x", metadata={"payment_intent_id": "pi_1"})
    r = client.post("/listings", json=body, headers=_as("sl_private"))
    assert r.status_code == 400 and "payment_intent_id" in r.json()["detail"]


def test_expiry_is_the_agents_and_bounded(client):
    from datetime import UTC, datetime, timedelta
    _register_keyed(client, "sl_expiry")
    too_long = _signed_listing(client, "sl_expiry", "x")
    too_long["expires_at"] = (datetime.now(UTC) + timedelta(hours=wire.MAX_LISTING_HOURS + 24)).isoformat()
    assert client.post("/listings", json=too_long, headers=_as("sl_expiry")).status_code == 400
    naive = _signed_listing(client, "sl_expiry", "x")
    naive["expires_at"] = naive["expires_at"].replace("+00:00", "")
    assert client.post("/listings", json=naive, headers=_as("sl_expiry")).status_code == 400


def test_another_agent_cannot_take_a_listings_id_by_reusing_its_nonce():
    a = wire.stable_id_for(get_public_key_hex("sl_squat_a"))
    b = wire.stable_id_for(get_public_key_hex("sl_squat_b"))
    assert wire.listing_id_for(a, "ab" * 16) != wire.listing_id_for(b, "ab" * 16)


# ─── the feed ────────────────────────────────────────────────────────────────

def test_the_feed_carries_the_agents_signature_and_leaves_out_what_the_relay_wrote(client):
    _register_keyed(client, "sl_feed")
    lid = client.post("/listings", json=_signed_listing(client, "sl_feed", "Signed"),
                      headers=_as("sl_feed")).json()["listing_id"]
    with rs.DB_LOCK, rs.get_db() as conn:     # what a bounty or an event's listing looks like
        conn.execute("INSERT INTO listings (listing_id, title, agent_id, origin_relay, created_at) "
                     "VALUES ('lst_sl_relay_wrote', 'Relay', 'sl_feed', ?, '2026-01-01')", (rs.RELAY_BASE_URL,))
    feed = client.get("/gaming/v1/federate/listings").json()["listings"]
    ids = {x["listing_id"] for x in feed}
    assert lid in ids and "lst_sl_relay_wrote" not in ids
    mine = next(x for x in feed if x["listing_id"] == lid)
    assert wire.listing_verdict(mine, verify) == ""


# ─── pulling one ─────────────────────────────────────────────────────────────

async def test_a_listing_its_agent_did_not_sign_is_refused_on_import():
    good = _peer_listing(PEER, "Honest")
    forged = _peer_listing(PEER, "Forged")
    forged["signature"] = sign({"not": "this listing"}, "peer_agent")   # right relay signature, wrong agent one
    assert await _pull([good, forged]) == 1
    assert _stored(good["listing_id"]) and not _stored(forged["listing_id"])
    record = rs._FEDERATION_LAST_SYNC[PEER]
    assert record["refused"] == 1 and record["refused_why"] == {"its agent's signature does not match": 1}


async def test_a_key_that_does_not_hash_to_its_stable_id_is_refused():
    x = _peer_listing(PEER, "Someone else's id")
    x["agent_stable_id"] = wire.stable_id_for(get_public_key_hex("sl_victim"))
    x["listing_id"] = wire.listing_id_for(x["agent_stable_id"], x["listing_nonce"])
    x["signature"] = sign(wire.listing_fields_as_signed(x), "peer_agent")   # validly signed, by the wrong key
    assert await _pull([x]) == 0
    assert "stable id" in next(iter(rs._FEDERATION_LAST_SYNC[PEER]["refused_why"]))


@pytest.mark.parametrize("field,value", [
    ("title", "Cheaper elsewhere"), ("price_usd", "0.01"), ("payment_mode", "stripe_pi"),
    ("metadata", {"link": "https://scam.example"}), ("expires_at", wire.listing_expires_at(700)),
    ("origin_relay", "https://elsewhere.example"),
])
async def test_a_relay_cannot_change_a_field_of_a_listing_it_serves(field, value):
    x = {**_peer_listing(PEER), field: value}
    assert await _pull([x]) == 0


async def test_a_listing_put_up_on_another_relay_is_not_re_served():
    """One hop: the agent's signature names the relay it was put up on, and a
    peer serving it as its own is refused — multi-hop is a separate decision."""
    x = _peer_listing("https://third-relay.example")
    assert await _pull([x]) == 0
    assert "another relay" in next(iter(rs._FEDERATION_LAST_SYNC[PEER]["refused_why"]))


async def test_an_unsigned_listing_from_a_peer_is_refused():
    x = _peer_listing(PEER)
    for k in ("signature", "listing_nonce"):
        x.pop(k)
    assert await _pull([x]) == 0
    assert rs._FEDERATION_LAST_SYNC[PEER]["refused_why"] == {"unsigned by its agent": 1}


async def test_the_peers_page_counts_refusals(client):
    bad = {**_peer_listing(PEER), "title": "tampered"}
    with patch.object(rs, "FEDERATED_RELAYS", [PEER]):
        await _pull([bad])
        peer = next(p for p in client.get("/federation/peers").json()["peers"] if p["url"] == PEER)
    assert peer["refused"] == 1


# ─── the clients sign, and check what they read ──────────────────────────────

def test_the_mcp_tool_signs_and_a_pulling_relay_accepts_it(client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(client, "sl_mcp")
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda method, url, **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers=_as("sl_mcp"), **{k: v for k, v in kw.items() if k == "json"}))
    with patch("httpx.get", lambda url, **kw: client.get("/" + url.split("/", 3)[3])):
        out = mcp_server.create_listing("From MCP", agent_id="sl_mcp", price_usd="4.00")
    assert out["success"], out
    feed = client.get("/gaming/v1/federate/listings").json()
    mine = next(x for x in feed["listings"] if x["listing_id"] == out["listing_id"])
    assert wire.listing_verdict(mine, verify) == ""

    # Pulled by another relay, in the batch exactly as served. (One database
    # here, so the seller's own row goes first: a local row is never peer-writable.)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM listings WHERE listing_id = ?", (out["listing_id"],))

    async def pull():
        rs._FEDERATION_PEER_PUBKEYS.pop(PEER, None)
        with patch("emporia.relay_server._fetch_federation_batch", new_callable=AsyncMock,
                   return_value=json.loads(json.dumps(feed))), \
             patch("emporia.relay_server.RELAY_BASE_URL", "https://the-puller.example"), \
             patch("emporia.relay_server.get_relay_public_key_hex", return_value="00" * 32):
            return await rs._pull_federated_listings(PEER)
    assert asyncio.run(pull()) >= 1
    assert _stored(out["listing_id"])["signature"] == mine["signature"]


def test_the_sdk_signs_and_drops_a_forged_federated_listing(client):
    import httpx

    from emporia.agent_sdk import EmporiaAgent

    async def scenario():
        agent = EmporiaAgent(relay_url="http://testserver", agent_id="sl_sdk",
                             public_key_hex=get_public_key_hex("sl_sdk"))
        agent._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        try:
            await agent.register()
            made = await agent.create_listing("From the SDK", price_usd="1.00")
            # A row this relay says it pulled, that its agent never signed as shown.
            forged = {**_peer_listing(PEER, "Real title"), "title": "Swapped title"}
            with rs.DB_LOCK, rs.get_db() as conn:
                conn.execute(
                    "INSERT INTO listings (listing_id, title, agent_id, origin_relay, expires_at, created_at, "
                    "federated_from, agent_public_key_hex, agent_stable_id, listing_nonce, signature, "
                    "listing_type, payment_mode, price_usd, metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')",
                    (forged["listing_id"], forged["title"], "peer_agent", PEER, forged["expires_at"],
                     forged["created_at"], PEER, forged["agent_public_key_hex"], forged["agent_stable_id"],
                     forged["listing_nonce"], forged["signature"], "service", "free", "0"))
            seen = {x["listing_id"] for x in await agent.get_listings(listing_type="service", limit=200)}
            return made, forged, seen
        finally:
            await agent._http.aclose()

    made, forged, seen = asyncio.run(scenario())
    assert made["listing_id"] in seen and forged["listing_id"] not in seen
    row = _stored(made["listing_id"])
    assert row["signature"] and row["agent_stable_id"] == wire.stable_id_for(get_public_key_hex("sl_sdk"))
