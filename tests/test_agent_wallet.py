"""P104: an MCP agent pays over x402 from its own wallet, within its own policy.

Before P104 every MCP payment tool was Stripe-only; x402 reached agents only
through the SDK, and a promised bounty's funder had no MCP tool to pay it with.
These pin the wallet's refusals (no wallet, no network allowlist, over the cap,
a network outside the allowlist), the key file's safety, and the MCP retry:
one paid retry carrying both the identity and the payment signature.
"""
from __future__ import annotations

import base64
import json
import os
import stat
from unittest.mock import patch

import pytest

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402

from emporia import agent_wallet, identity, mcp_server  # noqa: E402
from emporia.agent_wallet import AgentWalletError  # noqa: E402

NET = "eip155:84532"


def _challenge(amount_atomic: int, network: str = NET) -> dict:
    return {"x402": {
        "x402Version": 2,
        "resource": {"url": "http://relay/rooms/r1/join"},
        "accepts": [{
            "scheme": "exact", "network": network, "amount": str(amount_atomic),
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "payTo": Account.create().address, "maxTimeoutSeconds": 300,
            "extra": {"name": "USDC", "version": "2"},
        }],
    }}


@pytest.fixture
def wallet_env(tmp_path):
    env = {"EMPORIA_X402_PAY_NETWORKS": NET, "EMPORIA_X402_MAX_PAYMENT_CENTS": "500"}
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, env):
        yield tmp_path


def test_generate_writes_a_0600_key_once_and_address_matches(wallet_env):
    addr = agent_wallet.generate("buyer_one")
    path = agent_wallet.wallet_path("buyer_one")
    assert path.parent == wallet_env and path.name == "buyer_one.x402.secp256k1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert agent_wallet.address("buyer_one") == addr
    with pytest.raises(AgentWalletError, match="already exists"):
        agent_wallet.generate("buyer_one")


def test_no_wallet_is_a_clear_error_naming_the_fix(wallet_env):
    with pytest.raises(AgentWalletError, match="emporia.agent_wallet generate"):
        agent_wallet.sign_exact("nobody", _challenge(10_000))


def test_unset_network_allowlist_signs_nothing(wallet_env):
    agent_wallet.generate("buyer_two")
    with patch.dict(os.environ, {"EMPORIA_X402_PAY_NETWORKS": ""}):
        with pytest.raises(AgentWalletError, match="EMPORIA_X402_PAY_NETWORKS"):
            agent_wallet.sign_exact("buyer_two", _challenge(10_000))


