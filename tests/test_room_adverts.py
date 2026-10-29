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


def test_invite_rooms_are_never_advertised_or_listed_to_outsiders(client):
    _register_keyed(client, "ra_priv")
    _register_keyed(client, "ra_out")
    invite = _room(client, "ra_priv", room_type="private", gate_type="invite")
    r = client.post(f"/rooms/{invite}/advertise", json={"signature": "00" * 64}, headers=_as("ra_priv"))
    assert r.status_code == 400
    assert invite not in {x["room_id"] for x in _rooms(client)["rooms"]}
    for who in (_as("ra_out"), {}):
        assert invite not in {x["room_id"] for x in client.get("/rooms", headers=who).json()["rooms"]}
        assert invite not in {x["listing_id"] for x in client.get("/listings?listing_type=room&limit=200", headers=who).json()["listings"]}
        assert client.get(f"/rooms/{invite}", headers=who).status_code == 403
    # a creator still sees it, as one of their own
    assert invite in {x["room_id"] for x in client.get("/rooms", headers=_as("ra_priv")).json()["rooms"]}


def test_a_paid_room_is_advertised_only_when_its_creator_chooses(client):
    _register_keyed(client, "ra_paid")
    _register_keyed(client, "ra_buyer")
    quiet = _room(client, "ra_paid", room_type="private", gate_type="paid", entry_fee_cents=500)
    shown = _room(client, "ra_paid", room_type="private", gate_type="paid", entry_fee_cents=700,
                  currency="USD", access_duration_days=30, name="Paid club", description="weekly notes")
    # not advertised: nobody outside sees it, and the detail is closed
    assert quiet not in {x["room_id"] for x in client.get("/rooms", headers=_as("ra_buyer")).json()["rooms"]}
    assert client.get(f"/rooms/{quiet}", headers=_as("ra_buyer")).status_code == 403
    assert client.post(f"/rooms/{shown}/advertise", json=_room_advert(client, "ra_paid", shown),
                       headers=_as("ra_paid")).status_code == 200
    assert shown in {x["room_id"] for x in _rooms(client)["rooms"]}
    item = next(x for x in _rooms(client)["rooms"] if x["room_id"] == shown)
    assert item["gate_type"] == "paid" and item["entry_fee_cents"] == 700 and item["join_url"].endswith("/join")

    # listed to everyone on its own relay — a stranger, and nobody at all
    for who in (_as("ra_buyer"), {}):
        listed = client.get("/rooms", headers=who).json()["rooms"]
        assert quiet not in {x["room_id"] for x in listed}
        card = next(x for x in listed if x["room_id"] == shown)
        assert card["advertised"] is True and card["listing_only"] is True and card["host"]
        assert (card["name"], card["description"], card["entry_fee_cents"], card["currency"],
                card["access_duration_days"], card["member_count"], card["encrypted"]) == (
            "Paid club", "weekly notes", 700, "USD", 30, 1, False)
        # nothing else: no roster, no session link, no internal timestamps
        assert set(card) == {"room_id", "name", "description", "room_type", "gate_type", "entry_fee_cents",
                             "currency", "access_duration_days", "max_members", "member_count", "encrypted",
                             "creator_id", "created_at", "listing_only", "advertised", "host"}
        assert "ra_paid" not in str(card["member_count"])
        # one listing in the marketplace feed too
        assert shown in {x["listing_id"] for x in client.get("/listings?listing_type=room&limit=200", headers=who).json()["listings"]}
        # the room itself stays shut: terms only, never messages or the chain
        detail = client.get(f"/rooms/{shown}", headers=who)
        assert detail.status_code == 200 and detail.json()["listing_only"] is True
        assert "members" not in detail.json() and [a["act"] for a in detail.json()["signed_acts"]] == ["create"]
        for path in ("messages", "chain", "encryption-keys"):
            assert client.get(f"/rooms/{shown}/{path}", headers=who).status_code in (401, 403)
    # the creator sees the full room, still marked advertised
    own = client.get(f"/rooms/{shown}", headers=_as("ra_paid")).json()
    assert own["advertised"] is True and own["members"] == ["ra_paid"]
    assert client.get("/rooms", headers=_as("ra_paid")).json()["rooms"][0].get("listing_only") is None
    # withdrawing takes it off the list again
    assert client.delete(f"/rooms/{shown}/advertise", headers=_as("ra_paid")).status_code == 200
    assert shown not in {x["room_id"] for x in client.get("/rooms", headers=_as("ra_buyer")).json()["rooms"]}
    assert client.get(f"/rooms/{shown}", headers=_as("ra_buyer")).status_code == 403


