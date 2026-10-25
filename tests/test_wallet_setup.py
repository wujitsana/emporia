"""P160: networks in words, the agent's choice of network, and wallets from the CLI."""
import base64
import json
import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from emporia import agent_wallet, cli, identity, mcp_server, networks
from tests.test_cli import profiles  # noqa: F401  (fixture)

BASE = "eip155:84532"
SOL = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"


def test_networks_have_names_and_short_forms():
    assert networks.label(BASE) == "Base Sepolia (testnet)"
    assert networks.label("eip155:8453") == "Base"
    assert networks.label("eip155:999") == "eip155:999", "an unknown network keeps its id"
    assert networks.resolve_list("base-sepolia, solana-devnet,tempo-moderato") == [BASE, SOL, "eip155:42431"]
    assert networks.family(SOL) == "svm" and networks.family("eip155:1") == "evm"


# -- the agent chooses the network -------------------------------------------

def _challenge(*nets):
    accepts = [{"scheme": "exact", "network": n, "amount": "250000", "payTo": "0x" + "11" * 20,
                "maxTimeoutSeconds": 60, "asset": "0x" + "22" * 20,
                "extra": {"name": "USDC", "version": "2"}} for n in nets]
    return {"x402": {"x402Version": 2, "resource": {"url": "http://relay/r"}, "accepts": accepts}}


@pytest.fixture
def wallet_env(tmp_path):
    env = {"EMPORIA_X402_PAY_NETWORKS": f"eip155:8453,{BASE}", "EMPORIA_X402_MAX_PAYMENT_CENTS": "500"}
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, env):
        agent_wallet.generate("chooser")
        yield


def _paid_network(header):
    return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))["accepted"]["network"]


def test_the_agents_own_order_decides_not_the_relays(wallet_env):
    """The relay lists Base Sepolia first; the agent prefers Base (its allowlist order)."""
    assert _paid_network(agent_wallet.sign_exact("chooser", _challenge(BASE, "eip155:8453"))) == "eip155:8453"
    assert _paid_network(agent_wallet.sign_exact("chooser", _challenge(BASE, "eip155:8453"), network=BASE)) == BASE
    with pytest.raises(agent_wallet.AgentWalletError, match="not one of the networks"):
        agent_wallet.sign_exact("chooser", _challenge(SOL), network=SOL)


def test_a_402_lists_its_options_in_words():
    resp = MagicMock(headers={})
    detail = {**_challenge(BASE, SOL), "tempo": {"network": "eip155:42431"},
              "card": {"offered": False, "reason": "no Stripe account"}}
    options = mcp_server._payment_options(detail, resp)
    assert options[0] == "card: not offered here (no Stripe account)"
    assert "USDC on Base Sepolia (testnet)" in options[1] and f"pay_network='{BASE}'" in options[1]
    assert "Solana devnet" in options[2]
    assert "Tempo Moderato" in options[3] and "pay_with_tempo=True" in options[3]
    escrow = mcp_server._payment_options({"escrow": {"network": BASE}}, MagicMock(headers={}))
    assert escrow == ["USDC escrow hold on Base Sepolia (testnet): pay_with_x402=True"]


# -- the CLI ------------------------------------------------------------------

def test_setup_agent_with_both_wallets_pays_where_the_relay_takes_usdc(profiles, monkeypatch, capsys):
    root, _ = profiles
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: {
        "x402_networks": [BASE, SOL], "tempo_network": "eip155:42431"})
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "delta", "--wallet", "--solana"]) == 0
    keys = root / "delta" / "home" / ".hermes" / "keys"
    for f in ("delta.x402.secp256k1", "delta.x402.ed25519"):
        assert stat.S_IMODE((keys / f).stat().st_mode) == 0o600
    dotenv = (root / "delta" / ".env").read_text()
    assert f"EMPORIA_X402_PAY_NETWORKS={BASE},{SOL},eip155:42431" in dotenv
    out = capsys.readouterr().out
    assert "Solana devnet (testnet)" in out and "faucet.circle.com" in out


def test_wallet_create_never_replaces_a_key_and_limits_take_short_names(profiles, monkeypatch, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "eps", "--wallet"])
    key = root / "eps" / "home" / ".hermes" / "keys" / "eps.x402.secp256k1"
    before = key.read_bytes()
    assert cli.main(["wallet", "create", "eps"]) == 0
    assert key.read_bytes() == before, "an existing key is never replaced"
    assert "already there" in capsys.readouterr().out
    assert cli.main(["wallet", "create", "eps", "--solana"]) == 0
    assert (key.parent / "eps.x402.ed25519").exists()
    assert cli.main(["wallet", "limits", "eps", "--networks", "base-sepolia,solana-devnet",
                     "--max-daily-cents", "300"]) == 0
    dotenv = (root / "eps" / ".env").read_text()
    assert f"EMPORIA_X402_PAY_NETWORKS={BASE},{SOL}" in dotenv and "EMPORIA_X402_MAX_DAILY_CENTS=300" in dotenv
    assert cli.main(["wallet", "limits", "eps", "--networks", "dogecoin"]) == 1
    assert cli.main(["wallet", "show", "eps", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "Solana devnet (testnet)" in out and "PRIV" not in out.upper().replace("PRIVATE", "")


def test_setup_relay_adds_solana_devnet_with_the_facilitators_fee_payer(profiles, monkeypatch):
    _, op = profiles
    keys = op / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    monkeypatch.setattr(identity, "KEY_DIR", keys)
    supported = MagicMock()
    supported.json.return_value = {"kinds": [
        {"scheme": "exact", "network": SOL, "extra": {"feePayer": "FeePayer111"}}]}
    with patch("httpx.get", return_value=supported), \
         patch.dict(os.environ, {"EMPORIA_OPERATOR_PRIVATE_KEY": "", "EMPORIA_OPERATOR_ADDRESS": ""}):
        assert cli.main(["setup", "relay", "--fee-receiver", "0x" + "ab" * 20, "--yes",
                         "--with", "solana-devnet,tempo-moderato"]) == 0
        assert cli.main(["setup", "relay", "--fee-receiver", "0x" + "ab" * 20, "--yes",
                         "--with", "base"]) == 1, "only testnets have presets"
    dotenv = (op / ".env").read_text()
    assert f"EMPORIA_X402_NETWORKS={BASE},{SOL}" in dotenv
    assert "EMPORIA_X402_ASSET_SOLANA_ETWTRABZAYQ6IMFEYKOURU166VU2XQA1=4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU" in dotenv
    assert 'EMPORIA_X402_EXTRA_SOLANA_ETWTRABZAYQ6IMFEYKOURU166VU2XQA1={"feePayer": "FeePayer111"}' in dotenv
    assert "EMPORIA_TEMPO_NETWORK=eip155:42431" in dotenv
