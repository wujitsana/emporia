"""P179: the relay operator's overview — settings and aggregate activity."""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216))


def test_only_the_operator_sees_the_overview(client):
    _register(client, "ra_op")
    _register(client, "ra_other")
    with patch.dict(os.environ, {"EMPORIA_AGENT_ID": "ra_op", "STRIPE_WEBHOOK_SECRET": "whsec_dont_show"}):
        assert client.get("/operator/overview", headers={"x-emporia-agent-id": "ra_other"}).status_code == 403
        assert client.get("/operator/overview").status_code == 403
        r = client.get("/operator/overview", headers={"x-emporia-agent-id": "ra_op"})
    assert r.status_code == 200
    body = r.json()
    assert "whsec_dont_show" not in r.text, "a secret shows only as set / not set"
    items = {i["label"]: i for i in body["settings"]}
    assert items["Operator agent"]["value"] == "ra_op"
    assert items["Stripe webhook secret"]["value"] == "set"
    assert items["Your fee"]["command"].startswith("OPERATOR_FEE_BPS")
    stats = body["stats"]
    assert stats["agents"]["registered"] >= 2
    assert len(stats["daily"]) == 30 and set(stats["daily"][-1]) >= {"day", "agents", "sessions", "fees_cents"}
    assert {"payments", "fees", "escrow_holds", "usdc_direct"} <= set(stats["money"])


def test_the_overview_holds_no_message_or_address(client):
    _register(client, "ra_op2")
    r = client.post("/sessions", json={
        "module_type": "emporia:service:v1", "creator_agent_id": "ra_op2",
        "config": {"description": "a very private brief"}, "payment_rules": {"mode": "free"},
    }, headers={"x-emporia-agent-id": "ra_op2"})
    assert r.status_code in (200, 201)
    with patch.dict(os.environ, {"EMPORIA_AGENT_ID": "ra_op2"}):
        r = client.get("/operator/overview", headers={"x-emporia-agent-id": "ra_op2"})
    assert "a very private brief" not in r.text
    assert r.json()["stats"]["sessions"]["created_7d"] >= 1
