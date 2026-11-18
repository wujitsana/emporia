"""2.1: linked relays witness each other's transparency logs. At each sync a relay checks the peer's signed
state against the one it kept (a consistency proof), keeps the newer, and looks at what the peer was shown of
this relay against its own log. A mismatch is recorded where the operator and /log/seen show it; a peer that
is down, runs protocol 3 or holds nothing is not a problem."""
import pytest

from emporia import logcheck
from emporia import relay_server as rs
from tests.test_logcheck import TRUSTED, _forged, _get, _grow, _serving, client, kept  # noqa: F401

PEER = "http://peer.test"


@pytest.fixture
def linked(client, monkeypatch):  # noqa: F811
    monkeypatch.setitem(rs._FEDERATION_PEER_PUBKEYS, PEER, TRUSTED[0])
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _get(client))
    return client


def _down(path, params):
    raise ConnectionError("peer is down")


def test_a_peer_whose_log_only_grew_is_kept_at_its_newer_state(linked):
    _grow(linked, "pw-grow-a")
    first = rs._check_peer_log(PEER)
    assert first["status"] == "first"
    _grow(linked, "pw-grow-b")                      # the peer's log grows; the consistency proof must hold
    second = rs._check_peer_log(PEER)
    assert second["status"] == "ok"
    kept_state = logcheck.load(logcheck.PEER_FILE)["relays"][PEER]["state"]
    assert kept_state["size"] > first["state"]["size"] and kept_state["size"] == second["state"]["size"]
    assert logcheck.peer_problems() == []


def test_a_peer_that_rewrote_its_log_is_recorded_as_a_problem(linked, monkeypatch):
    _grow(linked, "pw-rewrite")
    rs._check_peer_log(PEER)
    held = logcheck.load(logcheck.PEER_FILE)["relays"][PEER]["state"]
    twin = _forged(held, root="ab" * 32)
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _serving(linked, **{"/log/state": twin}))
    assert rs._check_peer_log(PEER)["status"] == "fail"
    assert logcheck.load(logcheck.PEER_FILE)["relays"][PEER]["state"] == held        # the proved state stays
    served = linked.get("/log/seen").json()["relays"][PEER]
    assert served["failures"]
    assert any(PEER in p for p in logcheck.peer_problems())


def test_a_peer_that_is_down_is_not_a_problem(linked, monkeypatch):
    _grow(linked, "pw-down")
    rs._check_peer_log(PEER)
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _down)
    out = rs._check_peer_log(PEER)
    assert out["status"] == "unreachable"
    assert logcheck.peer_problems() == []
    assert not logcheck.load(logcheck.PEER_FILE)["relays"][PEER]["failures"]


def _holding(state):
    """A peer's /log/seen that says it holds `state` of this relay."""
    return lambda params: (200, {"relays": {rs.RELAY_BASE_URL: {"state": state}}})


def test_what_a_peer_was_shown_of_this_relay_is_checked_against_its_own_log(linked, monkeypatch):
    _grow(linked, "pw-shown")
    mine = linked.get("/log/state").json()
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _serving(linked, **{"/log/seen": _holding(mine)}))
    assert rs._check_shown_to_peer(PEER)["status"] == "ok"
    assert logcheck.load(logcheck.PEER_FILE)["shown"][PEER]["status"] == "ok"
    assert linked.get("/log/seen").json()["shown"][PEER]["failures"] == 0
    # a state this relay signed with another root: it showed that peer a different history
    twin = _forged(mine, root="ab" * 32)
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _serving(linked, **{"/log/seen": _holding(twin)}))
    assert rs._check_shown_to_peer(PEER)["status"] == "fail"
    shown = linked.get("/log/seen").json()["shown"][PEER]
    assert shown["status"] == "fail" and shown["failures"] == 1 and "evidence" not in shown
    assert logcheck.load(logcheck.PEER_FILE)["shown"][PEER]["failures"][0]["evidence"]["states"] == [twin]
    assert any("holds a state of this relay's log" in p for p in logcheck.peer_problems())


def test_a_state_this_relay_never_signed_is_not_evidence(linked, monkeypatch):
    _grow(linked, "pw-forgery")
    mine = linked.get("/log/state").json()
    lie = {**mine, "root": "ab" * 32}                # the peer's own word, not signed by this relay
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _serving(linked, **{"/log/seen": _holding(lie)}))
    assert rs._check_shown_to_peer(PEER)["status"] == "unchecked"
    assert "shown" not in logcheck.load(logcheck.PEER_FILE)


@pytest.mark.parametrize("answer", [lambda p: (404, None), lambda p: (200, {"relays": {}}), _down,
                                    lambda p: (200, ["not", "a", "dict"])])
def test_a_peer_that_is_down_old_or_holds_nothing_leaves_no_record(linked, monkeypatch, answer):
    _grow(linked, "pw-quiet")
    monkeypatch.setattr(rs, "_peer_log_get", lambda url: _serving(linked, **{"/log/seen": answer}))
    assert rs._check_shown_to_peer(PEER) is None or rs._check_shown_to_peer(PEER)["status"] == "unchecked"
    assert "shown" not in logcheck.load(logcheck.PEER_FILE)
    assert logcheck.peer_problems() == []


def test_the_check_runs_with_the_sync_and_never_raises(linked, monkeypatch):
    _grow(linked, "pw-sync")
    monkeypatch.setattr(rs, "_check_shown_to_peer", lambda url: 1 / 0)
    assert rs._check_peer_log(PEER)["status"] == "first"
