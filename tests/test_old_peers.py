"""A relay on an older protocol (Emporia 2.0.1 runs relay protocol 3): the refusals say so and say what to do,
never a bare 401 or "answered 401"."""
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from emporia import federation_dm as fd
from emporia import relay_server as rs

OLD_URL = "http://old-relay.test"


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51322))


def test_a_request_signed_the_old_way_is_told_to_upgrade(monkeypatch, client):
    monkeypatch.setattr(rs, "FEDERATED_RELAYS", [OLD_URL])
    monkeypatch.setattr(fd, "enabled", lambda: True)
    # what a 2.0.1 relay sends: a time and a signature, no nonce and no protocol header
    headers = {"X-Emporia-Relay": OLD_URL, "X-Emporia-Relay-Time": str(int(time.time())),
               "X-Emporia-Relay-Signature": "ab" * 64, "Content-Type": "application/json"}
    r = client.post("/federation/v1/dm", content=b"{}", headers=headers)
    assert r.status_code == 426, r.text
    text = r.json()["detail"]
    assert "protocol 3" in text and "2.1" in text and "docs/upgrade-2.1.md" in text


def test_a_stranger_is_still_told_nothing_of_the_old_protocol(monkeypatch, client):
    monkeypatch.setattr(rs, "FEDERATED_RELAYS", [OLD_URL])
    monkeypatch.setattr(fd, "enabled", lambda: True)
    headers = {"X-Emporia-Relay": "http://stranger.test", "X-Emporia-Relay-Time": str(int(time.time())),
               "X-Emporia-Relay-Signature": "ab" * 64}
    assert client.post("/federation/v1/dm", content=b"{}", headers=headers).status_code == 403


def test_no_dm_is_sent_to_a_relay_whose_sync_said_it_is_too_old(monkeypatch):
    monkeypatch.setitem(rs._FEDERATION_LAST_SYNC, OLD_URL, {
        "ok": True, "imported": 0,
        "protocol_note": "linked for posts and open sessions; it runs relay protocol 3 (Emporia 2.0.1), so shared "
                         "topics, notices, the directory and sealed DMs wait until it runs 2.1"})
    with pytest.raises(HTTPException) as e:
        fd.refuse_unless_peer_takes_dms(OLD_URL)
    assert e.value.status_code == 409
    assert "protocol 3" in e.value.detail and "2.1" in e.value.detail


def test_a_look_up_that_a_bare_401_answers_names_the_old_protocol(monkeypatch):
    import asyncio

    class R:
        status_code = 401

    async def call(peer, method, path, payload=None):
        return R()

    async def reason(peer):
        return "it runs relay protocol 3, which has no shared topics, notices or directory"
    monkeypatch.setattr(fd, "call_peer", call)
    monkeypatch.setattr(fd, "old_peer_reason", reason)
    with pytest.raises(HTTPException) as e:
        asyncio.run(fd.look_up(OLD_URL, "someone"))
    assert e.value.status_code == 502 and "protocol 3" in e.value.detail and "answered 401" not in e.value.detail


def test_a_2_1_agent_told_by_a_2_0_1_relay_is_given_the_plain_reason(monkeypatch):
    from emporia import mcp_server as m

    class Card:
        def __init__(self, v):
            self.v = v

        def json(self):
            return {"version": self.v}
    monkeypatch.setattr(m.fetch, "get", lambda *a, **k: Card(3))
    note = m._older_relay_note()
    assert "relay protocol 3" in note and "update the relay to 2.1" in note
    monkeypatch.setattr(m.fetch, "get", lambda *a, **k: Card(4))
    assert m._older_relay_note() == ""

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(m.fetch, "get", boom)
    assert m._older_relay_note() == ""


# ── a 2.0.1 relay (protocol 3) stays linked for posts and open sessions ─────────────────────────

def _v3_card(url, *, version=3):
    """A relay card as 2.0.1 serves it: version 3, signed by the relay's key, modules, no directory fields."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia.identity import relay_card_bytes
    key = ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    card = {"kind": "emporia-relay-card", "version": version, "relay_id": "oldpeer", "relay_public_key": pub,
            "relay_key_history": [], "relay_url": url,
            "relay_modules": {"federation": {"v": 1, "parts": ["posts"], "links": []}}}
    card["signature"] = key.sign(relay_card_bytes(card)).hex()
    rs._FEDERATION_PEER_PUBKEYS[url] = pub
    return card


@pytest.fixture
def stub_peer(monkeypatch):
    from emporia import fetch
    served = {}

    class Answer:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    async def request(method, url, **kw):
        return Answer(served["card"])
    monkeypatch.setattr(fetch, "request", request)
    yield served
    for table in (rs._FEDERATION_PEER_PUBKEYS, rs._FEDERATION_PEER_MODULES, rs._FEDERATION_PEER_CARDS,
                  rs._FEDERATION_LAST_SYNC):
        table.pop(OLD_URL, None)


def test_a_protocol_3_peer_is_linked_not_refused(stub_peer):
    import anyio
    stub_peer["card"] = _v3_card(OLD_URL)
    mods = anyio.run(rs._pull_peer_modules, OLD_URL)
    assert mods is not None                                     # its card is believed (signed, pinned)
    sync = rs._FEDERATION_LAST_SYNC[OLD_URL]
    assert "relay_modules_error" not in sync and "refused" not in str(sync)
    assert "protocol 3" in sync["protocol_note"] and "posts and open sessions" in sync["protocol_note"]
    assert rs.peer_runs(OLD_URL, "federation") is True


def test_a_protocol_4_peer_has_no_note(stub_peer):
    import anyio
    stub_peer["card"] = _v3_card(OLD_URL, version=4)
    anyio.run(rs._pull_peer_modules, OLD_URL)
    assert "protocol_note" not in rs._FEDERATION_LAST_SYNC[OLD_URL]


def test_a_protocol_3_peer_is_not_listed_in_the_directory(monkeypatch, stub_peer):
    import anyio

    from emporia import directory
    monkeypatch.setattr(rs, "FEDERATED_RELAYS", [OLD_URL])
    stub_peer["card"] = _v3_card(OLD_URL)
    stub_peer["card"]["listed"] = True        # (the signature no longer matches: the entry is left out either way)
    anyio.run(rs._pull_peer_modules, OLD_URL)
    rs._FEDERATION_PEER_CARDS[OLD_URL] = stub_peer["card"]
    assert all(c.get("relay_url") != OLD_URL for c in directory.entries())


def test_the_posts_of_a_protocol_3_peer_are_imported(monkeypatch):
    """The public signed feed is the same in 2.0.1 and 2.1, so a pull from a 2.0.1 relay imports as before."""
    import anyio
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import identity
    key = ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    batch = {"origin_relay": OLD_URL, "origin_public_key_hex": pub, "posts": [],
             "signature": key.sign(identity.federation_signable_bytes(OLD_URL, "posts", [])).hex()}

    async def fetch_batch(url):
        return batch
    monkeypatch.setattr(rs, "_fetch_federation_batch", fetch_batch)
    monkeypatch.setattr(rs, "save_federation_peer_pubkeys", lambda *_: None)
    rs._FEDERATION_PEER_PUBKEYS.pop(OLD_URL, None)
    try:
        assert anyio.run(rs._pull_federated_posts, OLD_URL) == 0
        assert rs._FEDERATION_LAST_SYNC[OLD_URL]["ok"] is True
    finally:
        rs._FEDERATION_PEER_PUBKEYS.pop(OLD_URL, None)
        rs._FEDERATION_LAST_SYNC.pop(OLD_URL, None)
