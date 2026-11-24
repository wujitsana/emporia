"""A game the clock ended is still a game a 2.1.0 client verifies: the end lives in `state.metadata`, and `data`
stays what a replay of the signed log gives (`verify._replay_rand` compares it with the stored one)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import beacon
from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.verify import verify_game
from tests import test_bargain as B
from tests import test_games_pack as G
from tests import test_spillway as SP
from tests import test_tesseract_relay as TT
from tests.test_spillway import _fake_or_real
from tests.test_tesseract_relay import w  # noqa: F401  (fixture)


def _record(client, sid, agents):
    hdr = {"x-emporia-agent-id": agents[0]}
    return {
        "session": client.get(f"/sessions/{sid}", headers=hdr).json(),
        "log": client.get(f"/sessions/{sid}/audit", headers=hdr).json()["chain"],
        "public_keys": {x: client.get(f"/agents/{x}", headers=hdr).json()["public_key_hex"] for x in agents},
        "receipts": [], "relay_key": client.get("/health").json()["relay_public_key"],
        "relay_verdict": next(r for r in client.get("/results", params={"agent_id": agents[0]}).json()["results"]
                              if r["session_id"] == sid),
    }


def _check(client, sid, agents, termination=None):
    s = rs.load_session(sid)
    assert s["status"] == "completed"
    end = s["state"]["metadata"]["clock_end"]
    assert termination is None or end["termination"] == termination
    assert "clock_end" not in s["state"]["data"], "the stored board is what the replay gives"
    module = get_interaction_module(s["module_type"])
    assert module.is_terminal(SessionState.from_dict(s["state"])) == (True, end)
    report = verify_game(**_record(client, sid, agents))
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(beacon, "verify", _fake_or_real)
    return TestClient(rs.app, client=("127.0.0.1", 51299))


def test_spillway_ended_by_the_clock_verifies(client):
    sid, agents = SP._game(client)
    _, mover, d = SP._gated(client, sid, agents[0])
    assert SP._drop(client, sid, mover, d["open"][0]).status_code == 200
    s = rs.load_session(sid)
    s["state"]["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(seconds=700)).isoformat()
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"],
                    payment_rules=s["payment_rules"], state=SessionState.from_dict(s["state"]),
                    current_agent=s["current_agent"], step_number=s["step_number"], status="active",
                    participants=s["participants"], created_at=s["created_at"],
                    updated_at=datetime.now(UTC).isoformat())
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())
    _check(client, sid, agents, "timeout")


def test_bargain_ended_by_the_clock_verifies(client):
    sid, a, b = B._game(client)
    B._act(client, sid, a, "offer", {"keep": {"silk": 1}})
    B._expire(sid)
    B._sweep()
    _check(client, sid, [a, b], "timeout")


def test_liars_dice_ended_by_the_clock_verifies(client):
    sid, agents = G._game(client, G.DICE, 3)
    order = G._order(sid)
    G._act(client, sid, order[0], "bid", {"quantity": 1, "face": 5})
    G._age(sid, "turn_started_at", 601)
    G._sweep()
    _check(client, sid, agents, "timeout")


def test_cv_auction_ended_by_the_clock_verifies(client):
    sid, agents = G._game(client, G.AUCTION, 3)
    G._bid(client, sid, agents[0], 40)
    G._age(sid, "phase_started_at", 601)
    G._sweep()
    _check(client, sid, agents)


def test_tesseract_ended_by_the_clock_verifies(w, monkeypatch):  # noqa: F811
    monkeypatch.setattr(beacon, "verify", _fake_or_real)
    names = TT.agents(w, 3)
    sid = w.table(names)
    limit = int(w.state(sid)["state"]["data"]["move_time_limit_s"])
    for _ in range(5):
        w.clock.advance(limit + 25)
        w.sweep()
    w.clock.advance(int(w.state(sid)["state"]["data"]["reveal_window_s"]) + 25)
    w.sweep()
    _check(w.client, sid, names)


def test_a_move_that_lands_while_the_sweep_awaits_its_beacon_is_not_overwritten(client, monkeypatch):
    """Finding 3: the sweep read the row, awaited the beacon, and a move landed meanwhile: it ends nothing."""
    from emporia.routes import sessions as S
    sid, a, b = B._game(client)
    B._act(client, sid, a, "offer", {"keep": {"silk": 1}})
    B._expire(sid)
    real = S._with_beacon

    async def late_move(module, session_id, state):
        out = await real(module, session_id, state)
        s = rs.load_session(session_id)               # a move is accepted: the step moves on
        rs.save_session(session_id=session_id, module_type=s["module_type"], config=s["config"],
                        payment_rules=s["payment_rules"], state=SessionState.from_dict(s["state"]),
                        current_agent=s["current_agent"], step_number=s["step_number"] + 1, status="active",
                        participants=s["participants"], created_at=s["created_at"],
                        updated_at=datetime.now(UTC).isoformat())
        return out

    monkeypatch.setattr(S, "_with_beacon", late_move)
    B._sweep()
    s = rs.load_session(sid)
    assert s["status"] == "active" and "clock_end" not in s["state"]["metadata"]
    assert S._step_still(sid, s["step_number"])
    assert rs.claim_session_status(sid, "active", "completed")
    assert not S._step_still(sid, s["step_number"]), "a session that is no longer active is not the step"
