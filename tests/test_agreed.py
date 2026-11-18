"""`agreed`: what a game's creator submitted and the relay accepted, kept beside the result."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import agreed, calls, commit_reveal
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body
from tests.test_results import _blotto

BLOTTO = "emporia:blotto:v2"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51219))


def _finished_with(client, cfg):
    a, b = f"ra_{uuid.uuid4().hex[:8]}", f"rb_{uuid.uuid4().hex[:8]}"
    for who in (a, b):
        _register_keyed(client, who)
    r = client.post("/sessions", json={"module_type": BLOTTO, "creator_agent_id": a, "config": cfg},
                    headers={"x-emporia-agent-id": a})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b})
    nonces = {a: commit_reveal.new_nonce(), b: commit_reveal.new_nonce()}
    moves = {a: [30, 30, 30, 5, 5], b: [20] * 5}
    for action in ("commit", "reveal"):
        for who in (a, b):
            payload = ({"commitment": calls.commitment(moves[who], None, nonces[who])}
                       if action == "commit" else {"move": moves[who], "nonce": nonces[who]})
            body = _signed_action_body(client, sid, who, action, payload, "Reading this opponent")
            assert client.post(f"/sessions/{sid}/action", json=body).status_code == 200
    return sid, a, b


def test_a_finished_game_keeps_what_was_agreed_beside_the_result(client):
    cfg = {"challenge": {"with_call": False, "note": "One blotto round, with calls, no stakes"}}
    sid, a, b = _finished_with(client, cfg)
    got = client.get(f"/sessions/{sid}").json()
    assert got["status"] == "completed", "kept through every move that saved the game"
    ag = got["agreed"]
    assert ag["format"] == agreed.FORMAT
    assert ag["submitted"] == cfg, "the settings exactly as sent, before the relay added to them"
    assert ag["submitted_sha256"] == agreed.sha256(cfg)
    assert ag["accepted"] == got["config"] and ag["accepted"]["challenge"]["kind"] == "open"
    assert ag["accepted_sha256"] == agreed.sha256(got["config"])
    assert "not the settings" in ag["signed"]["covers"]
    assert "with_call is false and governs" in ag["governs"] and "note says calls" in ag["governs"]
    [row] = [x for x in client.get("/results", params={"agent_id": b}).json()["results"] if x["session_id"] == sid]
    assert row["agreed"] == ag


def test_the_export_line_carries_it(client):
    from emporia.routes import games_export
    cfg = {"challenge": {"with_call": True}}
    sid, a, b = _finished_with(client, cfg)
    line = games_export.line_of(rs.load_session(sid))
    assert line is not None and line["agreed"]["submitted"] == cfg
    json.dumps(line)


def test_no_conflict_means_no_governs_line(client):
    sid, a, b = _blotto(client, [30, 30, 30, 5, 5], [20] * 5)
    ag = client.get(f"/sessions/{sid}").json()["agreed"]
    assert ag["submitted"] == {} and "governs" not in ag


def test_the_note_and_with_call_are_compared_in_words():
    assert agreed.governs({"challenge": {"with_call": False, "note": "with calls"}})
    assert agreed.governs({"challenge": {"with_call": True, "note": "no calls, please"}})
    assert agreed.governs({"challenge": {"with_call": True, "note": "with a call"}}) is None
    assert agreed.governs({"challenge": {"with_call": False, "note": "just a quick game"}}) is None
    assert agreed.governs({"chat": True}) is None


def test_an_old_game_without_the_record_reads_as_null(client):
    sid, a, b = _blotto(client, [30, 30, 30, 5, 5], [20] * 5)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET agreed_json = NULL WHERE session_id = ?", (sid,))
    assert client.get(f"/sessions/{sid}").json()["agreed"] is None
