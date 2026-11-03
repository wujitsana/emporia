"""P180: SDK agents get the wallet's caps, daily total and approval step."""
import asyncio

import pytest

from emporia.agent_sdk import EmporiaAgent, SpendingLimitError


def _agent(name: str = "", **kw):
    # P275: the daily total is on disk, per agent — a fresh name per test unless one is shared on purpose.
    import uuid
    return EmporiaAgent("http://relay.test", name or f"sdk_spender_{uuid.uuid4().hex[:8]}", "00", **kw)


def test_the_per_payment_cap_and_the_daily_total():
    a = _agent(max_payment_cents=500, max_daily_cents=800)
    with pytest.raises(SpendingLimitError, match="per-payment cap"):
        asyncio.run(a._check_spend(5_010_000, "x"))
    asyncio.run(a._check_spend(5_000_000, "x"))            # $5.00
    with pytest.raises(SpendingLimitError, match="in 24 hours"):
        asyncio.run(a._check_spend(4_000_000, "x"))        # $5 + $4 > $8
    asyncio.run(a._check_spend(3_000_000, "x"))            # $8 exactly


def test_above_the_threshold_it_needs_a_yes():
    asked = []
    a = _agent(approve_above_cents=100, approve=lambda cents, what: asked.append(cents) or cents < 300)
    asyncio.run(a._check_spend(1_000_000, "x"))            # $1.00: at the threshold, not above
    asyncio.run(a._check_spend(2_000_000, "x"))            # $2.00: asked, yes
    with pytest.raises(SpendingLimitError, match="not approved"):
        asyncio.run(a._check_spend(4_000_000, "x"))        # $4.00: asked, no
    assert asked == [200, 400]


def test_no_approver_means_no_payment_above_the_threshold():
    with pytest.raises(SpendingLimitError):
        asyncio.run(_agent(approve_above_cents=0)._check_spend(10_000, "x"))


def test_an_async_approver_is_awaited():
    async def approve(cents, what):
        return True
    asyncio.run(_agent(approve_above_cents=0, approve=approve)._check_spend(10_000, "x"))


def test_the_daily_total_outlives_the_agent_object():
    """P275: a new EmporiaAgent for the same agent — a restarted script — still
    counts what the last one signed today."""
    _agent("sdk_restarted", max_daily_cents=800)
    asyncio.run(_agent("sdk_restarted", max_daily_cents=800)._check_spend(5_000_000, "x"))
    with pytest.raises(SpendingLimitError, match="in 24 hours"):
        asyncio.run(_agent("sdk_restarted", max_daily_cents=800)._check_spend(4_000_000, "x"))


def test_the_sdk_and_the_mcp_wallet_share_one_daily_total():
    from unittest.mock import patch

    from emporia import agent_wallet
    with patch.dict("os.environ", {"EMPORIA_X402_MAX_DAILY_CENTS": "800"}):
        agent_wallet._charge_daily("sdk_and_mcp", 5_000_000)            # the MCP server paid $5
        with pytest.raises(SpendingLimitError, match="in 24 hours"):
            asyncio.run(_agent("sdk_and_mcp", max_daily_cents=800)._check_spend(4_000_000, "x"))
        asyncio.run(_agent("sdk_and_mcp", max_daily_cents=800)._check_spend(3_000_000, "x"))
        with pytest.raises(agent_wallet.AgentWalletError, match="daily cap"):
            agent_wallet._charge_daily("sdk_and_mcp", 10_000)             # and the MCP side sees the SDK's


def test_a_charge_refused_before_signing_is_taken_back():
    from emporia import agent_wallet
    a = _agent("sdk_refunded", max_daily_cents=800)
    asyncio.run(a._check_spend(5_000_000, "x"))
    agent_wallet.refund_daily("sdk_refunded", 5_000_000)
    assert agent_wallet.ledger_spent("sdk_refunded") == 0


def test_two_processes_paying_for_one_agent_never_pass_the_cap_together(tmp_path):
    """The log is held across processes: four paying at once, $3 each against
    an $8 cap — exactly two are charged."""
    import subprocess
    import sys
    code = ("import sys\nfrom emporia import agent_wallet as w\n"
            "try:\n    w.charge_daily('sdk_racing', 3_000_000, 8_000_000); print('ok')\n"
            "except w.AgentWalletError:\n    print('refused')\n")
    procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert sorted(outs) == ["ok", "ok", "refused", "refused"], outs


def test_review_a_payment_that_fails_before_it_is_signed_is_taken_off_the_daily_total():
    """Review of P276: the charge is on disk now, so a payment that could not be built must be refunded."""
    from unittest.mock import patch

    import httpx

    from emporia import agent_wallet, payee, x402_payer
    a = _agent(max_daily_cents=800, x402_private_key="0x" + "11" * 32)
    req = {"network": "eip155:84532", "amount": "2000000", "payTo": "0x" + "ab" * 20}
    resp = httpx.Response(402, json={"x402": {"accepts": [req]}},
                          request=httpx.Request("POST", "http://relay.test/rooms/r/join"))

    def broken(*args, **kwargs):
        raise ValueError("a malformed requirement")
    with patch.object(x402_payer, "payment_required_from", lambda d: {"accepts": [req]}), \
         patch.object(x402_payer, "select_requirement", lambda c, networks=None: req), \
         patch.object(payee, "check_pinned", lambda *a, **k: "the payee"), \
         patch.object(x402_payer, "build_payment_payload", broken), \
         pytest.raises(ValueError, match="malformed"):
        asyncio.run(a._pay_and_retry_x402(resp, lambda header: None))
    assert agent_wallet.ledger_spent(a.profile_id) == 0