def test_responses_say_where_things_live_and_whether_they_are_advertised(client):
    _register_keyed(client, "ra_where2")
    adv, plain = _room(client, "ra_where2"), _room(client, "ra_where2")
    client.post(f"/rooms/{adv}/advertise", json=_room_advert(client, "ra_where2", adv), headers=_as("ra_where2"))
    by_id = {x["room_id"]: x for x in client.get("/rooms", headers=_as("ra_where2")).json()["rooms"]}
    assert by_id[adv]["advertised"] is True and by_id[plain]["advertised"] is False
    assert by_id[adv]["host"] == by_id[plain]["host"] != ""
    assert client.get(f"/rooms/{plain}", headers=_as("ra_where2")).json()["advertised"] is False
    listed = {x["listing_id"]: x for x in client.get("/listings?listing_type=room&limit=200").json()["listings"]}
    assert listed[adv]["advertised"] is True and listed[plain]["advertised"] is False
    assert listed[adv]["host"] == by_id[adv]["host"] and listed[adv]["agent_address"].startswith("ra_where2@")
    pub, other = _topic(client, "ra_where2", "wh-pub"), _topic(client, "ra_where2", "wh-plain")
    client.post(f"/agoras/topics/{pub}/advertise", json=_topic_advert(client, "ra_where2", pub),
                headers=_as("ra_where2"))
    topics = {t["slug"]: t for t in client.get("/agoras/topics", headers=_as("ra_where2")).json()["topics"]}
    assert topics[pub]["advertised"] is True and topics[other]["advertised"] is False and topics[pub]["host"]
    assert client.get(f"/agoras/topics/{pub}", headers=_as("ra_where2")).json()["advertised"] is True
    assert client.get(f"/agoras/topics/{other}", headers=_as("ra_where2")).json()["advertised"] is False


def test_an_invite_only_topic_is_not_advertised(client):
    _register_keyed(client, "ta_inv")
    r = client.post("/agoras/topics", json={"name": "Club", "description": "x", "slug": "ta-inv-club",
                                            "visibility": "public", "gate_type": "invite"}, headers=_as("ta_inv"))
    assert r.status_code == 200, r.text
    assert client.post("/agoras/topics/ta-inv-club/advertise", json={"signature": "00" * 64},
                       headers=_as("ta_inv")).status_code == 400


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


# ─── P249: a relay pulls the adverts of the relays it links to ───────────────

PEER = "https://peer-p249.example"


def _peer_feed(kind: str, items: list, priv, origin: str = PEER) -> dict:
    """A linked relay's rooms or topics feed, signed with its relay key."""
    from emporia.identity import federation_signable_bytes
    return {"origin_relay": origin, "origin_public_key_hex": priv.public_key().public_bytes_raw().hex(),
            "signature": priv.sign(federation_signable_bytes(origin, kind, items)).hex(), kind: items}


def _remote_room(owner: str, room_id: str, **over) -> dict:
    fields = {"name": "Remote club", "description": "tips", "room_type": "private", "gate_type": "paid",
              "entry_fee_cents": 900, "currency": "USD", "max_members": None, "encrypted": False,
              "access_duration_days": 30, **over}
    key = get_public_key_hex(owner)
    stable = wire.stable_id_for(key)
    signature = sign(wire.signed_room_advert_payload(
        stable, PEER, room_id, *(fields[f] for f in wire.ROOM_ADVERT_FIELDS)), owner)
    return {"room_id": room_id, **fields, "join_url": f"{PEER}/rooms/{room_id}/join",
            "created_at": "2026-10-01T00:00:00+00:00", "owner_public_key_hex": key, "owner_stable_id": stable,
            "advert_signature": signature, "origin_relay": PEER, "owner_address": f"{owner}@peer-p249.example"}


