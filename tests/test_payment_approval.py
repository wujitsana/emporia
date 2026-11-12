"""P166: above a threshold the operator approves a payment, asked through the
agent's own app (an MCP elicitation, which Hermes routes to its approvals)."""
import asyncio
from pathlib import Path

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


def test_a_form_left_unticked_declines():
    async def accept_untouched(message, response_type, params, context):
        return {"approve": False}
    assert "declined" in _run(accept_untouched, 6_000_000)


def test_the_form_asks_with_the_box_unticked():
    seen = {}

    async def look(message, response_type, params, context):
        seen.update(params.requestedSchema if hasattr(params, "requestedSchema") else {})
        return {"approve": True}
    _run(look, 6_000_000)
    assert seen.get("properties", {}).get("approve", {}).get("default") is False
    assert "approve" in seen.get("required", [])


def test_mainnet_asks_for_every_payment_whatever_the_threshold(monkeypatch):
    """Review: with EMPORIA_X402_MAINNET on, a payment under the threshold (or with no threshold at all)
    was signed without asking. Real money is put to the operator every time."""
    monkeypatch.setenv("EMPORIA_X402_MAINNET", "1")
    asked = []

    async def approve(message, response_type, params, context):
        asked.append(message)
        return {"approve": True}

    assert _run(approve, 1_000) == "paid"                    # $0.001, far under the $5 threshold
    assert "every payment needs your approval" in asked[0] and "agent_a" in asked[0]
    monkeypatch.delenv("EMPORIA_PAYMENT_APPROVAL_CENTS")      # no threshold at all
    assert _run(approve, 1_000) == "paid" and len(asked) == 2


def test_mainnet_with_no_way_to_ask_refuses(monkeypatch):
    monkeypatch.setenv("EMPORIA_X402_MAINNET", "1")
    monkeypatch.delenv("EMPORIA_PAYMENT_APPROVAL_CENTS")
    out = _run(None, 1_000)
    assert "refused" in out and "cannot ask" in out and "mainnet" in out


def test_testnet_keeps_its_threshold(monkeypatch):
    monkeypatch.setenv("EMPORIA_X402_MAINNET", "0")
    asked = []
    assert _run(lambda *a: asked.append(a) or {"approve": True}, 4_000_000) == "paid" and asked == []


class _Signer:
    """A signer client: what /v1/wallet says of mainnet, and a record of what was handed over."""

    def __init__(self, mainnet):
        self.mainnet, self.called = mainnet, []

    def wallet(self, fresh=False):
        return {"mainnet": self.mainnet}

    def call(self, op, payload):
        self.called.append(op)
        return {"header": "h"}


@pytest.mark.parametrize("mainnet, asked, paid", [(True, 1, True), (False, 0, True)])
def test_behind_a_signer_mainnet_is_asked_here_before_the_hand_over(monkeypatch, mainnet, asked, paid):
    """B1: with the wallet behind a signer the mainnet unlock is in the signer's policy, not the profile, and
    pay_tempo/sign_exact/sign_escrow handed over before require_approval. Now the operator is asked first."""
    monkeypatch.delenv("EMPORIA_X402_MAINNET", raising=False)
    monkeypatch.delenv("EMPORIA_PAYMENT_APPROVAL_CENTS", raising=False)
    signer, seen = _Signer(mainnet), []
    monkeypatch.setattr(agent_wallet, "remote", lambda: signer)
    monkeypatch.setattr(agent_wallet, "APPROVER", lambda m: seen.append(m) or True)
    offer = {"network": "eip155:42431", "request": {"amount": "1000", "currency": "USD", "recipient": "0xabc"}}
    assert agent_wallet.pay_tempo("agent_a", offer) == "h"
    assert agent_wallet.sign_escrow("agent_a", {"network": "eip155:84532", "escrow": "0xdef"}, expected_amount=1000) == "h"
    if mainnet:
        assert "0xabc" in seen[0] and "Tempo" in seen[0] and "0xdef" in seen[1] and "Base Sepolia" in seen[1]
    assert len(seen) == 2 * asked and signer.called == ["tempo", "escrow"]
    if mainnet:
        assert "0.001" in seen[0] and "agent_a" in seen[0] and "Terms " in seen[0]
    assert agent_wallet.approval_threshold_cents() == (0 if mainnet else None)


def test_behind_a_signer_a_declined_or_unaskable_mainnet_payment_is_never_handed_over(monkeypatch):
    monkeypatch.delenv("EMPORIA_X402_MAINNET", raising=False)
    signer = _Signer(True)
    monkeypatch.setattr(agent_wallet, "remote", lambda: signer)
    for approver, text in ((lambda m: False, "declined"), (None, "cannot ask")):
        monkeypatch.setattr(agent_wallet, "APPROVER", approver)
        with pytest.raises(agent_wallet.AgentWalletError, match=text):
            agent_wallet.pay_tempo("agent_a", {"request": {"amount": "1000"}})
    assert signer.called == []


