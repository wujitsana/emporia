"""scripts/money_run.py: its safety checks, offline, with fakes. Nothing here reaches a relay or a chain."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("money_run", ROOT / "scripts" / "money_run.py")
mr = importlib.util.module_from_spec(spec)
sys.modules["money_run"] = mr
spec.loader.exec_module(mr)

A, B = "0x" + "aa" * 20, "0x" + "bb" * 20


def req(network=mr.NETWORK, asset=mr.USDC, amount="50000", pay_to=A):
    return {"network": network, "asset": asset, "amount": amount, "payTo": pay_to}


def test_mainnet_and_other_assets_are_refused():
    mr.refuse_unless_testnet(mr.NETWORK, mr.USDC)
    mr.refuse_unless_testnet(mr.NETWORK, mr.USDC.lower())
    with pytest.raises(mr.Refused):
        mr.refuse_unless_testnet("eip155:8453", "0x833589fCd6eDb6E08f4c7C32D4f71b54bdA02913")
    with pytest.raises(mr.Refused):
        mr.refuse_unless_testnet(mr.NETWORK, "0x" + "11" * 20)


def test_a_requirement_must_be_the_exact_amount_to_the_payee():
    mr.check_requirement(req(), 5, A)
    for bad in (req(amount="50001"), req(pay_to=B), req(network="eip155:8453")):
        with pytest.raises(mr.Refused):
            mr.check_requirement(bad, 5, A)


def test_the_spend_cap_and_the_approval_floor():
    s = mr.Spend(cap_cents=50)
    s.pay(15)
    with pytest.raises(mr.Refused):
        s.allow(50)                    # not below the approval threshold
    s.pay(30)
    with pytest.raises(mr.Refused):
        s.pay(10)                      # 45 + 10 is over the cap
    assert s.spent_cents == 45


class FakeChain:
    def __init__(self):
        self.reads = 0

    def chain_id(self):
        return 84532

    def block(self):
        return 1

    def usdc(self, address):
        self.reads += 1
        return 5_000_000

    def transfers(self, payer, payee, since):
        return []

    def succeeded(self, tx):
        return True


class FakeAgent:
    def __init__(self, address, name):
        self.address, self.name, self.calls = address, name, []

    def tool(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name == "x402_wallet_info":
            return {"address": self.address, "pay_networks": [mr.NETWORK], "registered_on_relay": self.address}
        if name == "create_room":
            return {"success": True, "room": {"room_id": "room_x"}}
        if name == "join_room":
            return {"error": "payment_required", "detail": {"x402": {"accepts": [req(pay_to=A)]}}, "success": False}
        if name == "create_session":
            return {"success": True, "session_id": "sess_x"}
        if name == "join_session":
            return {"success": True}
        if name == "fund_session":
            return {"error": "Relay 409", "success": False}
        return {"success": True}


def run_fakes(**kw):
    a, b, chain = FakeAgent(A, "alpha"), FakeAgent(B, "beta"), FakeChain()
    spend = mr.Spend()
    report = mr.Report()
    ok = mr.run(a, b, chain, report, relay_url="http://relay.invalid", sleep=lambda s: None, stamp="t",
                spend=spend, **{"room_cents": 5, "service_cents": 10, **kw})
    return ok, a, b, spend, report


def test_a_dry_run_pays_nothing():
    ok, a, b, spend, _ = run_fakes(dry_run=True)
    assert ok
    assert spend.spent_cents == 0
    paying = [c for c in a.calls + b.calls if c[2].get("pay_with_x402")]
    assert paying == []
    assert not any(c[0] == "fund_session" and c[2] for c in b.calls)


def test_a_plan_over_the_cap_stops_before_anything_happens():
    ok, a, b, spend, report = run_fakes(dry_run=False, room_cents=30, service_cents=30)
    assert not ok
    assert a.calls == [] and b.calls == []
    assert any("refused" in t for _, t in report.steps)


def test_a_failed_payment_stops_the_run_and_is_not_retried():
    class Failing(FakeAgent):
        def tool(self, name, *args, **kwargs):
            if name == "join_room" and kwargs.get("pay_with_x402"):
                self.calls.append((name, args, kwargs))
                return {"error": "boom", "success": False}
            return super().tool(name, *args, **kwargs)

    a, b = FakeAgent(A, "alpha"), Failing(B, "beta")
    report = mr.Report()
    ok = mr.run(a, b, FakeChain(), report, dry_run=False, room_cents=5, service_cents=10, spend=mr.Spend(),
                relay_url="x", sleep=lambda s: None, stamp="t")
    assert not ok
    assert len([c for c in b.calls if c[0] == "join_room" and c[2].get("pay_with_x402")]) == 1
    assert not any(c[0] == "create_session" for c in b.calls)