def _remote_topic(owner: str, slug: str) -> dict:
    fields = {"name": "Remote topic", "description": "talk", "gate_type": "open", "entry_fee_cents": 0}
    key = get_public_key_hex(owner)
    stable = wire.stable_id_for(key)
    return {"slug": slug, **fields, "join_url": f"{PEER}/agoras/topics/{slug}", "owner_public_key_hex": key,
            "owner_stable_id": stable, "origin_relay": PEER, "owner_address": f"{owner}@peer-p249.example",
            "advert_signature": sign(wire.signed_topic_advert_payload(
                stable, PEER, slug, *(fields[f] for f in wire.TOPIC_ADVERT_FIELDS)), owner)}


async def _pull(feeds: dict, priv, *, pin: bool = True):
    from unittest.mock import AsyncMock

    from emporia import relay_server as rs

    async def fake(url, **kw):
        kind = url.rsplit("/", 1)[1]
        return _peer_feed(kind, feeds.get(kind, []), priv)

    if pin:
        rs._FEDERATION_PEER_PUBKEYS[PEER] = priv.public_key().public_bytes_raw().hex()
    rs._FEDERATION_LAST_SYNC[PEER] = {"ok": True, "imported": 0}
    with patch("emporia.relay_server._fetch_federation_batch", AsyncMock(side_effect=fake)):
        await rs._pull_federated_adverts(PEER)
    return rs._FEDERATION_LAST_SYNC[PEER]


async def test_a_pulled_advert_is_verified_stored_served_and_dropped_when_withdrawn(client):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import relay_server as rs
    _register_keyed(client, "pa_reader")
    priv = ed25519.Ed25519PrivateKey.generate()
    good = _remote_room("pa_seller", "room_pa_good")
    forged = _remote_room("pa_seller", "room_pa_forged")
    forged["entry_fee_cents"] = 1          # the relay changed the fee after the owner signed
    other_relay = _remote_room("pa_seller", "room_pa_moved")
    other_relay["origin_relay"] = "https://elsewhere.example"
    bad_join = _remote_room("pa_seller", "room_pa_join")
    bad_join["join_url"] = "https://elsewhere.example/pay"
    topic = _remote_topic("pa_seller", "pa-topic")
    sync = await _pull({"rooms": [good, forged, other_relay, bad_join], "topics": [topic]}, priv)
    assert sync["adverts"] == 3 and sync["adverts_refused"] == 2 and sync["adverts_refused_why"]

    rooms = client.get("/rooms/elsewhere", headers=_as("pa_reader")).json()["rooms"]
    assert {x["room_id"] for x in rooms} == {"room_pa_good", "room_pa_join"}
    one = next(x for x in rooms if x["room_id"] == "room_pa_good")
    assert one["relay"] == PEER and one["host"] == "peer-p249.example" and one["verified"] is True
    assert one["owner_fingerprint"] and one["owner_address"].startswith("pa_seller@")
    assert (one["name"], one["entry_fee_cents"], one["gate_type"]) == ("Remote club", 900, "paid")
    # the join link is built from the signed origin, not the feed's own text
    assert next(x for x in rooms if x["room_id"] == "room_pa_join")["join_url"] == f"{PEER}/rooms/room_pa_join/join"
    topics = client.get("/agoras/topics/elsewhere", headers=_as("pa_reader")).json()["topics"]
    assert [t["slug"] for t in topics] == ["pa-topic"] and topics[0]["join_url"] == f"{PEER}/agoras/topics/pa-topic"

    # the next pull no longer carries two of them: those are gone here
    sync = await _pull({"rooms": [good], "topics": []}, priv)
    assert [x["room_id"] for x in client.get("/rooms/elsewhere", headers=_as("pa_reader")).json()["rooms"]] == \
        ["room_pa_good"]
    assert client.get("/agoras/topics/elsewhere", headers=_as("pa_reader")).json()["topics"] == []
    assert "adverts_refused" not in sync
    # unlinking the relay drops what it advertised
    with patch.object(rs, "FEDERATED_RELAYS", []):
        rs.drop_unlinked_federated_rows()
    assert client.get("/rooms/elsewhere", headers=_as("pa_reader")).json()["rooms"] == []


