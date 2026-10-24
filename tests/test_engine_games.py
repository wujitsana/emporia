"""P135: no money on games an engine decides, unless the operator opts in."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import emporia.relay_server  # noqa: F401
from emporia import bounties
from emporia.module_sdk import MODULE_REGISTRY, list_available_modules
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


@pytest.fixture
def no_opt_in(monkeypatch):
    monkeypatch.delenv("EMPORIA_ENGINE_GAME_STAKES", raising=False)


def test_a_staked_chess_game_is_refused_before_any_payment(no_opt_in):
    client = TestClient(app, client=("127.0.0.1", 51220))
    a = f"eg_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={
        "module_type": "emporia:chess:v1", "creator_agent_id": a,
        "payment_rules": {"mode": "stripe_pi", "stake_per_participant": "1.00", "currency": "usd"}},
        headers={"x-emporia-agent-id": a})
    assert r.status_code == 400 and "decided by engines" in r.text and "blotto" in r.text
    free = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": a},
                       headers={"x-emporia-agent-id": a})
    assert free.status_code == 200, "free chess stays"


def test_a_chess_bounty_is_refused(no_opt_in):
    now = datetime.now(UTC)
    with pytest.raises(bounties.BountyRejected, match="decided by engines"):
        bounties.validate_spec(
            module_type="emporia:chess:v1", prize_cents=500, rail="stripe", funder_id="f",
            entry_deadline=now + timedelta(days=1), now=now, registry=MODULE_REGISTRY)


def test_the_listing_says_which_games_take_stakes(no_opt_in, monkeypatch):
    listed = {m["module_type"]: m for m in list_available_modules()}
    assert listed["emporia:chess:v1"]["engine_decidable"] is True
    assert listed["emporia:chess:v1"]["stakes_allowed"] is False
    assert listed["emporia:blotto:v1"]["stakes_allowed"] is True
    monkeypatch.setenv("EMPORIA_ENGINE_GAME_STAKES", "1")
    assert {m["module_type"]: m for m in list_available_modules()}["emporia:chess:v1"]["stakes_allowed"]
