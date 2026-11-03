"""P281: the transparency log checked from outside — by an agent (step 2) and by
a linked relay (step 3). A relay that rewrites its log, shrinks it, or shows two
readers two histories is caught with two states it signed; a new log is
accepted only with its signed hand-over; the key changes a client followed must
be in the log. And the log is now off unless the operator turns it on."""
import pytest
from fastapi.testclient import TestClient

from emporia import identity, logcheck, translog
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _rotate_keyed


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51481))


@pytest.fixture(autouse=True)
def kept(tmp_path, monkeypatch):
    """Each test keeps its states in its own folder, not the suite's keys."""
    monkeypatch.setattr(logcheck, "_path", lambda name: tmp_path / name)
    return tmp_path


def _get(client):
    def get(path, params):
        r = client.get(path, params=params or None)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return get


def _grow(client, name):
    _register_keyed(client, name)
    _rotate_keyed(client, name)
    assert rs.sign_log_state() is not None


def _forged(state, **change):
    """A state this relay signed, with other contents — what a relay that keeps
    two histories would sign."""
    s = {k: v for k, v in state.items() if k != "signature"} | change
    return s | {"signature": identity.sign_raw_relay(translog.state_bytes(s))}


def _serving(client, **override):
    """The suite relay, with some answers replaced."""
    real = _get(client)

    def get(path, params):
        if path in override:
            return override[path](params) if callable(override[path]) else (200, override[path])
        return real(path, params)
    return get


TRUSTED = [identity.get_relay_public_key_hex()]


def test_the_log_is_off_unless_the_operator_turns_it_on(monkeypatch):
    monkeypatch.delenv("EMPORIA_TRANSPARENCY_LOG", raising=False)
    assert translog.enabled() is False
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert translog.enabled() is True


def test_a_log_that_only_grew_checks_and_the_newer_state_is_kept(client):
    _grow(client, "lc-grow")
    first = logcheck.check_and_keep("r", _get(client), TRUSTED)
    assert first["status"] == "first" and first["ok"]
    assert logcheck.check_and_keep("r", _get(client), TRUSTED)["status"] == "ok"     # nothing new
    _rotate_keyed(client, "lc-grow")
    rs.sign_log_state()
    grew = logcheck.check_and_keep("r", _get(client), TRUSTED)
    assert grew["ok"] and "kept what it had" in grew["detail"]
    assert logcheck.load()["relays"]["r"]["state"]["size"] > first["state"]["size"]


def test_two_histories_a_rewrite_and_a_shrunk_log_are_caught_with_evidence(client):
    _grow(client, "lc-forge")
    held = logcheck.check_and_keep("r", _get(client), TRUSTED)["state"]
    # the same size, another root: both signed, so the pair proves it
    twin = _forged(held, root="ab" * 32)
    out = logcheck.check_and_keep("r", _serving(client, **{"/log/state": twin}), TRUSTED)
    assert out["status"] == "fail" and "two histories" in out["detail"] and out["failures"] == 1
    kept = logcheck.load()["relays"]["r"]
    assert kept["state"] == held                                   # what was proved stays kept
    assert kept["failures"][0]["evidence"]["states"] == [held, twin]
    # a larger log whose proof does not fit what it showed before
    bigger = _forged(held, size=held["size"] + 1, root="cd" * 32)
    proof = lambda p: (200, {"proof": ["00" * 32]})                # noqa: E731
    out = logcheck.check(_serving(client, **{"/log/state": bigger, "/log/proof/consistency": proof}), TRUSTED, held)
    assert out["status"] == "fail" and "rewrote its log" in out["detail"]
    # ...or will not prove it at all
    out = logcheck.check(_serving(client, **{"/log/state": bigger, "/log/proof/consistency": lambda p: (404, {})}),
                         TRUSTED, held)
    assert out["status"] == "fail" and "will not prove" in out["detail"]
    shrunk = _forged(held, size=held["size"] - 1)
    assert "shrank" in logcheck.check(_serving(client, **{"/log/state": shrunk}), TRUSTED, held)["detail"]
    # a state not signed by the relay's key is not even a state
    stranger = held | {"relay_public_key": "11" * 32}
    assert logcheck.check(_serving(client, **{"/log/state": stranger}), TRUSTED, held)["status"] == "fail"


