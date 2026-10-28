"""P245: an agent changes its key and stays itself. Each change is a step signed
by the key it replaces; the steps are the agent's key log. Verifiers pick the
key that was the agent's when something was signed, pins follow the log, and a
log a relay made up is no log."""
import asyncio
import os
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import HTTPException
from fastapi.testclient import TestClient

from emporia import federation_dm as fd
from emporia import identity, key_rotation, wire
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import (
    get_public_key_hex,
    sign,
    sign_payload,
    verify,
    verify_agora_comment,
    verify_agora_post,
    verify_dm_message,
    verify_room_acts,
    verify_room_message,
)
from emporia.relay_server import app
from tests.test_emporia import (
    _make_peer_batch,
    _register_keyed,
    _rotate_keyed,
    _send_dm_raw,
    _signed_comment,
    _signed_invite,
    _signed_kick,
    _signed_post,
    _signed_room,
)
from tests.test_signed_open_sessions import _session

ORIGIN = "https://kr-relay.example"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51450))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _profile(client, agent_id: str, viewer: str = "") -> dict:
    r = client.get(f"/agents/{agent_id}", headers=_as(viewer or agent_id))
    assert r.status_code == 200, r.text
    return r.json()


def _key_for(client, agent_id: str, viewer: str) -> identity.SigningKey:
    """The signing key a client ends up with for `agent_id`: pinned, and aware of the keys it replaced."""
    body = _profile(client, agent_id, viewer)
    return identity.pinned_signing_key(agent_id, ORIGIN, body["public_key_hex"], body["stable_id"],
                                       key_log=body["key_log"])


def _topic(client, owner: str) -> str:
    r = client.post("/agoras/topics", json={"name": f"{owner} topic", "visibility": "public"}, headers=_as(owner))
    assert r.status_code == 200, r.text
    return r.json()["slug"]


# --- keys and logs made offline ---------------------------------------------

def _keys(n: int) -> list[ed25519.Ed25519PrivateKey]:
    return [ed25519.Ed25519PrivateKey.generate() for _ in range(n)]


def _hex(priv) -> str:
    return priv.public_key().public_bytes_raw().hex()


def _step(stable_id: str, old, new, at: str, signer=None) -> dict:
    return {"old": _hex(old), "new": _hex(new), "at": at, "by": "key",
            "signature": sign_payload(signer or old, wire.signed_key_rotation_payload(
                stable_id, _hex(old), _hex(new), at))}


def _at(minutes: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


def _chain(n: int = 3):
    keys = _keys(n)
    stable = wire.stable_id_for(_hex(keys[0]))
    return keys, stable, [_step(stable, keys[i], keys[i + 1], _at(10 * (i + 1))) for i in range(n - 1)]


# --- the log itself -----------------------------------------------------------

def test_a_valid_log_gives_each_key_its_time():
    keys, stable, log = _chain()
    windows = identity.key_log_windows(stable, log)
    assert [w["key"] for w in windows] == [_hex(k) for k in keys]
    assert windows[0]["from"] == "" and windows[-1]["until"] == ""
    assert identity.key_at(windows, _at(5)) == _hex(keys[0])
    assert identity.key_at(windows, _at(10)) == _hex(keys[1])          # the moment a key takes over is the new key's
    assert identity.key_at(windows, _at(15)) == _hex(keys[1])
    assert identity.key_at(windows, _at(500)) == _hex(keys[2])
    assert identity.key_at(windows, "") == _hex(keys[2])               # no time: now
    assert identity.key_at(windows, (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15)).timestamp()) == _hex(keys[1])
    assert identity.key_at(windows, "not a time") == ""
    assert identity.key_at([], _at(5)) == ""


def test_an_agent_that_never_changed_key_has_an_empty_log():
    (k,) = _keys(1)
    stable = wire.stable_id_for(_hex(k))
    assert identity.key_log_windows(stable, [], _hex(k)) == [{"key": _hex(k), "from": "", "until": ""}]
    assert identity.key_log_windows(stable, [], _hex(_keys(1)[0])) == []     # a key that is not the genesis one
    assert identity.key_log_windows(stable, []) == []


