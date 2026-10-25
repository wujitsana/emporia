"""P166: above a threshold the operator approves a payment, asked through the
agent's own app (an MCP elicitation, which Hermes routes to its approvals)."""
import asyncio
import os
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from emporia import agent_wallet, mcp_server


def _server():
    """A one-tool server paying the way the real payment tools do."""
    server = FastMCP("approval-test")

    @server.tool
    def pay(amount_atomic: int) -> str:
        try:
            agent_wallet.require_approval("agent_a", amount_atomic, "USDC on Base Sepolia (testnet)")
        except agent_wallet.AgentWalletError as e:
            return f"refused: {e}"
        return "paid"
    return server


def _run(handler, amount, mode="legacy"):
    """mode "legacy": the classic handshake session Hermes's client opens, which
    carries server-initiated questions. "auto" negotiates the stateless
    2026-07-28 protocol, which cannot ask mid-call."""
    async def go():
        async with Client(_server(), elicitation_handler=handler, mode=mode) as client:
            return (await client.call_tool("pay", {"amount_atomic": amount})).content[0].text
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def threshold(monkeypatch):
    monkeypatch.setenv("EMPORIA_PAYMENT_APPROVAL_CENTS", "500")       # ask above $5
    monkeypatch.setattr(agent_wallet, "APPROVER", mcp_server._ask_operator)


def test_below_the_threshold_nobody_is_asked():
    asked = []
    assert _run(lambda *a: asked.append(a) or {"approve": True}, 4_000_000) == "paid"
    assert asked == []


def test_above_it_the_operator_is_asked_and_decides():
    seen = []

    async def approve(message, response_type, params, context):
        seen.append(message)
        return {"approve": True}

    async def decline(message, response_type, params, context):
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="decline")

    assert _run(approve, 6_000_000) == "paid"
    assert "6.00" in seen[0] and "$5.00" in seen[0] and "agent_a" in seen[0]
    assert "declined" in _run(decline, 6_000_000)


def test_a_client_that_cannot_ask_refuses_rather_than_pays():
    assert "cannot ask" in _run(None, 6_000_000)


def test_no_threshold_means_no_question(monkeypatch):
    monkeypatch.delenv("EMPORIA_PAYMENT_APPROVAL_CENTS")
    assert _run(None, 60_000_000) == "paid"


def test_a_stateless_session_cannot_ask_so_it_refuses():
    async def approve(message, response_type, params, context):
        return {"approve": True}
    assert "cannot ask" in _run(approve, 6_000_000, mode="auto")