def test_a_relay_keeping_no_log_says_so_and_that_is_not_a_failure(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    out = logcheck.check(_get(client), TRUSTED, None)
    assert out["status"] == "off" and out["ok"]


def test_a_new_log_needs_the_relays_signed_hand_over(client):
    _grow(client, "lc-epoch")
    held = logcheck.check(_get(client), TRUSTED, None)["state"]
    fresh = _forged(held, epoch=held.get("epoch", 1) + 1, size=3)
    handover = {"type": translog.HANDOVER_TYPE, "epoch": fresh["epoch"], "reason": "rebuilt",
                "previous": {k: held.get(k) for k in ("epoch", "size", "root", "at", "relay_public_key")},
                "first": {k: fresh[k] for k in ("size", "root", "at")}, "relay_public_key": TRUSTED[0],
                "at": fresh["at"]}
    handover["signature"] = identity.sign_raw_relay(translog.handover_bytes(handover))
    epochs = {"epochs": [{"epoch": fresh["epoch"], "handover": handover}]}
    out = logcheck.check(_serving(client, **{"/log/state": fresh, "/log/epochs": epochs}), TRUSTED, held)
    assert out["status"] == "new_log" and out["ok"] and "rebuilt" in out["detail"]
    unsigned = {"epochs": [{"epoch": fresh["epoch"], "handover": {**handover, "signature": "00" * 64}}]}
    out = logcheck.check(_serving(client, **{"/log/state": fresh, "/log/epochs": unsigned}), TRUSTED, held)
    assert out["status"] == "fail"
    older = _forged(held, epoch=0)
    assert logcheck.check(_serving(client, **{"/log/state": older}), TRUSTED, held)["status"] == "fail"


def test_the_key_changes_a_client_followed_must_be_in_the_log(client):
    _register_keyed(client, "lc-keys")
    _rotate_keyed(client, "lc-keys")
    card = client.get("/agents/lc-keys", headers={"x-emporia-agent-id": "lc-keys"}).json()
    rs.sign_log_state()
    state = logcheck.check(_get(client), TRUSTED, None)["state"]
    pin_id = identity.signing_pin_id("lc-keys", "http://testserver")
    pins = {pin_id: {"public_key_hex": card["public_key_hex"], "stable_id": card["stable_id"],
                     "key_log": card["key_log"]}}
    assert logcheck.key_steps_logged("http://testserver", _get(client), state, pins) == \
        {"logged": 1, "pending": 0, "missing": []}
    # a key step the relay showed this client, but never logged
    shown = [{**card["key_log"][0], "signature": "ee" * 64}]
    pins[pin_id]["key_log"] = shown
    assert logcheck.key_steps_logged("http://testserver", _get(client), state, pins)["missing"] == ["lc-keys"]


def test_a_linked_relay_keeps_its_peers_states_and_the_evidence(client, monkeypatch):
    _grow(client, "lc-peer")
    peer = "http://peer.test"
    monkeypatch.setitem(rs._FEDERATION_PEER_PUBKEYS, peer, TRUSTED[0])
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _get(client))
    assert rs._check_peer_log(peer)["status"] == "first"
    seen = client.get("/log/seen").json()["relays"][peer]
    assert seen["status"] == "first" and logcheck.state_signed(seen["state"], TRUSTED)
    twin = _forged(seen["state"], root="ab" * 32)
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _serving(client, **{"/log/state": twin}))
    assert rs._check_peer_log(peer)["status"] == "fail"
    seen = client.get("/log/seen").json()["relays"][peer]
    assert seen["state"]["root"] != twin["root"] and seen["failures"][0]["evidence"]["states"][1] == twin
    assert rs._check_peer_log("http://unpinned.test") is None          # only a peer whose key it pinned


def test_a_client_compares_its_state_with_what_another_relay_was_shown(client, monkeypatch):
    _grow(client, "lc-cross")
    mine = logcheck.check(_get(client), TRUSTED, None)["state"]
    same = {"relays": {"http://home.test": {"state": mine}}}
    assert logcheck.cross_check(_get(client), TRUSTED, mine, same)["status"] == "ok"
    other = {"relays": {"http://home.test": {"state": _forged(mine, root="ab" * 32)}}}
    out = logcheck.cross_check(_get(client), TRUSTED, mine, other)
    assert out["status"] == "fail" and out["state"] == mine
    # the other relay's own word counts for nothing: only this relay's signature does
    lie = {"relays": {"http://home.test": {"state": mine | {"root": "ab" * 32}}}}
    assert logcheck.cross_check(_get(client), TRUSTED, mine, lie)["status"] == "unchecked"


def test_verify_relay_puts_it_together(client, monkeypatch):
    _grow(client, "lc-all")
    monkeypatch.setattr(identity, "load_peer_signing_keys", lambda: {})
    home, elsewhere = "http://testserver", "http://elsewhere.test"
    monkeypatch.setattr(identity, "KEY_DIR", logcheck._path("x").parent)      # its own relay pins
    seen_ok = {"relays": {home: {"state": None}}}

    def get_for(url):
        if url == elsewhere:
            return lambda path, params: (200, seen_ok)
        return _get(client)
    out = logcheck.verify_relay(home, get_for, compare_with=[elsewhere])
    assert out["ok"] and out["status"] == "first" and out["compared_with"][0]["status"] == "unchecked"
    seen_bad = {"relays": {home: {"state": _forged(out["state"], root="ab" * 32)}}}
    out = logcheck.verify_relay(home, lambda url: (lambda p, q: (200, seen_bad)) if url == elsewhere
                                else _get(client), compare_with=[elsewhere])
    assert not out["ok"] and "another history" in out["problems"][0]
    # a relay the operator barred is never asked
    out = logcheck.verify_relay(home, get_for, compare_with=[elsewhere], may_ask=lambda url: url != elsewhere)
    assert out["compared_with"] == []


def test_a_relay_that_kept_a_log_says_loudly_when_it_is_left_off(client, monkeypatch):
    _grow(client, "lc-left")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    assert "EMPORIA_TRANSPARENCY_LOG=on" in rs._warn_if_log_left_off()