def test_a_forged_log_is_no_log():
    keys, stable, log = _chain(3)
    k0, k1, k2 = keys
    assert identity.key_log_windows(stable, log)
    # a step signed by the wrong key
    assert identity.key_log_windows(stable, [log[0], _step(stable, k1, k2, _at(20), signer=_keys(1)[0])]) == []
    assert identity.key_log_windows(stable, [_step(stable, k0, k1, _at(10), signer=k2), log[1]]) == []
    # a fork: the second step does not start where the first ended
    other = _keys(1)[0]
    assert identity.key_log_windows(stable, [log[0], _step(stable, other, k2, _at(20))]) == []
    # a rollback: a key comes back
    assert identity.key_log_windows(stable, [log[0], _step(stable, k1, k0, _at(20))]) == []
    # time that does not go forward
    assert identity.key_log_windows(stable, [log[0], _step(stable, k1, k2, _at(10))]) == []
    assert identity.key_log_windows(stable, [log[0], _step(stable, k1, k2, _at(5))]) == []
    # a log that does not start at this identity's first key
    assert identity.key_log_windows(wire.stable_id_for(_hex(other)), log) == []
    # a step moved to another identity, a changed time, a step kind this verifier does not know
    assert identity.key_log_windows(wire.stable_id_for(_hex(other)), [_step(stable, k0, k1, _at(10))]) == []
    assert identity.key_log_windows(stable, [{**log[0], "at": _at(11)}, log[1]]) == []
    assert identity.key_log_windows(stable, [{**log[0], "by": "recovery"}, log[1]]) == []
    assert identity.key_log_windows(stable, [{**log[0], "new": "zz"}]) == []
    assert identity.key_log_windows(stable, "not a list") == []
    assert identity.key_log_windows(stable, [None]) == []
    assert identity.key_log_windows(stable, log * 60) == []             # too long


def test_a_log_extends_what_it_began_with():
    _, _, log = _chain(4)
    assert identity.key_log_extends([], log) and identity.key_log_extends(log[:1], log) and identity.key_log_extends(log, log)
    assert not identity.key_log_extends(log, log[:1])
    assert not identity.key_log_extends(log[:2], [log[0], {**log[1], "signature": "x"}, log[2]])
    assert identity.key_log_extends([{k: v for k, v in log[0].items() if k != "by"}], log)   # "by" defaults to "key"


# --- the route ----------------------------------------------------------------

def test_a_key_change_through_the_route_is_served_as_a_log(client):
    _register_keyed(client, "kr_route")
    before = _profile(client, "kr_route")
    assert before["key_log"] == []
    first_key = before["public_key_hex"]
    second = _rotate_keyed(client, "kr_route")
    third = _rotate_keyed(client, "kr_route")
    after = _profile(client, "kr_route")
    assert after["stable_id"] == before["stable_id"] and after["public_key_hex"] == third
    assert [s["old"] for s in after["key_log"]] == [first_key, second]
    windows = identity.key_log_windows(after["stable_id"], after["key_log"])
    assert [w["key"] for w in windows] == [first_key, second, third]
    assert after["fingerprint"] != before["fingerprint"]
    # served the same way where a client looks a key up
    enc = client.get("/agents/kr_route/encryption-key", headers=_as("kr_route")).json()
    assert enc["key_log"] == after["key_log"] and enc["stable_id"] == after["stable_id"]
    assert enc["public_key_hex"] == third


def test_a_key_change_is_the_agents_own_signed_step(client):
    for a in ("kr_signer", "kr_other"):
        _register_keyed(client, a)
    stable = _profile(client, "kr_signer")["stable_id"]
    old = get_public_key_hex("kr_signer")
    new = _hex(_keys(1)[0])

    def post(caller, signer, *, at=None, key=new, old_key=old, sid=stable):
        at = at or datetime.now(UTC).isoformat()
        return client.post("/agents/kr_signer/key-rotations", headers=_as(caller), json={
            "new_public_key_hex": key, "at": at,
            "signature": sign(wire.signed_key_rotation_payload(sid, old_key, key, at), signer)})

    assert post("kr_other", "kr_other").status_code == 403          # not the agent
    assert post("kr_signer", "kr_other").status_code == 403         # not signed by its key
    assert post("kr_signer", "kr_signer", sid=_profile(client, "kr_other")["stable_id"]).status_code == 403   # for another identity
    assert post("kr_signer", "kr_signer", key=old).status_code == 409                 # to the key it has
    assert post("kr_signer", "kr_signer", key="not a key").status_code == 400
    stale = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    assert post("kr_signer", "kr_signer", at=stale).status_code == 400
    ahead = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    assert post("kr_signer", "kr_signer", at=ahead).status_code == 400
    assert post("kr_signer", "kr_signer", at="yesterday").status_code == 400
    assert _profile(client, "kr_signer")["public_key_hex"] == old and _profile(client, "kr_signer")["key_log"] == []
    assert post("kr_signer", "kr_signer").status_code == 200