def test_payment_over_the_cap_is_refused_before_signing(wallet_env):
    agent_wallet.generate("buyer_three")
    with pytest.raises(AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact("buyer_three", _challenge(5_000_001))   # $5.000001 > $5.00
    with pytest.raises(AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_escrow("buyer_three", {}, expected_amount=5_000_001)


def test_a_network_outside_the_allowlist_is_not_signed(wallet_env):
    from emporia.x402_payer import X402PayerError

    agent_wallet.generate("buyer_four")
    with pytest.raises(X402PayerError, match="not in this payer's allowed networks"):
        agent_wallet.sign_exact("buyer_four", _challenge(10_000, network="eip155:8453"))


def test_within_policy_signs_from_the_agents_own_address(wallet_env):
    addr = agent_wallet.generate("buyer_five")
    header = agent_wallet.sign_exact("buyer_five", _challenge(1_000_000))
    payload = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
    assert payload["payload"]["authorization"]["from"].lower() == addr.lower()
    assert payload["accepted"]["amount"] == "1000000"


def test_wallet_key_is_refused_inside_an_unignored_git_tree(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with patch.object(identity, "KEY_DIR", repo / "keys"):
        with pytest.raises(AgentWalletError, match="never be committable"):
            agent_wallet.generate("buyer_six")
    assert not (repo / "keys").exists()


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _FakeHttpx:
    """The relay as the MCP server sees it: a 402 until PAYMENT-SIGNATURE arrives."""

    def __init__(self, challenge: dict):
        self.calls: list[dict] = []
        self.challenge = challenge

    def post(self, url, headers=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        if "PAYMENT-SIGNATURE" in (headers or {}):
            return _Resp(200, {"room_id": "r1", "joined": True})
        return _Resp(402, self.challenge)


def test_mcp_join_room_pays_once_with_identity_and_signature(wallet_env):
    agent_wallet.generate("mcp_payer")
    fake = _FakeHttpx(_challenge(250_000))
    with patch.object(mcp_server, "httpx", fake), \
         patch.object(mcp_server, "_dashboard_bearer_headers",
                      return_value={"Authorization": "Bearer id-token"}):
        unpaid = mcp_server.join_room(room_id="r1", agent_id="mcp_payer")
        assert unpaid["error"] == "payment_required" and len(fake.calls) == 1
        paid = mcp_server.join_room(room_id="r1", agent_id="mcp_payer", pay_with_x402=True)
    assert paid["success"] is True, paid
    retry = fake.calls[-1]["headers"]
    assert retry["Authorization"] == "Bearer id-token"
    assert retry["PAYMENT-SIGNATURE"]
    assert len(fake.calls) == 3          # one unpaid probe, then challenge + one paid retry


def test_mcp_join_room_sends_the_mpp_token(wallet_env):
    """P104 also fixed join_room dropping mpp_token on the floor."""
    fake = _FakeHttpx(_challenge(250_000))
    with patch.object(mcp_server, "httpx", fake), \
         patch.object(mcp_server, "_dashboard_bearer_headers",
                      return_value={"Authorization": "Bearer id-token"}):
        mcp_server.join_room(room_id="r1", agent_id="mcp_payer", mpp_token="spt_test_123")
    sent = fake.calls[0]["headers"]
    assert sent["Authorization"] == "Payment spt_test_123"
    assert sent["X-Emporia-Authorization"] == "Bearer id-token"


def test_mcp_over_cap_payment_is_reported_not_signed(wallet_env):
    agent_wallet.generate("mcp_capped")
    fake = _FakeHttpx(_challenge(50_000_000))
    with patch.object(mcp_server, "httpx", fake), \
         patch.object(mcp_server, "_dashboard_bearer_headers", return_value={}):
        out = mcp_server.join_room(room_id="r1", agent_id="mcp_capped", pay_with_x402=True)
    assert out["success"] is False and "per-payment cap" in out["error"]
    assert len(fake.calls) == 1


def test_mcp_settle_promise_pays_the_challenge(wallet_env):
    agent_wallet.generate("funder")
    fake = _FakeHttpx(_challenge(1_000_000))
    with patch.object(mcp_server, "httpx", fake), \
         patch.object(mcp_server, "_dashboard_bearer_headers", return_value={}):
        out = mcp_server.settle_promise(listing_id="b1", agent_id="funder")
    assert out["success"] is True, out
    assert fake.calls[0]["url"].endswith("/bounties/b1/settle-promise")
    assert "PAYMENT-SIGNATURE" in fake.calls[1]["headers"]


def test_mcp_x402_wallet_info_returns_no_key_material(wallet_env):
    addr = agent_wallet.generate("info_agent")
    info = mcp_server.x402_wallet_info(agent_id="info_agent")
    assert info == {"agent_id": "info_agent", "max_payment_cents": 500,
                    "max_daily_cents": 5000, "spent_today_cents": 0,
                    "address": addr, "pay_networks": [NET]}


def test_mcp_fund_session_signs_the_escrow_offer_for_the_sessions_stake(wallet_env):
    seen = {}

    class Relay:
        calls: list[dict] = []

        def post(self, url, headers=None, **kw):
            self.calls.append(dict(headers or {}))
            if "PAYMENT-ESCROW" in (headers or {}):
                return _Resp(200, {"status": "active"})
            return _Resp(402, {"escrow": {"scheme": "emporia-escrow-v1"}})

        def get(self, url, headers=None, **kw):
            return _Resp(200, {"payment_rules": {"stake_per_participant": "1.25"}})

    def fake_sign(aid, offer, *, expected_amount):
        seen.update(aid=aid, offer=offer, amount=expected_amount)
        return "signed-escrow"

    relay = Relay()
    with patch.object(mcp_server, "httpx", relay), \
         patch.object(mcp_server, "_dashboard_bearer_headers", return_value={}), \
         patch.object(agent_wallet, "sign_escrow", fake_sign):
        out = mcp_server.fund_session(session_id="s1", agent_id="buyer", pay_with_x402=True)
    assert seen == {"aid": "buyer", "offer": {"scheme": "emporia-escrow-v1"}, "amount": 1_250_000}
    assert relay.calls[-1]["PAYMENT-ESCROW"] == "signed-escrow"
    assert out.get("status") == "active" or out.get("success") is True, out


def test_the_daily_cap_counts_every_signature_in_24_hours(wallet_env):
    """P115: the per-payment cap alone let a relay ask for the maximum again and
    again. $5 per payment, $12 per day: two pass, the third is refused."""
    with patch.dict(os.environ, {"EMPORIA_X402_MAX_DAILY_CENTS": "1200"}):
        agent_wallet.generate("daily_payer")
        agent_wallet.sign_exact("daily_payer", _challenge(5_000_000))   # $5.00
        agent_wallet.sign_exact("daily_payer", _challenge(5_000_000))
        with pytest.raises(AgentWalletError, match="daily cap"):
            agent_wallet.sign_exact("daily_payer", _challenge(5_000_000))
        agent_wallet.sign_exact("daily_payer", _challenge(1_000_000))             # $1 still fits
        later = __import__("time").time() + 25 * 3600
        with patch("time.time", return_value=later):
            agent_wallet.sign_exact("daily_payer", _challenge(5_000_000))         # a new day
