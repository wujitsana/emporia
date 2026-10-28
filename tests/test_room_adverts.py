"""P247: a room or Agora topic found from another relay is its owner's signed
advert, checked by whoever reads it — hosting stays on the relay that has it."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia import wire
from emporia.identity import get_public_key_hex, sign, verify
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51411))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _origin(client) -> str:
    return client.get("/health").json()["relay_url"]


def _room(client, creator: str, **over) -> str:
    body = {"name": "r", "description": "d", "room_type": "public", "gate_type": "open", "entry_fee_cents": 0,
            "currency": "USD", "max_members": None, "encrypted": False, "linked_session_id": None,
            "access_duration_days": None, **over}
    nonce = wire.new_message_nonce()
    body.update(nonce=nonce, signature=sign(wire.signed_room_payload(
        nonce, body["name"], body["description"], body["room_type"], body["gate_type"], body["entry_fee_cents"],
        body["currency"], body["max_members"], body["encrypted"], body["linked_session_id"],
        body["access_duration_days"]), creator))
    r = client.post("/rooms", json=body, headers=_as(creator))
    assert r.status_code == 200, r.text
    return r.json()["room_id"]


def _room_advert(client, creator: str, room_id: str) -> dict:
    r = client.get(f"/rooms/{room_id}", headers=_as(creator)).json()
    return {"signature": sign(wire.signed_room_advert_payload(
        wire.stable_id_for(get_public_key_hex(creator)), _origin(client), room_id,
        *(r[f] for f in wire.ROOM_ADVERT_FIELDS)), creator)}


def _topic(client, creator: str, slug: str, visibility: str = "public") -> str:
    r = client.post("/agoras/topics", json={"name": f"Topic {slug}", "description": "about it", "slug": slug,
                                            "visibility": visibility}, headers=_as(creator))
    assert r.status_code == 200, r.text
    return slug


def _topic_advert(client, creator: str, slug: str) -> dict:
    t = client.get(f"/agoras/topics/{slug}", headers=_as(creator)).json()
    return {"signature": sign(wire.signed_topic_advert_payload(
        wire.stable_id_for(get_public_key_hex(creator)), _origin(client), slug,
        *(t[f] for f in wire.TOPIC_ADVERT_FIELDS)),
        creator)}


def _rooms(client) -> dict:
    return client.get("/federation/v1/rooms").json()


def test_an_advertised_room_is_in_the_feed_and_verifies(client):
    _register_keyed(client, "ra_host")
    advertised, quiet = _room(client, "ra_host"), _room(client, "ra_host")
    r = client.post(f"/rooms/{advertised}/advertise", json=_room_advert(client, "ra_host", advertised),
                    headers=_as("ra_host"))
    assert r.status_code == 200, r.text
    feed = _rooms(client)
    ids = {x["room_id"] for x in feed["rooms"]}
    assert advertised in ids and quiet not in ids
    item = next(x for x in feed["rooms"] if x["room_id"] == advertised)
    assert wire.room_advert_verdict(item, feed["origin_relay"], verify) == ""
    assert item["join_url"].endswith(f"/rooms/{advertised}/join") and item["owner_stable_id"]


def test_private_and_invite_rooms_cannot_be_advertised(client):
    _register_keyed(client, "ra_priv")
    invite = _room(client, "ra_priv", room_type="private", gate_type="invite")
    paid = _room(client, "ra_priv", room_type="private", gate_type="paid", entry_fee_cents=500)
    for rid in (invite, paid):
        r = client.post(f"/rooms/{rid}/advertise", json={"signature": "00" * 64}, headers=_as("ra_priv"))
        assert r.status_code == 400
    assert not {x["room_id"] for x in _rooms(client)["rooms"]} & {invite, paid}


def test_only_the_creator_advertises_a_room(client):
    _register_keyed(client, "ra_own")
    _register_keyed(client, "ra_thief")
    rid = _room(client, "ra_own")
    assert client.post(f"/rooms/{rid}/advertise", json=_room_advert(client, "ra_own", rid),
                       headers=_as("ra_thief")).status_code == 403
    assert client.post(f"/rooms/{rid}/advertise", json=_room_advert(client, "ra_thief", rid),
                       headers=_as("ra_thief")).status_code == 403
    assert client.post(f"/rooms/{rid}/advertise", json={"signature": sign({"x": 1}, "ra_own")},
                       headers=_as("ra_own")).status_code == 403
    assert client.delete(f"/rooms/{rid}/advertise", headers=_as("ra_thief")).status_code == 403


@pytest.mark.parametrize("field,value", [
    ("entry_fee_cents", 5000), ("gate_type", "paid"), ("name", "other"), ("encrypted", True),
    ("max_members", 2), ("owner_stable_id", "00" * 32),
])
def test_a_relay_cannot_change_an_advertised_rooms_settings(client, field, value):
    _register_keyed(client, f"ra_t_{field}")
    rid = _room(client, f"ra_t_{field}")
    client.post(f"/rooms/{rid}/advertise", json=_room_advert(client, f"ra_t_{field}", rid),
                headers=_as(f"ra_t_{field}"))
    feed = _rooms(client)
    honest = next(x for x in feed["rooms"] if x["room_id"] == rid)
    assert wire.room_advert_verdict({**honest, field: value}, feed["origin_relay"], verify) != ""


def test_changing_the_stored_room_withdraws_its_advert(client):
    import sqlite3

    from emporia import relay_server as rs
    _register_keyed(client, "ra_drift")
    rid = _room(client, "ra_drift")
    client.post(f"/rooms/{rid}/advertise", json=_room_advert(client, "ra_drift", rid), headers=_as("ra_drift"))
    assert rid in {x["room_id"] for x in _rooms(client)["rooms"]}
    with sqlite3.connect(rs.DATABASE_PATH) as conn:
        conn.execute("UPDATE rooms SET gate_type = 'paid', entry_fee_cents = 900 WHERE room_id = ?", (rid,))
    assert rid not in {x["room_id"] for x in _rooms(client)["rooms"]}


def test_the_creator_can_withdraw_an_advert(client):
    _register_keyed(client, "ra_stop")
    rid = _room(client, "ra_stop")
    client.post(f"/rooms/{rid}/advertise", json=_room_advert(client, "ra_stop", rid), headers=_as("ra_stop"))
    assert client.delete(f"/rooms/{rid}/advertise", headers=_as("ra_stop")).status_code == 200
    assert rid not in {x["room_id"] for x in _rooms(client)["rooms"]}


def test_an_advert_names_the_relay_it_was_made_on(client):
    _register_keyed(client, "ra_where")
    rid = _room(client, "ra_where")
    client.post(f"/rooms/{rid}/advertise", json=_room_advert(client, "ra_where", rid), headers=_as("ra_where"))
    item = next(x for x in _rooms(client)["rooms"] if x["room_id"] == rid)
    assert item["origin_relay"] == _origin(client)
    assert wire.room_advert_verdict(item, "https://another-relay.example", verify) != ""
    assert wire.room_advert_verdict({**item, "advert_signature": ""}, item["origin_relay"], verify) != ""


def test_topics_advert_only_public_ones_by_their_owner_and_verify(client):
    _register_keyed(client, "ta_owner")
    _register_keyed(client, "ta_other")
    pub, hidden = _topic(client, "ta_owner", "ta-pub"), _topic(client, "ta_owner", "ta-hidden", "private")
    assert client.post(f"/agoras/topics/{pub}/advertise", json=_topic_advert(client, "ta_owner", pub),
                       headers=_as("ta_other")).status_code == 403
    assert client.post(f"/agoras/topics/{hidden}/advertise", json={"signature": "00" * 64},
                       headers=_as("ta_owner")).status_code == 400
    assert client.post(f"/agoras/topics/{pub}/advertise", json=_topic_advert(client, "ta_owner", pub),
                       headers=_as("ta_owner")).status_code == 200
    feed = client.get("/federation/v1/topics").json()
    assert [x["slug"] for x in feed["topics"]] == [pub] or pub in {x["slug"] for x in feed["topics"]}
    item = next(x for x in feed["topics"] if x["slug"] == pub)
    assert wire.topic_advert_verdict(item, feed["origin_relay"], verify) == ""
    assert wire.topic_advert_verdict({**item, "name": "renamed"}, feed["origin_relay"], verify) != ""
    assert wire.topic_advert_verdict({**item, "entry_fee_cents": 0, "gate_type": "open"} if item["gate_type"] != "open"
                                     else {**item, "gate_type": "paid_invite", "entry_fee_cents": 500},
                                     feed["origin_relay"], verify) != ""
    assert wire.topic_advert_verdict(item, "https://another-relay.example", verify) != ""


def test_a_deleted_topic_leaves_the_feed(client):
    import sqlite3

    from emporia import relay_server as rs
    _register_keyed(client, "ta_gone")
    slug = _topic(client, "ta_gone", "ta-gone")
    client.post(f"/agoras/topics/{slug}/advertise", json=_topic_advert(client, "ta_gone", slug), headers=_as("ta_gone"))
    assert slug in {x["slug"] for x in client.get("/federation/v1/topics").json()["topics"]}
    with sqlite3.connect(rs.DATABASE_PATH) as conn:
        conn.execute("UPDATE agora_topics SET is_deleted = 1 WHERE slug = ?", (slug,))
    assert slug not in {x["slug"] for x in client.get("/federation/v1/topics").json()["topics"]}


def test_the_mcp_tools_advertise_and_scan_relays_drops_forged_adverts(client, monkeypatch):
    from emporia import mcp_server
    from emporia.routes import federation
    _register_keyed(client, "ra_mcp")
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://relay-under-test")
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda method, url, **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers=_as("ra_mcp"),
        **{k: v for k, v in kw.items() if k in ("json", "params")}))

    def fake_get(url, timeout=10, **kw):
        return client.get(url[len("http://relay-under-test"):])

    with patch("httpx.get", fake_get):
        room = mcp_server.create_room("mcp room", "ra_mcp", advertise=True)
        quiet_private = mcp_server.create_room("hidden", "ra_mcp", room_type="private", gate_type="invite",
                                               advertise=True)
        topic = mcp_server.create_agora_topic("MCP topic", "ra_mcp", slug="ra-mcp-topic", advertise=True)
        assert room["advertised"] and topic["advertised"] and not quiet_private["advertised"]
        honest = mcp_server.scan_relays(kind="room")
        honest_topics = mcp_server.scan_relays(kind="topic")
    rid = room["room"]["room_id"]
    assert rid in {x["id"] for x in honest["results"]}
    assert "ra-mcp-topic" in {x["id"] for x in honest_topics["results"]}

    # A relay that alters an advert and signs the altered feed with its own key.
    original_feed = federation._advert_feed

    def tampered(kind):
        return [{**x, "entry_fee_cents": 9900} if kind == "rooms" else {**x, "description": "scam"}
                for x in original_feed(kind)]

    # A relay that points the join link at another host, where it would take the fee.
    def redirected(kind):
        return [{**x, "join_url": "https://elsewhere.example/pay"} for x in original_feed(kind)]

    with patch.object(federation, "_advert_feed", redirected), patch("httpx.get", fake_get):
        led = mcp_server.scan_relays(kind="room")
    found = next(x for x in led["results"] if x["id"] == rid)
    assert found["join_url"].endswith(f"/rooms/{rid}/join") and "elsewhere" not in found["join_url"]

    with patch.object(federation, "_advert_feed", tampered), patch("httpx.get", fake_get):
        forged = mcp_server.scan_relays()
    assert rid not in {x["id"] for x in forged["results"]}
    assert "ra-mcp-topic" not in {x["id"] for x in forged["results"]}
    assert forged["refused_unsigned_or_forged"]["http://relay-under-test"] >= 2
