"""P203: wallet keys and limits in a process of their own."""
import os
import re
from unittest.mock import patch

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from emporia import agent_wallet, identity, signer, signer_client
from tests.test_wallet_setup import BASE, _challenge

AGENT = "signed_agent"


def _challenge_for(amount_cents: int) -> dict:
    body = _challenge(BASE)
    body["x402"]["accepts"][0]["amount"] = str(amount_cents * 10_000)
    return body


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A signer holding AGENT's key and limits; the agent's own process holds neither."""
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    account = Account.create()
    with agent_wallet.scope(agent_wallet.Scope(env={}, key_dir=signer.agent_dir(AGENT) / "keys")):
        path = agent_wallet.wallet_path(AGENT)
    path.parent.mkdir(parents=True)
    identity._write_secret_atomically(path, bytes(account.key))
    signer.save_policy(AGENT, {"EMPORIA_X402_PAY_NETWORKS": BASE, "EMPORIA_X402_MAX_PAYMENT_CENTS": "500",
                               "EMPORIA_X402_MAX_DAILY_CENTS": "2000", "EMPORIA_PAYMENT_APPROVAL_CENTS": "300"})
    token = signer.new_agent_token(AGENT)
    app = TestClient(signer.app, base_url="http://signer")
    monkeypatch.setattr(signer_client.Client, "_http", lambda self: app)
    signer_client._CACHE.clear()
    agent_side = {"EMPORIA_SIGNER_URL": "unix:///x/signer.sock", "EMPORIA_SIGNER_TOKEN": token,
                  "EMPORIA_AGENT_ID": AGENT,
                  # The agent raising its own limits, as one with a terminal could: ignored.
                  "EMPORIA_X402_MAX_PAYMENT_CENTS": "999999", "EMPORIA_PAYMENT_APPROVAL_CENTS": ""}
    with patch.object(identity, "KEY_DIR", tmp_path / "agent-keys"), patch.dict(os.environ, agent_side):
        yield account, app


def test_the_agent_pays_through_the_signer_holding_nothing_itself(held):
    account, _ = held
    assert agent_wallet.address(AGENT) == account.address
    assert agent_wallet.allowed_networks() == [BASE]
    assert agent_wallet.sign_exact(AGENT, _challenge_for(250))
    assert agent_wallet.spent_today_atomic(AGENT) == 250 * 10_000
    assert not (identity.KEY_DIR).exists()                            # no key on the agent's side
    with pytest.raises(agent_wallet.AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact(AGENT, _challenge_for(600))           # its own 999999 does not count


def test_above_the_threshold_only_the_operator_approves_and_only_once(held):
    _, app = held
    with pytest.raises(agent_wallet.AgentWalletError, match="emporia signer approve int_") as e:
        agent_wallet.sign_exact(AGENT, _challenge_for(400))
    intent = re.search(r"int_[0-9a-f]+", str(e.value)).group()
    with pytest.raises(agent_wallet.AgentWalletError, match=intent):  # asking again: the same intent
        agent_wallet.sign_exact(AGENT, _challenge_for(400))
    token = os.environ["EMPORIA_SIGNER_TOKEN"]
    assert app.post(f"/admin/intents/{intent}/approve",
                    headers={"authorization": f"Bearer {token}"}).status_code == 401   # the agent's token: no
    admin = signer_client.Admin("unix:///x", signer.admin_token())
    assert admin.decide(intent, True)["status"] == "approved"
    assert agent_wallet.sign_exact(AGENT, _challenge_for(400))        # paid, once
    with pytest.raises(agent_wallet.AgentWalletError, match="approval"):
        agent_wallet.sign_exact(AGENT, _challenge_for(400))


def test_a_wrong_token_is_refused(held, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_TOKEN", "not-the-token")
    signer_client._CACHE.clear()
    with pytest.raises(agent_wallet.AgentWalletError, match="wrong token"):
        agent_wallet.address(AGENT)


def test_the_signer_says_whether_it_runs_apart_from_the_agents(held):
    _, app = held
    assert app.get("/health").json() == {"service": "emporia-signer", "uid": os.getuid()}
