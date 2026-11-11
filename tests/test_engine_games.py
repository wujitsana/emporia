"""P135: no money on games an engine decides (games take no stakes at all)."""
import uuid

from fastapi.testclient import TestClient

import emporia.relay_server  # noqa: F401
from emporia.module_sdk import list_available_modules
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


def test_a_staked_chess_game_is_refused_before_any_payment():
    client = TestClient(app, client=("127.0.0.1", 51220))
    a = f"eg_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={
        "module_type": "emporia:chess:v1", "creator_agent_id": a,
        "payment_rules": {"mode": "x402", "price": "1.00", "currency": "usd"}},
        headers={"x-emporia-agent-id": a})
    assert r.status_code == 400 and "takes no stakes" in r.text
    free = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": a},
                       headers={"x-emporia-agent-id": a})
    assert free.status_code == 200, "free chess stays"


def test_the_listing_says_which_games_are_engine_decidable():
    listed = {m["module_type"]: m for m in list_available_modules()}
    assert listed["emporia:chess:v1"]["engine_decidable"] is True
    assert listed["emporia:blotto:v2"]["engine_decidable"] is False
    assert all("stakes_allowed" not in m and m["price_model"] is None for m in listed.values()
               if m["module_type"].startswith("emporia:") and m["module_type"] != "emporia:service:v1")
