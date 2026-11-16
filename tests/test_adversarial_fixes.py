"""Regression tests for the adversarial check of 2.1: each is an attack that worked, now refused."""
from __future__ import annotations

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