def test_the_signer_reports_whether_mainnet_is_unlocked_in_its_policy(monkeypatch, tmp_path):
    """Behaviour, not source text: the signer's /v1/wallet answers `mainnet` from the agent's policy."""

    from emporia import signer
    seen = {}

    def scope(aid):
        return agent_wallet.Scope(env={"EMPORIA_X402_MAINNET": seen["flag"], "EMPORIA_X402_PAY_NETWORKS": "eip155:84532"},
                                  key_dir=tmp_path)
    monkeypatch.setattr(signer, "_scope", scope)
    monkeypatch.setattr(signer, "_agent", lambda request: "agent_a")
    monkeypatch.setattr(agent_wallet, "_families", lambda aid: ())
    for flag in ("1", "0"):
        seen["flag"] = flag
        got = signer.wallet(type("Req", (), {})())
        assert got["mainnet"] is (flag == "1")


def test_a_signer_that_says_nothing_of_mainnet_counts_as_mainnet(monkeypatch):
    """S1: an older signer's /v1/wallet has no `mainnet`: ask, as for a signer that says yes."""
    class Old(_Signer):
        def wallet(self, fresh=False):
            return {"pay_networks": []}
    monkeypatch.delenv("EMPORIA_X402_MAINNET", raising=False)
    monkeypatch.setattr(agent_wallet, "remote", lambda: Old(None))
    assert agent_wallet.mainnet_on() is True
    monkeypatch.setattr(agent_wallet, "remote", lambda: _Signer(False))
    assert agent_wallet.mainnet_on() is False


def test_inside_the_signer_its_own_policy_decides_and_the_agent_side_asks_on_mainnet(monkeypatch):
    """S3: threshold 0 is the agent side's (it asked through Hermes). In the signer's scope the signer follows its
    policy: no threshold means no question there (the texts say "no approval threshold at the signer")."""
    monkeypatch.delenv("EMPORIA_PAYMENT_APPROVAL_CENTS", raising=False)
    monkeypatch.setenv("EMPORIA_X402_MAINNET", "1")
    asked = []
    monkeypatch.setattr(agent_wallet, "APPROVER", lambda m: asked.append(m) or True)
    agent_wallet.require_approval("agent_a", 1_000, "USDC")                      # agent side: asks
    assert len(asked) == 1
    scope = agent_wallet.Scope(env={"EMPORIA_X402_MAINNET": "1"}, key_dir=Path("."), approver=lambda m: asked.append(m) or True)
    with agent_wallet.scope(scope):
        agent_wallet.require_approval("agent_a", 1_000, "USDC")                   # signer side: its policy has no threshold
        assert agent_wallet.approval_threshold_cents() is None
    assert len(asked) == 1


def test_a_declined_or_cancelled_mainnet_payment_is_not_paid(monkeypatch):
    """N1: the wallet_env approver says no, or nothing (cancel): not paid, never retried."""
    monkeypatch.setenv("EMPORIA_X402_MAINNET", "1")
    monkeypatch.setattr(agent_wallet, "APPROVER", lambda m: False)
    with pytest.raises(agent_wallet.AgentWalletError, match="declined"):
        agent_wallet.require_approval("agent_a", 1_000, "USDC")
    monkeypatch.setattr(agent_wallet, "APPROVER", lambda m: None)                 # cancelled / cannot ask
    with pytest.raises(agent_wallet.AgentWalletError, match="Not paid"):
        agent_wallet.require_approval("agent_a", 1_000, "USDC")


def test_sign_exact_behind_a_signer_approves_what_it_hands_over_and_nothing_else(monkeypatch):
    """S4: the requirement is chosen here by the local rule, shown as it is, and the signer is handed that one
    entry alone with its network pinned. A two-entry offer: the second network is the agent's pick."""
    from emporia import networks
    base, sepolia = "eip155:8453", "eip155:84532"
    monkeypatch.setenv("EMPORIA_X402_MAINNET", "0")
    accepts = [{"scheme": "exact", "network": n, "amount": a, "payTo": "0x" + p * 20, "maxTimeoutSeconds": 60,
                "asset": networks.NETWORKS[n].usdc, "extra": {"name": "USDC", "version": "2"}}
               for n, a, p in ((base, "900000", "aa"), (sepolia, "250000", "bb"))]
    body = {"x402": {"x402Version": 2, "resource": {"url": "http://relay/r"}, "accepts": accepts}}

    class Wallet(_Signer):
        def __init__(self):
            super().__init__(True)
            self.payload = None

        def wallet(self, fresh=False):
            return {"mainnet": True, "pay_networks": [sepolia, base], "families": ["evm"]}

        def call(self, op, payload):
            self.payload = payload
            return {"header": "h"}
    signer, seen = Wallet(), []
    monkeypatch.setattr(agent_wallet, "remote", lambda: signer)
    monkeypatch.setattr(agent_wallet, "APPROVER", lambda m: seen.append(m) or True)
    monkeypatch.setattr(agent_wallet, "_regate_actual", lambda header, counted: None)
    assert agent_wallet.sign_exact("agent_a", body) == "h"
    sent = signer.payload["body"]["x402"]["accepts"]
    assert [e["network"] for e in sent] == [sepolia] and signer.payload["network"] == sepolia
    flat = {"accepts": accepts, "resource": {"url": "http://relay/r"}}                  # a top-level challenge
    assert agent_wallet.sign_exact("agent_a", flat) == "h"
    assert [e["network"] for e in signer.payload["body"]["accepts"]] == [sepolia]
    assert "0.25" in seen[0] and "bb" * 20 in seen[0] and "aa" * 20 not in seen[0] and "Base Sepolia" in seen[0]