def test_a_key_that_is_anyones_cannot_be_taken(client):
    for a in ("kr_taker", "kr_held", "kr_moved"):
        _register_keyed(client, a)
    held = get_public_key_hex("kr_held")
    moved_genesis = get_public_key_hex("kr_moved")
    _rotate_keyed(client, "kr_moved")

    def rotate_to(agent, key):
        stable = _profile(client, agent)["stable_id"]
        old = get_public_key_hex(agent)
        at = datetime.now(UTC).isoformat()
        return client.post(f"/agents/{agent}/key-rotations", headers=_as(agent), json={
            "new_public_key_hex": key, "at": at,
            "signature": sign(wire.signed_key_rotation_payload(stable, old, key, at), agent)})

    assert rotate_to("kr_taker", held).status_code == 409               # another agent's key now
    assert rotate_to("kr_taker", moved_genesis).status_code == 409      # the key another agent's identity was made from
    # nor can an agent go back to a key it held
    own_genesis = get_public_key_hex("kr_taker")
    _rotate_keyed(client, "kr_taker")
    assert rotate_to("kr_taker", own_genesis).status_code == 409
    # and a key some agent holds after rotating cannot be registered anew under another name
    r = client.post("/agents/register", json={"agent_id": "kr_squatter", "public_key_hex": get_public_key_hex("kr_taker")})
    assert r.status_code == 409 or r.status_code == 400


def test_a_step_must_be_later_than_the_one_before(client):
    _register_keyed(client, "kr_order")
    stable = _profile(client, "kr_order")["stable_id"]
    _rotate_keyed(client, "kr_order")
    last = _profile(client, "kr_order")["key_log"][-1]["at"]
    earlier = (datetime.fromisoformat(last) - timedelta(seconds=1)).isoformat()
    new = _hex(_keys(1)[0])
    r = client.post("/agents/kr_order/key-rotations", headers=_as("kr_order"), json={
        "new_public_key_hex": new, "at": earlier,
        "signature": sign(wire.signed_key_rotation_payload(stable, get_public_key_hex("kr_order"), new, earlier),
                          "kr_order")})
    assert r.status_code == 409


def _session_token(client, agent_id: str, priv) -> int:
    ch = client.post("/dashboard/challenge").json()
    sig = priv.sign(identity.challenge_bytes(ch["nonce"])).hex()
    return client.post("/dashboard/session", json=wire.dashboard_session_body(agent_id, ch["challenge_id"], sig)).status_code


def test_sign_in_uses_the_new_key_at_once_and_not_the_old(client):
    _register_keyed(client, "kr_signin")
    old_priv = ed25519.Ed25519PrivateKey.from_private_bytes(identity._key_path("kr_signin").read_bytes())
    assert _session_token(client, "kr_signin", old_priv) == 200
    _rotate_keyed(client, "kr_signin")
    new_priv = ed25519.Ed25519PrivateKey.from_private_bytes(identity._key_path("kr_signin").read_bytes())
    assert _session_token(client, "kr_signin", new_priv) == 200
    assert _session_token(client, "kr_signin", old_priv) != 200
    # and the agent's signed acts are checked against the new key
    slug = _topic(client, "kr_signin")
    assert client.post(f"/agoras/topics/{slug}/posts", json=_signed_post("kr_signin", slug, {"title": "t", "content": "c"}),
                       headers=_as("kr_signin")).status_code == 200


# --- what was signed before still checks; what is signed after checks too -------

def test_posts_comments_messages_and_acts_check_against_the_key_of_their_time(client):
    for a in ("kr_old_hand", "kr_reader", "kr_pal"):
        _register_keyed(client, a)
    hand = "kr_old_hand"
    slug = _topic(client, hand)
    room_id = client.post("/rooms", json=_signed_room(hand, {"name": "kr room", "room_type": "private",
                                                             "gate_type": "invite"}), headers=_as(hand)).json()["room_id"]
    thread = client.post("/dm/start", json={"to_agent": "kr_pal"}, headers=_as(hand)).json()["thread_id"]

    def say_room(text):
        nonce = wire.new_message_nonce()
        r = client.post(f"/rooms/{room_id}/message", headers=_as(hand), json={
            "content": text, "nonce": nonce, "signature": sign(wire.signed_room_message_payload(room_id, text, "chat", nonce), hand)})
        assert r.status_code == 200, r.text

    def write_everything(tag):
        post_id = client.post(f"/agoras/topics/{slug}/posts", json=_signed_post(hand, slug, {"title": tag, "content": tag}),
                              headers=_as(hand)).json()["post_id"]
        assert client.post(f"/agoras/posts/{post_id}/comments", json=_signed_comment(hand, post_id, {"content": tag}),
                           headers=_as(hand)).status_code == 200
        say_room(tag)
        assert _send_dm_raw(client, thread, hand, tag).status_code == 200
        return post_id

    old_post = write_everything("before")
    assert client.post(f"/rooms/{room_id}/invite", json=_signed_invite(hand, room_id, {"invitee_id": "kr_pal"}),
                       headers=_as(hand)).status_code == 200
    assert client.post(f"/rooms/{room_id}/join", json={"agent_id": "kr_pal"}, headers=_as("kr_pal")).status_code == 200
    old_key = get_public_key_hex(hand)
    new_key = _rotate_keyed(client, hand)
    new_post = write_everything("after")
    assert client.post(f"/rooms/{room_id}/kick", json=_signed_kick(hand, room_id, {"target_agent_id": "kr_pal"}),
                       headers=_as(hand)).status_code == 200

    key = _key_for(client, hand, "kr_reader")
    assert key == new_key and len(key.windows) == 2

    # Agora
    for post_id, tag in ((old_post, "before"), (new_post, "after")):
        post = client.get(f"/agoras/posts/{post_id}", headers=_as("kr_reader")).json()
        assert verify_agora_post(slug, post, key) is True, tag
        assert verify_agora_comment(post["comments"][0], key) is True, tag
    old = client.get(f"/agoras/posts/{old_post}", headers=_as("kr_reader")).json()
    assert verify_agora_post(slug, old, new_key) is False                  # the new key alone does not explain it
    assert verify_agora_post(slug, old, identity.SigningKey(old_key)) is True

    # room messages and DMs
    msgs = client.get(f"/rooms/{room_id}/messages", headers=_as(hand)).json()
    msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
    assert [verify_room_message(room_id, m, key) for m in msgs] == [True, True]
    assert [verify_room_message(room_id, m, new_key) for m in msgs] == [False, True]
    dms = client.get(f"/dm/{thread}/messages", params={"agent_id": "kr_pal"}, headers=_as("kr_pal")).json()["messages"]
    assert [verify_dm_message(thread, m, key) for m in dms] == [True, True]
    assert [verify_dm_message(thread, m, new_key) for m in dms] == [False, True]

    # room acts: created and invited before, kicked after
    room = client.get(f"/rooms/{room_id}", headers=_as(hand)).json()
    checked = verify_room_acts(room, key)
    assert checked["settings"] is True and [a["verified"] for a in checked["acts"]] == [True, True]
    assert [a["act"] for a in checked["acts"]] == ["invite", "kick"]
    assert verify_room_acts(room, new_key)["settings"] is False


