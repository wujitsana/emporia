"""Dealt Blotto: the battlefields' weights come from a drand round the creator named at creation
(`emporia:rand:v1`), public before anyone commits."""
import uuid

import pytest
from conftest import fake_beacon_sig
from fastapi.testclient import TestClient

from emporia import calls, rand
from emporia import relay_server as rs
from emporia.modules import blotto as BL
from emporia.relay_server import app
from emporia.routes import beacon as beacon_route
from tests.test_emporia import _register_keyed, _signed_action_body

BLOTTO = "emporia:blotto:v2"
WHY = "Putting everything on the heaviest field"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51233))


def _hdr(a):
    return {"x-emporia-agent-id": a}


def _round(offset=-5):
    import time
    return rand.round_at(time.time()) + offset


def _game(client, config=None):
    a, b = f"da_{uuid.uuid4().hex[:8]}", f"db_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    cfg = {"dealt": True, "weights_round": _round(), **(config or {})}
    r = client.post("/sessions", json={"module_type": BLOTTO, "config": cfg}, headers=_hdr(a))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b)).status_code == 200
    return sid, a, b


def _act(client, sid, who, action, payload):
    return client.post(f"/sessions/{sid}/action", json=_signed_action_body(client, sid, who, action, payload, WHY))


def test_the_weights_are_dealt_from_the_named_round_and_shown_before_any_commit(client):
    sid, a, b = _game(client)
    cfg = rs.load_session(sid)["config"]
    expected = BL.weights_from(sid, 5, bytes.fromhex(fake_beacon_sig(cfg["weights_round"])))
    assert cfg["rand"] == {"scope": sid} and len(expected) == 5 and all(1 <= w <= 5 for w in expected)
    seen = client.get(f"/sessions/{sid}", headers=_hdr(a)).json()["state"]["data"]
    assert seen["weights"] == expected and seen["weights_round"] == cfg["weights_round"], "public, before commits"
    assert seen["rand"]["beacon_sig"] is None, "a read stores nothing"
    # the weights differ between games and between rounds: no canned board
    other = BL.weights_from(sid, 5, bytes.fromhex(fake_beacon_sig(cfg["weights_round"] + 1)))
    assert expected != other or BL.weights_from("sess_z", 5, bytes(48)) != expected


def test_a_battlefield_scores_its_weight(client):
    sid, a, b = _game(client)
    w = BL.weights_from(sid, 5, bytes.fromhex(fake_beacon_sig(rs.load_session(sid)["config"]["weights_round"])))
    heavy, light = max(range(5), key=w.__getitem__), min(range(5), key=w.__getitem__)
    ma, mb = [0] * 5, [0] * 5
    ma[heavy], mb[light] = 100, 100
    nonce = "ab" * 32
    for who, mv in ((a, ma), (b, mb)):
        assert _act(client, sid, who, "commit", {"commitment": calls.commitment(mv, None, nonce)}).status_code == 200
    for who, mv in ((a, ma), (b, mb)):
        assert _act(client, sid, who, "reveal", {"move": mv, "nonce": nonce}).status_code == 200
    s = rs.load_session(sid)
    [h] = s["state"]["data"]["history"]
    assert s["state"]["data"]["weights"] == w and h["weights"] == w
    assert h["battlefields_won"] == {a: w[heavy], b: w[light]}
    with rs.DB_LOCK, rs.get_db() as conn:     # the round used and the result are in the transparency log's tables
        row = conn.execute("SELECT * FROM rand_results WHERE session_id = ?", (sid,)).fetchone()
    assert row["beacon_round"] == rs.load_session(sid)["config"]["weights_round"] and row["termination"] == "rounds"
    assert h["winner"] == (a if w[heavy] > w[light] else None if w[heavy] == w[light] else b)


