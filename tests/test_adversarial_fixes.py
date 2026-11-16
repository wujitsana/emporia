"""Regression tests for the adversarial check of 2.1: each is an attack that worked, now refused."""
from __future__ import annotations

import pytest

from emporia import federation_dm as fd
from tests import test_shared_topics as T

B_URL = T.B_URL
client, host, carrier = T.client, T.host, T.carrier       # the shared-topics fixtures, reused


def _forget_nonces():
    from emporia import relay_server as rs
    fd._SEEN_NONCES.clear()
    getattr(fd, "_PEER_HITS", {}).clear()
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM peer_nonces")


def test_one_peer_filling_its_nonce_cache_locks_out_only_itself(host, client, monkeypatch):
    """Was: one global cache of 20000, so one linked relay's flood refused every peer."""
    for name in ("_MAX_SEEN", "_MAX_SEEN_PER_PEER"):
        monkeypatch.setattr(fd, name, 5, raising=False)
    monkeypatch.setattr(fd, "PEER_RATE_PER_MIN", 10_000, raising=False)
    _forget_nonces()
    b, c = host.peers["b"], host.peers["c"]
    codes = [b.call(client, "GET", "/federation/v1/topics").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) <= {404, 429}      # B is refused once its own cache is full
    assert c.call(client, "GET", "/federation/v1/topics").status_code == 200       # C is not
    assert b.call(client, "GET", "/federation/v1/topics").status_code != 200


def test_a_peer_over_its_rate_is_refused_before_its_nonce_is_stored(host, client, monkeypatch):
    monkeypatch.setattr(fd, "PEER_RATE_PER_MIN", 3, raising=False)
    _forget_nonces()
    b, c = host.peers["b"], host.peers["c"]
    codes = [b.call(client, "GET", "/federation/v1/topics").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert len(fd._SEEN_NONCES.get(B_URL, {})) == 3          # the refused calls stored no nonce
    assert c.call(client, "GET", "/federation/v1/topics").status_code == 200


def test_a_captured_peer_request_is_not_valid_again_after_a_restart(host, client):
    """Was: the nonce cache was memory only, so a restart made a captured in-window request valid again."""
    b = host.peers["b"]
    h = b.headers("GET", "/federation/v1/topics")
    assert client.get("/federation/v1/topics", headers=h).status_code == 200
    assert client.get("/federation/v1/topics", headers=h).status_code != 200
    fd._SEEN_NONCES.clear()                 # the process restarts: memory is empty
    assert client.get("/federation/v1/topics", headers=h).status_code != 200


def test_nonces_past_their_window_are_dropped_from_the_table():
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO peer_nonces (peer, nonce, expires_at) VALUES ('http://old.test', 'aa', 1)")
    fd._SEEN_NONCES.pop("http://old.test", None)
    assert fd._first_use("http://old.test", "aa") == "new"          # expired: not remembered
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT expires_at FROM peer_nonces WHERE peer = 'http://old.test'").fetchone()[0] > 1000


# ── the home check: one resolution, the exact address, and one address rule ──────────────────────────

def test_a_name_that_answers_differently_the_second_time_reaches_nothing(monkeypatch):
    """DNS rebinding: the name was public when checked and loopback when connected."""
    import socket

    from emporia import fetch, visitors
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)
    real_gai = socket.getaddrinfo
    lookups: list[int] = []
    dialed: list[tuple] = []

    def gai(host, port, *a, **k):
        if host == "rebind.test":
            lookups.append(1)
            ip = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return real_gai(host, port, *a, **k)

    def cc(address, *a, **k):
        dialed.append(address)
        raise ConnectionRefusedError("test: nothing is dialed for real")

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket, "create_connection", cc)
    assert visitors._pull("https://rebind.test:8443") == {"error": visitors._UNANSWERED}
    assert len(lookups) == 1                                        # resolved once
    assert dialed and all(a[0] == "93.184.216.34" for a in dialed), dialed       # only the checked address


def test_the_pinned_call_keeps_the_name_as_host_and_tls_server_name(monkeypatch):
    import httpx

    from emporia import fetch
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)
    seen = {}

    def stream(self, method, url, **kw):
        seen.update(url=str(url), headers=kw.get("headers"), ext=kw.get("extensions"))
        raise httpx.ConnectError("test")

    monkeypatch.setattr(httpx.Client, "stream", stream)
    with pytest.raises(httpx.ConnectError):
        fetch.get("https://relay.example:8443/federation/v1/listed-agents", connect_ip="93.184.216.34")
    assert seen["url"] == "https://93.184.216.34:8443/federation/v1/listed-agents"
    assert seen["headers"]["Host"] == "relay.example:8443" and seen["ext"] == {"sni_hostname": "relay.example"}
    with pytest.raises(httpx.ConnectError):
        fetch.get("https://relay.example/x", connect_ip="2606:4700::1111")
    assert seen["url"] == "https://[2606:4700::1111]/x" and seen["headers"]["Host"] == "relay.example"


@pytest.mark.parametrize("raw", [
    "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::a00:1", "::7f00:1", "::1", "::",
    "::ffff:127.0.0.1", "::ffff:0:7f00:1", "fec0::1", "2002:7f00:1::1", "2002:a00:1::1",
    "2002:c0a8:101::1", "fe80::1", "fd00::1", "ff02::1", "127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1"])
def test_an_address_that_is_not_public_or_carries_one_that_is_not_is_refused(raw):
    from emporia import fetch
    assert not fetch.is_public_address(raw), raw


@pytest.mark.parametrize("raw", ["8.8.8.8", "93.184.216.34", "2606:4700::1111", "64:ff9b::808:808", "2002:808:808::1"])
def test_public_addresses_still_pass(raw):
    from emporia import fetch
    assert fetch.is_public_address(raw), raw


def test_the_home_check_refuses_those_as_hosts(monkeypatch):
    from emporia import fetch, visitors
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)
    for u in ("https://[64:ff9b::7f00:1]", "https://[::7f00:1]", "https://[::ffff:0:7f00:1]", "https://[fec0::1]",
              "https://[2002:a00:1::1]"):
        assert visitors.target_problem(u) == "not a public address", u
