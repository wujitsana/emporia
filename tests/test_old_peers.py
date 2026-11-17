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
        "relay_modules_error": "refused: it runs relay protocol 3, which has no shared topics, notices or "
                               "directory; this relay speaks protocol 4. Upgrade it to sync"})
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
