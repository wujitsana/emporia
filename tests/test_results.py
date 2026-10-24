"""P134: the leaderboard ranks games only, counts every rail's payout as paid,
and finished games are readable as results."""
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import commit_reveal
from emporia import relay_server as rs
from emporia.relay_server import app
from emporia.standing import compute_standings
from tests.test_emporia import _register_keyed, _signed_action_body

BLOTTO = "emporia:blotto:v1"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51219))


def _blotto(client, move_a, move_b):
    a, b = f"ra_{uuid.uuid4().hex[:8]}", f"rb_{uuid.uuid4().hex[:8]}"
    for who in (a, b):
        _register_keyed(client, who)
    sid = client.post("/sessions", json={"module_type": BLOTTO, "creator_agent_id": a},
                      headers={"x-emporia-agent-id": a}).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers={"x-emporia-agent-id": b})
    nonces = {a: commit_reveal.new_nonce(), b: commit_reveal.new_nonce()}
    moves = {a: move_a, b: move_b}
    for action in ("commit", "reveal"):
        for who in (a, b):
            payload = ({"commitment": commit_reveal.commitment(moves[who], nonces[who])}
                       if action == "commit" else {"move": moves[who], "nonce": nonces[who]})
            body = _signed_action_body(client, sid, who, action, payload, "Reading this opponent")
            assert client.post(f"/sessions/{sid}/action", json=body).status_code == 200
    return sid, a, b


def test_a_finished_game_is_a_result_with_its_record(client):
    sid, a, b = _blotto(client, [30, 30, 30, 5, 5], [20] * 5)
    res = client.get("/results", params={"agent_id": b}).json()["results"]
    [r] = [x for x in res if x["session_id"] == sid]
    assert (r["winner"], r["outcome_type"], r["participants"]) == (a, "won", [a, b])
    assert r["record"]["history"][0]["allocations"][b] == [20] * 5, \
        "an opponent's past allocations are what a player studies"
    assert all(b in x["participants"] for x in res)


def test_the_leaderboard_counts_games_not_sales(client):
    sid, a, b = _blotto(client, [30, 30, 30, 5, 5], [20] * 5)
    rows = {r["agent_id"]: r for r in client.get("/leaderboard", params={"limit": 500}).json()["standings"]}
    assert rows[a]["wins"] == 1 and rows[a]["entries"] == 1
    assert rows[b]["wins"] == 0 and rows[b]["entries"] == 1
    seller = f"rs_{uuid.uuid4().hex[:8]}"
    rs.record_settlement({"session_id": f"room_{uuid.uuid4().hex[:8]}", "winner_id": seller,
                          "payer_id": a, "status": "settled", "transfer_status": "transferred",
                          "winner_payout_cents": 98, "payment_intent_ids": ["pi_room"]})
    names = {r["agent_id"] for r in client.get("/leaderboard", params={"limit": 500}).json()["standings"]}
    assert seller not in names, "a room's paid entry is a sale, not a win"


def test_escrow_and_direct_payouts_count_as_paid():
    rows = [{"winner_id": w, "status": "settled", "winner_payout_cents": 98, "transfer_status": t}
            for w, t in (("e", "escrow_captured"), ("d", "destination"), ("x", "direct"),
                         ("s", "transferred"), ("o", "pending_connect"))]
    owed = {s.agent_id: s.prize_owed_cents for s in compute_standings(rows, [])}
    assert owed == {"e": 0, "d": 0, "x": 0, "s": 0, "o": 98}
