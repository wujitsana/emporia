"""Regression tests for the adversarial check of 2.1: each is an attack that worked, now refused."""
from __future__ import annotations

from emporia import federation_dm as fd
from tests import test_shared_topics as T

B_URL = T.B_URL
client, host, carrier = T.client, T.host, T.carrier       # the shared-topics fixtures, reused


def test_one_peer_filling_its_nonce_cache_locks_out_only_itself(host, client, monkeypatch):
    """Was: one global cache of 20000, so one linked relay's flood refused every peer."""
    for name in ("_MAX_SEEN", "_MAX_SEEN_PER_PEER"):
        monkeypatch.setattr(fd, name, 5, raising=False)
    monkeypatch.setattr(fd, "PEER_RATE_PER_MIN", 10_000, raising=False)
    fd._SEEN_NONCES.clear()
    b, c = host.peers["b"], host.peers["c"]
    codes = [b.call(client, "GET", "/federation/v1/topics").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) <= {404, 429}      # B is refused once its own cache is full
    assert c.call(client, "GET", "/federation/v1/topics").status_code == 200       # C is not
    assert b.call(client, "GET", "/federation/v1/topics").status_code != 200


def test_a_peer_over_its_rate_is_refused_before_its_nonce_is_stored(host, client, monkeypatch):
    monkeypatch.setattr(fd, "PEER_RATE_PER_MIN", 3, raising=False)
    fd._SEEN_NONCES.clear()
    getattr(fd, "_PEER_HITS", {}).clear()
    b, c = host.peers["b"], host.peers["c"]
    codes = [b.call(client, "GET", "/federation/v1/topics").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert len(fd._SEEN_NONCES.get(B_URL, {})) == 3          # the refused calls stored no nonce
    assert c.call(client, "GET", "/federation/v1/topics").status_code == 200