# --- listings, offers and adverts need the current key ---------------------------

def _listing(client, agent_id: str, title: str) -> dict:
    stable = _profile(client, agent_id)["stable_id"]
    body = {"title": title, "description": "", "listing_type": "service", "payment_mode": "free", "price_usd": "0",
            "module_type": None, "metadata": {}, "expires_at": wire.listing_expires_at(72),
            "listing_nonce": wire.new_message_nonce()}
    body["signature"] = sign(wire.signed_listing_payload(
        stable, client.get("/health").json()["relay_url"], body["listing_nonce"], title, "", "service", "free", "0",
        None, {}, body["expires_at"]), agent_id)
    r = client.post("/listings", json=body, headers=_as(agent_id))
    assert r.status_code == 200, r.text
    return r.json()


def _feed_listing(client, listing_id: str):
    return next((x for x in client.get("/gaming/v1/federate/listings").json()["listings"]
                 if x["listing_id"] == listing_id), None)


def test_a_listing_is_dropped_from_the_feed_when_its_key_is_replaced_until_signed_again(client):
    _register_keyed(client, "kr_seller")
    old = _listing(client, "kr_seller", "Before")["listing_id"]
    item = _feed_listing(client, old)
    assert wire.listing_verdict(item, verify) == ""
    _rotate_keyed(client, "kr_seller")
    assert _feed_listing(client, old) is None
    now_log = _profile(client, "kr_seller")["key_log"]
    assert "since replaced" in wire.listing_verdict({**item, "key_log": now_log}, verify)
    fresh = _listing(client, "kr_seller", "After")["listing_id"]
    new_item = _feed_listing(client, fresh)
    assert wire.listing_verdict(new_item, verify) == "" and new_item["key_log"] == now_log
    assert wire.listing_verdict({**new_item, "key_log": []}, verify) != ""         # without the log the key proves nothing
    forged = [{**now_log[0], "signature": now_log[0]["signature"][::-1]}]
    assert wire.listing_verdict({**new_item, "key_log": forged}, verify) != ""


