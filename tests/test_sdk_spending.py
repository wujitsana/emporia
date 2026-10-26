"""P180: SDK agents get the wallet's caps, daily total and approval step."""
import asyncio

import pytest

from emporia.agent_sdk import EmporiaAgent, SpendingLimitError


def _agent(**kw):
    return EmporiaAgent("http://relay.test", "sdk_spender", "00", **kw)


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