def test_dealt_needs_a_round_named_ten_ahead_and_the_round_must_be_published_to_commit(client, monkeypatch):
    a = f"dc_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": BLOTTO, "config": {"dealt": True}}, headers=_hdr(a))
    assert r.status_code == 400 and "weights_round" in r.text
    monkeypatch.setattr(rand, "MIN_LEAD_ROUNDS", 10)
    r = client.post("/sessions", json={"module_type": BLOTTO, "config": {"dealt": True, "weights_round": _round(3)}},
                    headers=_hdr(a))
    assert r.status_code == 400 and "at least 10 rounds" in r.text
    r = client.post("/sessions", json={"module_type": BLOTTO, "config": {"weights_round": _round(30)}}, headers=_hdr(a))
    assert r.status_code == 400 and "dealt game" in r.text
    r = client.post("/sessions", json={"module_type": BLOTTO, "config": {"rand": {"scope": "x"}}}, headers=_hdr(a))
    assert r.status_code == 400 and "config.rand" in r.text
    # the round is named, not yet published: no commit
    monkeypatch.setattr(rand, "MIN_LEAD_ROUNDS", -10**6)
    sid, a, b = _game(client, {"weights_round": _round(1000)})
    monkeypatch.setattr(beacon_route, "signature", lambda rnd: None)
    r = _act(client, sid, a, "commit", {"commitment": calls.commitment([20] * 5, None, "cd" * 32)})
    assert r.status_code == 400 and "not dealt yet" in r.text


def test_an_ordinary_blotto_game_is_unchanged(client):
    a, b = f"dd_{uuid.uuid4().hex[:6]}", f"de_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    sid = client.post("/sessions", json={"module_type": BLOTTO}, headers=_hdr(a)).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b))
    d = client.get(f"/sessions/{sid}", headers=_hdr(a)).json()["state"]["data"]
    assert "weights" not in d and "dealt" not in d
    assert BL._score([1, 0], [0, 1]) == (1, 1) and BL._score([5, 0], [0, 5], [3, 1]) == (3, 1)


def test_a_v2_moves_message_reaches_the_opponent_and_the_games_record(client):
    """Table talk is unchanged in the v2 games: an optional plain-text `say` rides with each move (an offer, a
    commit), is signed with it, is seen by the opponent at once (a commit's too: bluffing is the player's choice)
    and stays in the game's chat and the public record. The tools mark it untrusted."""
    # Bargain v2: an offer with a message
    a, b = f"ta_{uuid.uuid4().hex[:6]}", f"tb_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    sid = client.post("/sessions", json={"module_type": "emporia:bargain:v2"}, headers=_hdr(a)).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b))
    talk = "I care about silk, honest."
    body = _signed_action_body(client, sid, a, "offer", {"keep": {"silk": 2}}, WHY, say=talk)
    assert body["say"] == talk
    r = client.post(f"/sessions/{sid}/action", json=body)
    assert r.status_code == 200, r.text
    chat = client.get(f"/sessions/{sid}/chat", headers=_hdr(b)).json()["messages"]
    assert [(m["agent_id"], m["content"]) for m in chat] == [(a, talk)], "the opponent reads it"
    record = client.get(f"/sessions/{sid}/audit", headers=_hdr(b)).json()["chain"]
    assert any(e.get("say") == talk for e in record), "signed with the move and in the public record"
    r = client.post(f"/sessions/{sid}/action", json=_signed_action_body(client, sid, b, "accept", {}, WHY, say=talk))
    assert r.status_code == 200

    # dealt Blotto: a commit with a message, visible before the reveal
    sid2, c, d = _game(client, {"chat": True})
    bluff = "I will go heavy on the big field."
    body = _signed_action_body(client, sid2, c, "commit",
                               {"commitment": calls.commitment([20] * 5, None, "ef" * 32)}, WHY, say=bluff)
    assert client.post(f"/sessions/{sid2}/action", json=body).status_code == 200
    chat = client.get(f"/sessions/{sid2}/chat", headers=_hdr(d)).json()["messages"]
    assert [m["content"] for m in chat] == [bluff], "a message with a commit is not held back with the move"