async def test_a_listing_pulled_from_a_rotated_agent_is_kept_with_its_log_and_served_with_it(client):
    keys, stable, log = _chain(2)
    new_priv = keys[1]
    nonce = wire.new_message_nonce()
    item = {"title": "Rotated seller", "description": "", "listing_type": "service", "agent_id": "kr_remote",
            "payment_mode": "free", "price_usd": "0", "module_type": None, "metadata": {},
            "expires_at": wire.listing_expires_at(72), "created_at": "2026-01-01T00:00:00+00:00",
            "origin_relay": "https://peer-p245.example", "listing_id": wire.listing_id_for(stable, nonce),
            "listing_nonce": nonce, "agent_stable_id": stable, "agent_public_key_hex": _hex(new_priv), "key_log": log}
    item["signature"] = sign_payload(new_priv, wire.listing_fields_as_signed(item))
    assert wire.listing_verdict(item, verify) == ""
    peer = "https://peer-p245.example"
    batch, _ = _make_peer_batch(peer, [item])
    rs._FEDERATION_PEER_PUBKEYS.pop(peer, None)
    with patch("emporia.relay_server._fetch_federation_batch", new_callable=AsyncMock, return_value=batch):
        assert await rs._pull_federated_listings(peer) == 1
    served = next(x for x in client.get("/listings?limit=200").json()["listings"] if x["listing_id"] == item["listing_id"])
    assert served["key_log"] == log and wire.listing_verdict(served, verify) == ""

    # without its log the same listing is refused on import
    nonce2 = wire.new_message_nonce()
    bare = {**item, "listing_nonce": nonce2, "listing_id": wire.listing_id_for(stable, nonce2), "key_log": []}
    bare["signature"] = sign_payload(new_priv, wire.listing_fields_as_signed(bare))
    batch, _ = _make_peer_batch(peer, [bare])
    rs._FEDERATION_PEER_PUBKEYS.pop(peer, None)
    with patch("emporia.relay_server._fetch_federation_batch", new_callable=AsyncMock, return_value=batch):
        assert await rs._pull_federated_listings(peer) == 0
    assert "stable id" in next(iter(rs._FEDERATION_LAST_SYNC[peer]["refused_why"]))


def test_an_offer_or_an_advert_signed_with_a_replaced_key_leaves_the_feeds_until_signed_again(client):
    _register_keyed(client, "kr_host")
    origin = client.get("/health").json()["relay_url"]

    def offer(session_id):
        stable = _profile(client, "kr_host")["stable_id"]
        s = client.get(f"/sessions/{session_id}", headers=_as("kr_host")).json()
        signed = wire.signed_open_session_payload(stable, origin, session_id, s["module_type"], s["config"], s["payment_rules"])
        return client.post(f"/sessions/{session_id}/offer", json={"signature": sign(signed, "kr_host")}, headers=_as("kr_host"))

    def advertise_room(room_id):
        stable = _profile(client, "kr_host")["stable_id"]
        r = client.get(f"/rooms/{room_id}", headers=_as("kr_host")).json()
        signed = wire.signed_room_advert_payload(stable, origin, room_id, *(r[f] for f in wire.ROOM_ADVERT_FIELDS))
        return client.post(f"/rooms/{room_id}/advertise", json={"signature": sign(signed, "kr_host")}, headers=_as("kr_host"))

    def advertise_topic(slug):
        stable = _profile(client, "kr_host")["stable_id"]
        t = client.get(f"/agoras/topics/{slug}", headers=_as("kr_host")).json()
        signed = wire.signed_topic_advert_payload(stable, origin, slug, *(t[f] for f in wire.TOPIC_ADVERT_FIELDS))
        return client.post(f"/agoras/topics/{slug}/advertise", json={"signature": sign(signed, "kr_host")}, headers=_as("kr_host"))

    session_id = _session(client, "kr_host")
    room_id = client.post("/rooms", json=_signed_room("kr_host", {"name": "kr open room"}), headers=_as("kr_host")).json()["room_id"]
    slug = _topic(client, "kr_host")
    assert offer(session_id).status_code == 200 and advertise_room(room_id).status_code == 200
    assert advertise_topic(slug).status_code == 200

    def in_feeds():
        lobby = client.get("/federation/v1/open-sessions").json()
        rooms, topics = client.get("/federation/v1/rooms").json(), client.get("/federation/v1/topics").json()
        return (session_id in {x["session_id"] for x in lobby["open_sessions"]},
                room_id in {x["room_id"] for x in rooms["rooms"]}, slug in {x["slug"] for x in topics["topics"]}), \
            (lobby, rooms, topics)

    seen, (lobby, rooms, topics) = in_feeds()
    assert seen == (True, True, True)
    assert wire.open_session_verdict(lobby["open_sessions"][0], lobby["origin_relay"], verify) == ""
    _rotate_keyed(client, "kr_host")
    assert in_feeds()[0] == (False, False, False)
    assert offer(session_id).status_code == 200 and advertise_room(room_id).status_code == 200
    assert advertise_topic(slug).status_code == 200
    seen, (lobby, rooms, topics) = in_feeds()
    assert seen == (True, True, True)
    mine = next(x for x in lobby["open_sessions"] if x["session_id"] == session_id)
    assert mine["key_log"] and wire.open_session_verdict(mine, lobby["origin_relay"], verify) == ""
    room = next(x for x in rooms["rooms"] if x["room_id"] == room_id)
    topic = next(x for x in topics["topics"] if x["slug"] == slug)
    assert wire.room_advert_verdict(room, rooms["origin_relay"], verify) == ""
    assert wire.topic_advert_verdict(topic, topics["origin_relay"], verify) == ""
    for bad in ({"key_log": []}, {"key_log": mine["key_log"][::-1] + mine["key_log"]}):
        assert wire.open_session_verdict({**mine, **bad}, lobby["origin_relay"], verify) != ""
        assert wire.room_advert_verdict({**room, **bad}, rooms["origin_relay"], verify) != ""
        assert wire.topic_advert_verdict({**topic, **bad}, topics["origin_relay"], verify) != ""


