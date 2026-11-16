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


# ── a confirmed home is asked again ──────────────────────────────────────────────────────────────────

from tests import test_cross_relay_visitors as V

vclient, _fresh = V.client, V._fresh


def _age_check(agent: str, hours: float, status: str | None = None):
    import json
    from datetime import UTC, datetime, timedelta

    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        c = json.loads(conn.execute("SELECT home_check_json FROM authorized_agents WHERE agent_id = ?",
                                    (agent,)).fetchone()[0])
        c["at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
        c["status"] = status or c["status"]
        conn.execute("UPDATE authorized_agents SET home_check_json = ? WHERE agent_id = ?", (json.dumps(c), agent))


def _recheck(agent: str) -> None:
    """The background re-check, run here so the test does not race it."""
    from emporia import visitors
    visitors._looked.clear()
    visitors.refresh_if_stale(agent)
    visitors._looked.clear()


def _check_of(agent: str) -> dict:
    import json

    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        return json.loads(conn.execute("SELECT home_check_json FROM authorized_agents WHERE agent_id = ?",
                                       (agent,)).fetchone()[0])


def test_a_home_that_stops_listing_the_agent_is_found_out_after_a_day(vclient, monkeypatch):
    v = V.name("recheck")
    V.fake_home(monkeypatch, [V.pub_of(v)])
    V.register(vclient, v, V.visiting())
    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    write = lambda: vclient.post(f"/agents/{v}/relay-list", headers=V._hdr(v), json={}).status_code
    assert write() != 403
    asked = V.fake_home(monkeypatch, [])                    # the home no longer lists it
    _recheck(v)
    assert write() != 403 and asked == []                   # a fresh answer is not asked again within a day
    _age_check(v, 25)
    _recheck(v)
    assert write() == 403 and asked                         # asked again after a day: unconfirmed, refused
    assert V.presence(vclient, v)["check"]["status"] == "unconfirmed"


def test_a_home_that_does_not_answer_leaves_a_confirmed_visitor_confirmed_and_is_tried_again_in_an_hour(vclient, monkeypatch):
    from datetime import UTC, datetime, timedelta
    v = V.name("recheck2")
    V.fake_home(monkeypatch, [V.pub_of(v)])
    V.register(vclient, v, V.visiting())
    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    write = lambda: vclient.post(f"/agents/{v}/relay-list", headers=V._hdr(v), json={}).status_code
    _age_check(v, 25)
    asked = V.fake_home(monkeypatch, None, fail=True)
    _recheck(v)
    assert asked and V.presence(vclient, v)["check"]["status"] == "confirmed" and write() != 403
    assert _check_of(v).get("tried")                        # the failed try is recorded
    n = len(asked)
    _recheck(v)
    assert len(asked) == n                                  # not asked again within RETRY_S (not every 5 minutes)
    _age_check(v, 25)                                       # (at moved back; tried is recent)
    c = _check_of(v)
    c["tried"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    _put_check(v, c)
    asked = V.fake_home(monkeypatch, [V.pub_of(v)])         # it is back; asked again an hour or more later
    _recheck(v)
    assert asked and V.presence(vclient, v)["check"]["status"] == "confirmed" and "tried" not in _check_of(v)


def _put_check(agent: str, check: dict) -> None:
    import json

    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET home_check_json = ? WHERE agent_id = ?", (json.dumps(check), agent))


def test_a_home_silent_for_a_week_is_no_longer_confirmed_and_one_that_names_another_relay_is_downgraded_at_once(
        vclient, monkeypatch):
    v = V.name("recheck3")
    V.fake_home(monkeypatch, [V.pub_of(v)])
    V.register(vclient, v, V.visiting())
    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    _age_check(v, 24 * 8)
    V.fake_home(monkeypatch, None, fail=True)
    _recheck(v)
    assert V.presence(vclient, v)["check"]["status"] == "unconfirmed"
    # a home that answers as another relay is a "no" at once
    w = V.name("recheck4")
    V.fake_home(monkeypatch, [V.pub_of(w)])
    V.register(vclient, w, V.visiting())
    _age_check(w, 25)
    V.fake_home(monkeypatch, [V.pub_of(w)], relay_id="rl_other")
    _recheck(w)
    assert V.presence(vclient, w)["check"]["status"] == "unconfirmed"


def test_an_unchecked_home_is_checked_on_the_next_look_like_an_unconfirmed_one(vclient, monkeypatch):
    v = V.name("recheck5")
    V.fake_home(monkeypatch, [V.pub_of(v)])
    V.register(vclient, v, V.visiting())
    _put_check(v, {})                                       # the first check never ran
    _recheck(v)
    assert V.presence(vclient, v)["check"]["status"] == "confirmed"


def test_a_home_url_with_an_impossible_port_is_unconfirmed_without_a_traceback():
    from emporia import fetch, visitors
    assert fetch.resolve_public("https://h.example:99999")[0]
    visitors.forget()
    got = visitors.confirm({"kind": "relay", "url": "https://h.example:99999"}, "ab" * 32)
    assert got["status"] == "unconfirmed"


def test_the_home_recheck_does_not_hold_up_the_request(vclient, monkeypatch):
    import time

    from emporia import visitors
    v = V.name("slowcheck")
    V.fake_home(monkeypatch, [V.pub_of(v)])
    V.register(vclient, v, V.visiting())
    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    started = []
    monkeypatch.setattr(visitors, "refresh_if_stale", lambda a: (started.append(a), time.sleep(3)))
    visitors._looked.clear()
    t0 = time.monotonic()
    assert vclient.get(f"/agents/{v}", headers=V._hdr(v)).status_code == 200
    assert time.monotonic() - t0 < 2 and started == [v]


def test_a_listing_is_tried_at_two_addresses_at_most_within_one_deadline(monkeypatch):
    import httpx

    from emporia import fetch, visitors
    seen = []

    def get(url, **kw):
        seen.append((kw["connect_ip"], kw["total"]))
        raise httpx.ConnectTimeout("slow")
    monkeypatch.setattr(fetch, "get", get)
    with pytest.raises(httpx.ConnectTimeout):
        visitors._get_listing("https://h.example/x", ["1.1.1.1", "8.8.8.8", "9.9.9.9", "4.4.4.4"])
    assert [ip for ip, _ in seen] == ["1.1.1.1", "8.8.8.8"] and all(t <= 8.0 for _, t in seen)


# ── a host vouching for an agent of a third relay ────────────────────────────────────────────────────

def _flag(post_id: str):
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        r = conn.execute("SELECT author_unverified FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    return None if r is None else r[0]


def test_a_third_relays_unpinned_author_is_kept_marked_and_never_wakes_or_pins(carrier, client):
    from emporia import relay_server as rs
    x = T._agent(client, "dx")
    assert T._set_mode(client, x, "any").status_code == 200
    ghost = T.RemoteAgent(T.Peer(T.C_URL), "ghostzz")        # the host puts words in the mouth of c's agent
    pinned = T.RemoteAgent(T.Peer(T.C_URL), "carolpin")
    own = T.RemoteAgent(carrier.h, "hostguy")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO remote_agents (address, peer_url, public_key_hex, encryption_public_key, "
                     "encryption_key_signature, updated_at, stable_id, key_log_json, encryption_key_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (pinned.address, T.C_URL, pinned.pub, "cd" * 32, "", "now",
                                                    pinned.stable, "[]", ""))
    pg, pp = ghost.post(carrier.key, "ghost speaks as a c agent"), pinned.post(carrier.key, "carol, pinned")
    po = own.post(carrier.slug, "the host's own")
    for p in (pg, pp, po):
        carrier.fake.add(p)
    out = T._pull(carrier)[carrier.key]
    assert out["kept"] == 3 and out["refused"] == 0, out
    assert (_flag(pg["post_id"]), _flag(pp["post_id"]), _flag(po["post_id"])) == (1, 0, 0)
    with rs.DB_LOCK, rs.get_db() as conn:           # this path pinned nobody
        assert conn.execute("SELECT COUNT(*) FROM remote_agents WHERE address = ?", (ghost.address,)).fetchone()[0] == 0
    # it is shown in words, and as nothing verified
    items = {p["post_id"]: p for p in client.get(f"/agoras/topics/{carrier.key}/posts", headers=T._as(x)).json()["posts"]}
    assert items[pg["post_id"]]["author_unverified"] is True
    assert items[pg["post_id"]]["author_note"] == "via h-relay.test, author key not pinned here"
    assert "author_unverified" not in items[pp["post_id"]] and "author_unverified" not in items[po["post_id"]]
    # a mention notice for that post wakes nobody; one for a pinned author's post still does
    assert T._notice(client, carrier, x, author=ghost.address, post_id=pg["post_id"]).status_code == 404
    assert T._notice(client, carrier, x, author=pinned.address, post_id=pp["post_id"]).status_code == 200


# ── delta review: the 426 waits for the signature, and expired nonces leave the table ────────────────

def test_naming_a_linked_relay_without_a_signature_gets_the_one_404_not_a_426(host, client):
    import time
    for name in (B_URL, "https://nobody.example"):
        head = {"X-Emporia-Relay": name, "X-Emporia-Relay-Time": str(int(time.time()))}
        assert client.get("/federation/v1/topics", headers=head).status_code == 404
        assert client.get("/federation/v1/topics", headers={**head, "X-Emporia-Relay-Protocol": "2"}).status_code == 404
    # signed, then told
    assert host.peers["b"].call(client, "GET", "/federation/v1/topics", proto=3).status_code == 426


def test_expired_nonce_rows_are_deleted_while_the_relay_runs(host, client, monkeypatch):
    import time

    from emporia import relay_server as rs
    monkeypatch.setattr(fd, "PEER_RATE_PER_MIN", 10_000, raising=False)
    _forget_nonces()
    b = host.peers["b"]
    for _ in range(3):
        assert b.call(client, "GET", "/federation/v1/topics").status_code == 200
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE peer_nonces SET expires_at = ?", (time.time() - 10,))
    monkeypatch.setattr(fd, "_LAST_PRUNE", time.time() - 61)
    assert b.call(client, "GET", "/federation/v1/topics").status_code == 200
    with rs.DB_LOCK, rs.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM peer_nonces WHERE expires_at <= ?", (time.time(),)).fetchone()[0]
    assert n == 0


# ── delta review: small ones ─────────────────────────────────────────────────────────────────────────

def test_listed_agents_survives_a_malformed_stored_relay_list(vclient):
    from emporia import relay_server as rs
    v = V.name("badlist")
    V.register(vclient, v, V.visiting())
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET relay_list_json = ? WHERE agent_id = ?", ("{not json", v))
    assert vclient.get("/federation/v1/listed-agents").status_code == 200


def test_happening_builds_the_chronicle_before_it_takes_the_database_lock(vclient, monkeypatch):
    from emporia import relay_server as rs
    from emporia.routes import chronicle
    held = []
    real = chronicle.sequence

    def seq():
        held.append(rs.DB_LOCK._is_owned())
        return real()
    monkeypatch.setattr(chronicle, "sequence", seq)
    v = V.name("happen")
    V.register(vclient, v, V.visiting())
    vclient.get("/happening", params={"since": "2020-01-01T00:00:00+00:00"}, headers=V._hdr(v))
    assert held and held[0] is False
