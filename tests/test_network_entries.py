"""The Robinhood Chain entries: present, labelled, and never a payment network."""
import pytest

from emporia import agent_env, networks

TEST, MAIN = "eip155:46630", "eip155:4663"


def test_the_table_entries():
    t, m = networks.NETWORKS[TEST], networks.NETWORKS[MAIN]
    assert (t.label, t.testnet, t.family, t.payments, t.usdc) == ("Robinhood Chain Testnet", True, "evm", False, "")
    assert (m.label, m.testnet, m.family, m.payments, m.usdc) == ("Robinhood Chain", False, "evm", False, "")
    assert t.rpc == "https://rpc.testnet.chain.robinhood.com"
    assert m.rpc == "https://rpc.mainnet.chain.robinhood.com"
    assert "robinhoodchain.blockscout.com" in m.address_explorer
    assert networks.label(TEST) == "Robinhood Chain Testnet (testnet)"
    assert networks.label(MAIN) == "Robinhood Chain"
    assert networks.is_testnet(TEST) and not networks.is_testnet(MAIN)
    assert networks.resolve("robinhood-testnet") == TEST and networks.resolve("robinhood") == MAIN


def test_existing_networks_still_pay():
    assert all(v.payments for k, v in networks.NETWORKS.items() if k not in (TEST, MAIN))
    assert {TEST, MAIN}.isdisjoint(networks.pay_networks())
    assert "eip155:84532" in networks.pay_networks()


def test_the_default_pay_allowlist_leaves_them_out(monkeypatch):
    from emporia.agent_sdk import EmporiaAgent
    import inspect
    src = inspect.getsource(EmporiaAgent.__init__)
    assert "pay_networks()" in src


def test_no_declared_payment_names_for_them():
    names = set(agent_env.network_names())
    for chain in ("EIP155_46630", "EIP155_4663"):
        for prefix in agent_env.NETWORK_PREFIXES:
            assert prefix + chain not in names
    assert {"EMPORIA_RPC_46630", "EMPORIA_RPC_4663", "EMPORIA_RPC_8453"} <= names


def test_the_wallet_menu_and_signer_policy_refuse_them(monkeypatch):
    from emporia import cli
    seen = {}
    monkeypatch.setattr(cli, "health", lambda: {"x402_networks": [TEST, "eip155:84532"]})
    monkeypatch.setattr(cli, "choose_many", lambda title, opts, ticked: seen.setdefault("opts", opts) and [])
    cli._choose_networks(["evm"], [TEST], mainnet=True)
    keys = [k for k, _ in seen["opts"]]
    assert "eip155:84532" in keys and TEST not in keys and MAIN not in keys


@pytest.mark.parametrize("net", [TEST, MAIN])
def test_the_payer_refuses_any_token_there(net):
    pytest.importorskip("eth_account")
    from emporia import x402_payer
    req = {"scheme": "exact", "network": net, "asset": "0x" + "11" * 20, "payTo": "0x" + "22" * 20,
           "amount": "1000", "maxTimeoutSeconds": 60, "extra": {"name": "X", "version": "1"}}
    with pytest.raises(x402_payer.X402PayerError, match="not the USDC contract"):
        x402_payer.build_payment_payload(req, "0x" + "ab" * 32)


def test_registries_table():
    assert set(networks.ERC8004_REGISTRIES) == {"eip155:8453", "eip155:4663", "eip155:84532", TEST}
    assert networks.ERC8004_REGISTRIES[TEST] == networks.ERC8004_REGISTRIES["eip155:84532"]