# --- pins follow a log, and only a real one ---------------------------------------

def _pin(name: str) -> tuple[str, str]:
    return f"{name}@kr.example", "https://kr.example"


def test_a_pin_follows_the_agents_own_log_to_a_new_key():
    keys, stable, log = _chain(3)
    pid, url = _pin("kr_pin_follow")
    changed: list[str] = []
    first = identity.pinned_signing_key("kr_pin_follow", url, _hex(keys[0]), stable, changed)
    assert first == _hex(keys[0]) and not first.windows
    got = identity.pinned_signing_key("kr_pin_follow", url, _hex(keys[2]), stable, changed, log)
    assert got == _hex(keys[2]) and changed == []
    assert [w["key"] for w in got.windows] == [_hex(k) for k in keys]
    assert identity.signing_key_for(got, _at(5)) == _hex(keys[0])
    pin = identity.load_peer_signing_keys()[pid]
    assert pin["public_key_hex"] == _hex(keys[2]) and pin["key_log"] == log
    # later reads with no log keep what is known
    again = identity.pinned_signing_key("kr_pin_follow", url, _hex(keys[2]), stable, changed)
    assert again == _hex(keys[2]) and len(again.windows) == 3 and changed == []


def test_a_client_that_first_meets_an_agent_after_its_change_still_checks_older_things():
    keys, stable, log = _chain(2)
    key = identity.pinned_signing_key("kr_pin_late", "https://kr.example", _hex(keys[1]), stable, None, log)
    assert key == _hex(keys[1]) and identity.signing_key_for(key, _at(1)) == _hex(keys[0])
    # a log that does not end at the key served is not kept
    other = identity.pinned_signing_key("kr_pin_late2", "https://kr.example", _hex(keys[0]), stable, None, log)
    assert not other.windows and "key_log" not in identity.load_peer_signing_keys()["kr_pin_late2@kr.example"]


@pytest.mark.parametrize("how", ["no_log", "bad_signature", "other_identity", "rollback", "fork", "shorter"])
def test_a_pin_does_not_follow_a_log_that_is_not_the_agents(how):
    keys, stable, log = _chain(3)
    attacker = _keys(1)[0]
    name = f"kr_pin_{how}"
    _, url = _pin(name)
    # the pin is at keys[1] with the first step known
    identity.pinned_signing_key(name, url, _hex(keys[1]), stable, None, log[:1])
    served_key, served_log, sid = _hex(keys[2]), log, stable
    if how == "no_log":
        served_log = []
    elif how == "bad_signature":
        served_log = [log[0], {**log[1], "signature": sign_payload(attacker, wire.signed_key_rotation_payload(
            stable, _hex(keys[1]), _hex(keys[2]), log[1]["at"]))}]
    elif how == "other_identity":
        astable = wire.stable_id_for(_hex(attacker))
        served_key, sid, served_log = _hex(attacker), astable, []
    elif how == "rollback":
        served_key, served_log = _hex(keys[0]), log[:1] + [_step(stable, keys[1], keys[0], _at(20))]
    elif how == "fork":     # the first step is not the one known
        fork = _step(stable, keys[0], attacker, _at(10))
        served_key, served_log = _hex(attacker), [fork]
    elif how == "shorter":  # a log that stops before the pinned key
        served_key, served_log = _hex(keys[0]), []
    changed: list[str] = []
    got = identity.pinned_signing_key(name, url, served_key, sid, changed, served_log)
    assert got == _hex(keys[1]) and changed == [name]
    assert identity.load_peer_signing_keys()[f"{name}@kr.example"]["public_key_hex"] == _hex(keys[1])


def test_the_sdk_follows_a_rotation_it_can_check_and_flags_one_it_cannot():
    keys, stable, log = _chain(2)
    served = {"public_key_hex": _hex(keys[0]), "stable_id": stable, "key_log": []}

    async def bearer(method, url, **kw):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: dict(served))
    agent = SimpleNamespace(relay_url="https://sdk-p245.example", _bearer_request=bearer)

    async def run():
        changed: list[str] = []
        first = await EmporiaAgent._sender_public_key(agent, "kr_sdk_peer", changed)
        served.update(public_key_hex=_hex(keys[1]), key_log=log)
        second = await EmporiaAgent._sender_public_key(agent, "kr_sdk_peer", changed)
        served.update(public_key_hex=_hex(_keys(1)[0]), key_log=[])
        third = await EmporiaAgent._sender_public_key(agent, "kr_sdk_peer", changed)
        return first, second, third, changed
    first, second, third, changed = asyncio.run(run())
    assert (first, second, third, changed) == (_hex(keys[0]), _hex(keys[1]), _hex(keys[1]), ["kr_sdk_peer"])
    assert identity.signing_key_for(second, _at(1)) == _hex(keys[0])


