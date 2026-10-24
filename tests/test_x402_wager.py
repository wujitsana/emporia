"""P154: a two-player wager staked in USDC escrow — each player's hold is paid
to the opponent if they lose; no card, and the relay never holds the money."""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from eth_account import Account

from emporia import commit_reveal, escrow
from emporia import relay_server as rs
from tests.test_emporia import _register_keyed, _signed_action_body
from tests.test_escrow_purchase import AMOUNT, _sql, chain, client  # noqa: F401  (fixtures)
from tests.test_platform_bridge import _agent_events

WAGER = {"mode": "x402", "stake_per_participant": "5.00", "currency": "usd"}


def _player(client):
    agent, key = "xw_" + uuid.uuid4().hex[:8], Account.create()
    _register_keyed(client, agent)
    r = client.post(f"/agents/{agent}/x402-wallet", json={"network_family": "evm", "address": key.address},
                    headers={"x-emporia-agent-id": agent})
    assert r.status_code == 200, r.text
    return agent, key


def _hdr(a):
    return {"x-emporia-agent-id": a}


def _wager(client, module="emporia:blotto:v1"):
    (a, ka), (b, kb) = _player(client), _player(client)
    r = client.post("/sessions", json={"module_type": module, "creator_agent_id": a,
                                       "payment_rules": WAGER}, headers=_hdr(a))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b)).status_code == 200
    return sid, (a, ka), (b, kb)


def _stake(client, sid, who, key):
    first = client.post(f"/sessions/{sid}/fund", headers=_hdr(who))
    assert "escrow" in first.json(), first.text
    offer = first.json()["escrow"]
    header = escrow.sign_offer(offer, key.key.hex(), expected_amount=AMOUNT)
    return offer, client.post(f"/sessions/{sid}/fund", headers={**_hdr(who), "PAYMENT-ESCROW": header})


def _play(client, sid, a, b, move_a, move_b):
    nonces = {a: commit_reveal.new_nonce(), b: commit_reveal.new_nonce()}
    moves = {a: move_a, b: move_b}
    r = None
    for action in ("commit", "reveal"):
        for who in (a, b):
            payload = ({"commitment": commit_reveal.commitment(moves[who], nonces[who])}
                       if action == "commit" else {"move": moves[who], "nonce": nonces[who]})
            r = client.post(f"/sessions/{sid}/action",
                            json=_signed_action_body(client, sid, who, action, payload, "Reading this opponent"))
            assert r.status_code == 200, r.text
    return r


def test_each_stake_is_held_for_the_opponent_and_the_loser_pays_the_winner(client, chain):
    sid, (a, ka), (b, kb) = _wager(client)
    s = rs.load_session(sid)
    assert s["status"] == "awaiting_funding", "free to create and join; staked once both are in"
    assert [e for e in _agent_events(a, "funding_required") if e["session_id"] == sid][0]["opponent"] == b

    offer, r = _stake(client, sid, a, ka)
    assert r.status_code == 200, r.text
    assert offer["payment_info"]["receiver"].lower() == kb.address.lower(), "a's stake pays b if a loses"
    assert int(offer["payment_info"]["min_fee_bps"]) == 500, "2× on one stake = 2.5% of the pot"
    assert rs.load_session(sid)["status"] == "awaiting_funding", "waits for the second stake"
    again = client.post(f"/sessions/{sid}/fund", headers=_hdr(a))
    assert again.status_code == 409 and "already staked" in again.text, "staked once"

    assert _stake(client, sid, b, kb)[1].status_code == 200
    assert rs.load_session(sid)["status"] == "active"

    r = _play(client, sid, a, b, [30, 30, 30, 5, 5], [20] * 5)
    assert r.json()["outcome"]["winner"] == a
    [(captured, amount)] = chain.captured
    assert captured.receiver.lower() == ka.address.lower() and amount == AMOUNT, "b's hold, to a"
    [voided] = chain.voided
    assert voided.payer.lower() == ka.address.lower(), "a's own hold, back to a"
    [row] = _sql("SELECT * FROM settlements WHERE session_id = ?", sid)
    assert (row["rail"], row["winner_id"], row["transfer_status"]) == ("x402", a, "escrow_captured")
    assert (row["total_stake_cents"], row["platform_fee_cents"], row["winner_payout_cents"]) == (1000, 25, 975)
    assert (row["platform_fee_atomic"], row["winner_payout_atomic"]) == ("250000", "9750000"), \
        "the winner receives the pot less 2.5%: 4.75 captured + 5.00 returned"


def test_a_draw_returns_both_stakes(client, chain):
    sid, (a, ka), (b, kb) = _wager(client)
    _stake(client, sid, a, ka)
    _stake(client, sid, b, kb)
    r = _play(client, sid, a, b, [20] * 5, [20] * 5)
    assert r.json()["outcome"]["outcome_type"] == "draw"
    assert not chain.captured and len(chain.voided) == 2


def test_a_wager_left_half_staked_is_voided_by_the_idle_sweep(client, chain, monkeypatch):
    from emporia.routes import sessions as routes
    sid, (a, ka), _ = _wager(client)
    _stake(client, sid, a, ka)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                     ((datetime.now(UTC) - timedelta(hours=25)).isoformat(), sid))
    asyncio.run(routes.sweep_idle_sessions())
    assert rs.load_session(sid)["status"] == "abandoned"
    assert len(chain.voided) == 1, "a's stake went back"


def test_an_x402_wager_is_for_two_players_with_wallets(client, chain):
    (a, _), _ = _player(client), None
    r = client.post("/sessions", json={"module_type": "emporia:two-thirds:v1", "creator_agent_id": a,
                                       "config": {"players": 3}, "payment_rules": WAGER}, headers=_hdr(a))
    assert r.status_code == 400 and "two players" in r.text
    nowallet = "xn_" + uuid.uuid4().hex[:8]
    _register_keyed(client, nowallet)
    r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": nowallet,
                                       "payment_rules": WAGER}, headers=_hdr(nowallet))
    assert r.status_code == 409 and "x402 wallet" in r.text


def test_only_a_player_stakes(client, chain):
    sid, _, _ = _wager(client)
    outsider, _ = _player(client)
    assert client.post(f"/sessions/{sid}/fund", headers=_hdr(outsider)).status_code == 403