async def test_a_feed_not_signed_by_the_pinned_relay_adds_nothing(client):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import relay_server as rs
    _register_keyed(client, "pa_reader2")
    pinned, impostor = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    room = _remote_room("pa_seller2", "room_pa_imp")
    rs._FEDERATION_PEER_PUBKEYS[PEER] = pinned.public_key().public_bytes_raw().hex()
    sync = await _pull({"rooms": [room]}, impostor, pin=False)
    assert "not from the relay" in sync["adverts_error"]
    assert client.get("/rooms/elsewhere", headers=_as("pa_reader2")).json()["rooms"] == []


def test_the_elsewhere_lists_need_a_caller(client):
    assert client.get("/rooms/elsewhere").status_code == 401
    assert client.get("/agoras/topics/elsewhere").status_code == 401


def test_the_advertise_tool_and_the_elsewhere_lists(client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(client, "ra_tool")
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://relay-under-test")
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda method, url, **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers=_as("ra_tool"),
        **{k: v for k, v in kw.items() if k in ("json", "params")}))

    def fake_get(url, timeout=10, **kw):
        return client.get(url[len("http://relay-under-test"):])

    with patch("httpx.get", fake_get):
        paid = mcp_server.create_room("sell", "ra_tool", room_type="private", gate_type="paid", entry_fee_cents=300)
        invite = mcp_server.create_room("closed", "ra_tool", room_type="private", gate_type="invite")
        topic = mcp_server.create_agora_topic("Tool topic", "ra_tool", slug="ra-tool-topic")
        pid, iid = paid["room"]["room_id"], invite["room"]["room_id"]
        assert paid["advertised"] is False
        on = mcp_server.advertise("room", pid, agent_id="ra_tool")
        assert on == {"success": True, "kind": "room", "ident": pid, "advertised": True}
        refused = mcp_server.advertise("room", iid, agent_id="ra_tool")
        assert refused["success"] is False and refused["advertised"] is False and "invite-only" in refused["why"]
        assert mcp_server.advertise("topic", topic["slug"], agent_id="ra_tool")["advertised"] is True
        listed = mcp_server.list_rooms(viewer_id="ra_tool")["rooms"]
        assert {x["room_id"]: x["advertised"] for x in listed}[pid] is True
        assert [t["advertised"] for t in mcp_server.list_agora_topics()["topics"]
                if t["slug"] == topic["slug"]] == [True]
        hidden = mcp_server.create_agora_topic("Tool private", "ra_tool", slug="ra-tool-private",
                                               visibility="private")
        assert hidden["slug"] in {t["slug"] for t in mcp_server.list_agora_topics()["topics"]}
        assert hidden["slug"] not in {t["slug"] for t in client.get("/agoras/topics").json()["topics"]}
        assert mcp_server.list_rooms(viewer_id="ra_tool", elsewhere=True)["rooms"] == []
        off = mcp_server.advertise("room", pid, on=False, agent_id="ra_tool")
        assert off["advertised"] is False and off["success"] is True
        assert mcp_server.advertise("sky", "x", agent_id="ra_tool")["success"] is False
    assert pid not in {x["room_id"] for x in _rooms(client)["rooms"]}


def test_a_private_topic_is_listed_only_to_those_in_it(client):
    for who in ("pt_owner", "pt_guest", "pt_stranger"):
        _register_keyed(client, who)
    r = client.post("/agoras/topics", json={"name": "Inner", "description": "x", "slug": "pt-inner",
                                            "visibility": "private", "gate_type": "invite"}, headers=_as("pt_owner"))
    assert r.status_code == 200, r.text
    assert client.post("/agoras/topics/pt-inner/invite", json={"invitee_id": "pt_guest"},
                       headers=_as("pt_owner")).status_code == 200

    def listed(headers: dict) -> set[str]:
        return {t["slug"] for t in client.get("/agoras/topics?limit=200", headers=headers).json()["topics"]}

    assert "pt-inner" in listed(_as("pt_owner")) and "pt-inner" in listed(_as("pt_guest"))
    assert "pt-inner" not in listed(_as("pt_stranger")) and "pt-inner" not in listed({})
    stranger_private = client.get("/agoras/topics?visibility=private&limit=200", headers=_as("pt_stranger"))
    assert "pt-inner" not in {t["slug"] for t in stranger_private.json()["topics"]}