# --- remote agents pinned by the relay ---------------------------------------------

def _remote_record(host: str, name: str, priv, stable_id: str, log: list[dict]) -> dict:
    enc = os.urandom(32).hex()
    return {"address": f"{name}@{host}", "public_key_hex": _hex(priv), "stable_id": stable_id, "key_log": log,
            "encryption_public_key": enc,
            "encryption_key_signature": sign_payload(priv, wire.signed_encryption_key_payload(name, enc))}


def test_a_relay_follows_a_remote_agents_log_and_refuses_anything_else(client):
    host, peer = "kr-remote.example", "https://kr-remote.example"
    keys, stable, log = _chain(3)
    addr = f"kr_remote@{host}"
    fd.pin_remote(peer, _remote_record(host, "kr_remote", keys[0], stable, []))
    assert fd.remote(addr)["public_key_hex"] == _hex(keys[0])

    # a stranger's key under the same address, with a log of its own
    attacker = _keys(1)[0]
    astable = wire.stable_id_for(_hex(attacker))
    with pytest.raises(HTTPException) as e:
        fd.pin_remote(peer, _remote_record(host, "kr_remote", attacker, astable, []))
    assert e.value.status_code == 409
    # the right identity with a log that does not lead to the key it presents
    forged = [{**log[0], "signature": log[0]["signature"][::-1]}]
    with pytest.raises(HTTPException) as e:
        fd.pin_remote(peer, _remote_record(host, "kr_remote", keys[1], stable, forged))
    assert e.value.status_code == 422
    assert fd.remote(addr)["public_key_hex"] == _hex(keys[0])
    # its own signed steps
    fd.pin_remote(peer, _remote_record(host, "kr_remote", keys[1], stable, log[:1]))
    assert fd.remote(addr)["public_key_hex"] == _hex(keys[1])
    fd.pin_remote(peer, _remote_record(host, "kr_remote", keys[2], stable, log))
    row = fd.remote(addr)
    assert row["public_key_hex"] == _hex(keys[2]) and row["stable_id"] == stable
    # a rollback to an earlier key is not a change it can make
    with pytest.raises(HTTPException):
        fd.pin_remote(peer, _remote_record(host, "kr_remote", keys[1], stable, log[:1]))
    # served to clients that look the key up
    r = client.get(f"/agents/{addr}/encryption-key", headers=_as("kr_remote_viewer"))
    assert r.status_code == 200, r.text
    assert r.json()["key_log"] == log and r.json()["stable_id"] == stable and r.json()["public_key_hex"] == _hex(keys[2])


# --- the person's command ---------------------------------------------------------------

def _agent_with_file(client, tmp_path, name: str):
    priv = ed25519.Ed25519PrivateKey.generate()
    (tmp_path / f"{name}.priv").write_bytes(priv.private_bytes_raw())
    os.chmod(tmp_path / f"{name}.priv", 0o600)
    r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": _hex(priv)})
    assert r.status_code == 200, r.text
    return priv


def test_the_command_writes_the_new_key_and_keeps_the_old_file(client, tmp_path):
    old = _agent_with_file(client, tmp_path, "kr_cli")
    out = key_rotation.rotate_identity_key("kr_cli", tmp_path, client)
    assert out["installed"] and out["old_public_key"] == _hex(old)
    live = tmp_path / "kr_cli.priv"
    new = ed25519.Ed25519PrivateKey.from_private_bytes(live.read_bytes())
    assert _hex(new) == out["new_public_key"] != _hex(old)
    assert stat.S_IMODE(live.stat().st_mode) == 0o600
    kept = out["kept_old_key_file"]
    assert kept.name.startswith("kr_cli.priv.rotated-") and kept.read_bytes() == old.private_bytes_raw()
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert not (tmp_path / "kr_cli.priv.new").exists()
    served = client.get("/agents/kr_cli", headers=_as("kr_cli")).json()
    assert served["public_key_hex"] == _hex(new)
    assert identity.key_log_windows(served["stable_id"], served["key_log"])[-1]["key"] == _hex(new)
    # twice in a row: another file kept, never overwritten
    again = key_rotation.rotate_identity_key("kr_cli", tmp_path, client)
    assert again["kept_old_key_file"] != kept and kept.exists()
    assert len(list(tmp_path.glob("kr_cli.priv.rotated-*"))) == 2


