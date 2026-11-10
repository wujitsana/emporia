"""A relay that forgot an agent gets its own x402 pay-to back on the agent's start; a pay-to it has stays."""

from __future__ import annotations

import pytest

from emporia import agent_wallet, mcp_server


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


@pytest.fixture
def calls(monkeypatch):
    got = []
    monkeypatch.setattr(mcp_server, "AGENT_ID", "agent-a")
    monkeypatch.setattr(mcp_server, "register_x402_wallet",
                        lambda network_family="evm", agent_id="", address="": got.append(network_family)
                        or {"success": True})
    return got


def _relay(monkeypatch, body, status=200):
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: _Resp(body, status))


def test_a_missing_pay_to_is_registered_on_start(monkeypatch, calls):
    monkeypatch.setattr(agent_wallet, "_families", lambda aid: ("evm",))
    _relay(monkeypatch, {"x402_pay_to": {}})
    mcp_server._heal_x402_pay_to()
    assert calls == ["evm"]


def test_only_the_missing_family_is_registered(monkeypatch, calls):
    monkeypatch.setattr(agent_wallet, "_families", lambda aid: ("evm", "svm"))
    _relay(monkeypatch, {"x402_pay_to": {"evm": "0xSomeoneElsesHardwareWallet"}})
    mcp_server._heal_x402_pay_to()
    assert calls == ["svm"]


def test_a_pay_to_the_relay_has_is_left_alone(monkeypatch, calls):
    monkeypatch.setattr(agent_wallet, "_families", lambda aid: ("evm",))
    _relay(monkeypatch, {"x402_pay_to": {"evm": "0xElsewhere"}})
    mcp_server._heal_x402_pay_to()
    assert calls == []


def test_no_local_wallet_does_nothing(monkeypatch, calls):
    monkeypatch.setattr(agent_wallet, "_families", lambda aid: ())
    _relay(monkeypatch, {"x402_pay_to": {}})
    mcp_server._heal_x402_pay_to()
    assert calls == []


def test_a_relay_failure_is_swallowed(monkeypatch, calls):
    monkeypatch.setattr(agent_wallet, "_families", lambda aid: ("evm",))

    def boom(*a, **k):
        raise RuntimeError("relay down")
    monkeypatch.setattr(mcp_server, "_bearer_request", boom)
    mcp_server._heal_x402_pay_to()
    _relay(monkeypatch, {}, status=500)
    mcp_server._heal_x402_pay_to()
    assert calls == []