def test_a_key_kept_elsewhere_is_not_touched(client, tmp_path):
    old = _agent_with_file(client, tmp_path, "kr_linked")
    (tmp_path / "kr_linked.priv").unlink()                   # it lives in the source, not a file
    source_env = {"KR_LINKED_KEY": old.private_bytes_raw().hex()}
    out = key_rotation.rotate_identity_key("kr_linked", tmp_path, client, source="env:KR_LINKED_KEY",
                                           source_env=source_env)
    assert out["installed"] is False and out["kept_old_key_file"] is None
    assert not (tmp_path / "kr_linked.priv").exists()
    new_file = tmp_path / "kr_linked.priv.new"
    assert out["new_key_file"] == new_file and stat.S_IMODE(new_file.stat().st_mode) == 0o600
    assert _hex(ed25519.Ed25519PrivateKey.from_private_bytes(new_file.read_bytes())) == out["new_public_key"]
    assert client.get("/agents/kr_linked", headers=_as("kr_linked")).json()["public_key_hex"] == out["new_public_key"]


def test_a_run_that_stopped_after_the_relay_took_the_key_finishes_with_the_key_on_disk(client, tmp_path):
    old = _agent_with_file(client, tmp_path, "kr_resume")

    class DropsTheAnswer:
        """The relay gets the request; the answer never arrives."""
        def __init__(self, inner):
            self.inner = inner

        def get(self, *a, **k):
            return self.inner.get(*a, **k)

        def post(self, url, **k):
            r = self.inner.post(url, **k)
            if "key-rotations" in url:
                raise ConnectionError("connection reset")
            return r

    with pytest.raises(ConnectionError):
        key_rotation.rotate_identity_key("kr_resume", tmp_path, DropsTheAnswer(client))
    assert (tmp_path / "kr_resume.priv.new").exists()                          # kept before the relay was asked
    assert (tmp_path / "kr_resume.priv").read_bytes() == old.private_bytes_raw()
    out = key_rotation.rotate_identity_key("kr_resume", tmp_path, client)
    assert out["installed"]
    on_relay = client.get("/agents/kr_resume", headers=_as("kr_resume")).json()
    assert on_relay["public_key_hex"] == out["new_public_key"] and len(on_relay["key_log"]) == 1
    assert _hex(ed25519.Ed25519PrivateKey.from_private_bytes((tmp_path / "kr_resume.priv").read_bytes())) == on_relay["public_key_hex"]


def test_a_refused_change_leaves_the_key_as_it_was(client, tmp_path):
    old = _agent_with_file(client, tmp_path, "kr_refused")
    other = _agent_with_file(client, tmp_path, "kr_refused_other")

    class Refuses:
        def __init__(self, inner):
            self.inner = inner

        def get(self, *a, **k):
            return self.inner.get(*a, **k)

        def post(self, url, **k):
            if "key-rotations" in url:
                k["json"] = {**k["json"], "new_public_key_hex": _hex(other)}      # a key that is another agent's
            return self.inner.post(url, **k)

    with pytest.raises(key_rotation.RotationError):
        key_rotation.rotate_identity_key("kr_refused", tmp_path, Refuses(client))
    assert (tmp_path / "kr_refused.priv").read_bytes() == old.private_bytes_raw()
    assert not (tmp_path / "kr_refused.priv.new").exists()


def test_the_command_asks_a_person_and_signs_the_agents_registration_again(client, tmp_path, monkeypatch, capsys):
    from emporia import cli
    old = _agent_with_file(client, tmp_path, "kr_person")
    ran = []

    class Installer:
        def harvest_env(self, profile):
            return {}

        def print_restart_instructions(self, profiles):
            print("restart it")

    monkeypatch.setattr(cli, "_agent", lambda name: (tmp_path, "kr_person", {"EMPORIA_AGENT_ID": "kr_person"}))
    monkeypatch.setattr(cli, "_key_file", lambda profile, agent_id, which: tmp_path / "kr_person.priv")
    monkeypatch.setattr(cli, "installer", lambda: Installer())
    monkeypatch.setattr(cli, "relay_url", lambda: "http://testserver")
    monkeypatch.setattr(cli, "_as_agent", lambda profile, code, **env: ran.append((code, env)) or SimpleNamespace(
        returncode=0, stdout="ok\n", stderr=""))
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.cmd_agent_rotate_key("kr_person", yes=True, http=client) == 1       # an agent's terminal is not a person's
    assert (tmp_path / "kr_person.priv").read_bytes() == old.private_bytes_raw()
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    assert cli.cmd_agent_rotate_key("kr_person", yes=True, http=client) == 0
    text = capsys.readouterr().out
    assert "kept as" in text and "signed again" in text and "restart it" in text
    assert "not compromised" in text and "delete the file" in text
    assert len(ran) == 1 and "register_agent" in ran[0][0]
    assert _hex(ed25519.Ed25519PrivateKey.from_private_bytes((tmp_path / "kr_person.priv").read_bytes())) == \
        client.get("/agents/kr_person", headers=_as("kr_person")).json()["public_key_hex"]
