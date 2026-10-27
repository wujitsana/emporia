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
    env = {"EMPORIA_X402_PAY_NETWORKS": f"eip155:8453,{BASE}", "EMPORIA_X402_MAX_PAYMENT_CENTS": "500",
           "EMPORIA_X402_MAINNET": "1", "EMPORIA_ALLOW_HOT_AGENT_WALLET": "1"}   # P203: in-process, accepted
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


def test_mainnet_is_locked_until_unlocked(wallet_env):
    """P177: with mainnet locked, a mainnet network on the list is skipped, and
    a list of only mainnet networks pays nowhere."""
    with patch.dict(os.environ, {"EMPORIA_X402_MAINNET": ""}):
        assert agent_wallet.allowed_networks() == [BASE]
        assert _paid_network(agent_wallet.sign_exact("chooser", _challenge(BASE, "eip155:8453"))) == BASE
        with pytest.raises(agent_wallet.AgentWalletError, match="not one of the networks"):
            agent_wallet.sign_exact("chooser", _challenge("eip155:8453"), network="eip155:8453")
        with patch.dict(os.environ, {"EMPORIA_X402_PAY_NETWORKS": "eip155:8453,eip155:999"}):
            with pytest.raises(agent_wallet.AgentWalletError, match="mainnet .real money. is locked"):
                agent_wallet.allowed_networks()
    from emporia.agent_sdk import EmporiaAgent
    with patch.dict(os.environ, {"EMPORIA_X402_MAINNET": ""}):
        assert EmporiaAgent("http://r", "a", "00", x402_networks=["eip155:8453", BASE])._x402_networks == [BASE]
        assert "eip155:8453" not in EmporiaAgent("http://r", "a", "00")._x402_networks
        assert EmporiaAgent("http://r", "a", "00", x402_networks=["eip155:8453"],
                            allow_mainnet=True)._x402_networks == ["eip155:8453"]


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
        assert cli.main(["setup", "agent", "delta", "--keys", "files", "--wallet", "--solana"]) == 0
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
        cli.main(["setup", "agent", "eps", "--keys", "files", "--wallet"])
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


# -- P161: one recovery phrase, or separate keys ------------------------------

HARDHAT = "test test test test test test test test test test test junk"


def test_one_phrase_gives_the_addresses_wallet_apps_show(tmp_path):
    """MetaMask's path gives the well-known Hardhat address for this phrase;
    the Solana address was checked against an independent SLIP-0010 derivation
    (Phantom's path)."""
    with patch.object(identity, "KEY_DIR", tmp_path):
        made = agent_wallet.generate_from_phrase("ph", HARDHAT)
        assert made == {"evm": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
                        "svm": "oeYf6KAJkLYhBuR8CiGc6L4D4Xtfepr85fuDgA9kq96"}
        assert agent_wallet.address("ph") == made["evm"] and agent_wallet.solana_address("ph") == made["svm"]
        for f in ("ph.x402.phrase", "ph.x402.secp256k1", "ph.x402.ed25519"):
            assert stat.S_IMODE((tmp_path / f).stat().st_mode) == 0o600
        with pytest.raises(agent_wallet.AgentWalletError, match="already exists"):
            agent_wallet.generate_from_phrase("ph")
        with pytest.raises(agent_wallet.AgentWalletError, match="not a valid"):
            agent_wallet.generate_from_phrase("other", "test " * 12)


def test_a_wallet_added_later_comes_from_the_same_phrase(tmp_path):
    with patch.object(identity, "KEY_DIR", tmp_path):
        agent_wallet.generate_from_phrase("half", HARDHAT, families=("evm",))
        assert not agent_wallet.solana_wallet_path("half").exists()
        assert agent_wallet.add_from_phrase("half", "svm") == "oeYf6KAJkLYhBuR8CiGc6L4D4Xtfepr85fuDgA9kq96"
        agent_wallet.generate("sep")
        with pytest.raises(agent_wallet.AgentWalletError, match="no recovery phrase"):
            agent_wallet.add_from_phrase("sep", "svm")


def _typing(monkeypatch, *answers):
    """Run the CLI as if a person typed `answers` at its prompts."""
    it = iter(answers)
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))


def test_setup_agent_asks_and_one_phrase_is_the_recommended_choice(profiles, monkeypatch, capsys):
    root, _ = profiles
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: None)
    # existing profile? no · wallets: 3 (one phrase) · networks: Enter · caps: 5, 20
    # role: 1 (marketplace) · wallets: 3 (one phrase) · networks: Enter · caps: 5, 20 · ask above $2
    _typing(monkeypatch, "1", "3", "", "5", "20", "2")
    monkeypatch.setattr(cli, "_choose_relay", lambda current="": "")   # P177: tested on its own
    shown = []
    monkeypatch.setattr(cli, "_show_phrase", lambda phrase, name: shown.append(phrase) or True)
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "zeta", "--keys", "files"]) == 0
    keys = root / "zeta" / "home" / ".hermes" / "keys"
    # P175: shown once and written down, so only the keys are kept.
    assert {p.name for p in keys.glob("zeta.x402.*")} == {"zeta.x402.secp256k1", "zeta.x402.ed25519"}
    evm, sol = agent_wallet.derive_from_phrase(shown[0])
    assert (keys / "zeta.x402.secp256k1").read_bytes() == evm and (keys / "zeta.x402.ed25519").read_bytes() == sol
    dotenv = (root / "zeta" / ".env").read_text()
    assert "EMPORIA_X402_MAX_PAYMENT_CENTS=500" in dotenv and "EMPORIA_X402_MAX_DAILY_CENTS=2000" in dotenv
    assert "EMPORIA_PAYMENT_APPROVAL_CENTS=200" in dotenv
    out = capsys.readouterr().out
    assert "one recovery phrase" in out and "not kept on this machine" in out
    assert shown[0] not in out and shown[0] not in dotenv


def test_separate_keys_stay_an_option(profiles):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "eta", "--keys", "files", "--wallet", "--solana", "--separate-keys"]) == 0
    keys = root / "eta" / "home" / ".hermes" / "keys"
    assert {p.name for p in keys.glob("eta.x402.*")} == {"eta.x402.secp256k1", "eta.x402.ed25519"}


def test_the_wallet_menu_picks_the_agent_and_the_action(profiles, monkeypatch, capsys):
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "theta", "--keys", "files"])
    _typing(monkeypatch, "1", "1")          # agent 1 (theta) · show
    assert cli.main(["wallet"]) == 0
    assert "Wallets for theta" in capsys.readouterr().out


def test_without_a_terminal_emporia_alone_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.main([]) == 2
    assert "doctor" in capsys.readouterr().out


# -- an agent that already has a key ------------------------------------------

def test_an_existing_key_comes_in_from_any_common_export(tmp_path):
    from eth_account import Account
    from solders.keypair import Keypair
    evm, sol = Account.create(), Keypair()
    with patch.object(identity, "KEY_DIR", tmp_path):
        assert agent_wallet.import_key("a", "evm", evm.key.hex()) == evm.address
        assert agent_wallet.import_key("b", "svm", str(sol)) == str(sol.pubkey()), "Phantom's base58 export"
        assert agent_wallet.import_key("c", "svm", json.dumps(list(bytes(sol)))) == str(sol.pubkey()), \
            "solana-keygen's JSON file"
        with pytest.raises(agent_wallet.AgentWalletError, match="already exists"):
            agent_wallet.import_key("a", "evm", Account.create().key.hex())
        assert agent_wallet.address("a") == evm.address, "never replaced"
        with pytest.raises(agent_wallet.AgentWalletError, match="64 hex"):
            agent_wallet.import_key("d", "evm", "not a key")
        bad = bytearray(bytes(sol)); bad[40] ^= 1
        with pytest.raises(Exception):
            agent_wallet.import_key("e", "svm", json.dumps(list(bad))), "a mismatched public half is refused"


def test_wallet_import_reads_a_key_file_and_never_prints_it(profiles, tmp_path, capsys):
    from solders.keypair import Keypair
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "iota", "--keys", "files"])
    sol = Keypair()
    keyfile = tmp_path / "id.json"
    keyfile.write_text(json.dumps(list(bytes(sol))))
    assert cli.main(["wallet", "import", "iota", "--solana", "--key-file", str(keyfile)]) == 0
    out = capsys.readouterr().out
    assert str(sol.pubkey()) in out and str(sol) not in out
    assert (root / "iota" / "home" / ".hermes" / "keys" / "iota.x402.ed25519").exists()
    assert cli.main(["wallet", "import", "iota", "--solana", "--key-file", str(keyfile)]) == 1, \
        "a second import does not replace the first"


# -- P162: a key that stays in the environment or a password manager ---------

def test_a_linked_key_signs_without_ever_being_written(tmp_path, monkeypatch):
    import sys
    from eth_account import Account
    acct = Account.create()
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(agent_wallet, "_SOURCE_CACHE", {})
    monkeypatch.setenv("MY_WALLET_KEY", acct.key.hex())
    monkeypatch.setenv("EMPORIA_X402_EVM_KEY_SOURCE", "env:MY_WALLET_KEY")
    monkeypatch.setenv("EMPORIA_X402_PAY_NETWORKS", BASE)
    assert agent_wallet.address("linked") == acct.address
    assert agent_wallet._families("linked") == ("evm",)
    agent_wallet.sign_exact("linked", _challenge(BASE))
    assert not [f for f in tmp_path.glob("*.x402.*") if not f.name.endswith(".spend.json")], \
        "no key on disk (only the daily-spend ledger)"
    # A password manager is any command that prints the secret (here, a stand-in for `op read`).
    monkeypatch.setattr(agent_wallet, "_SOURCE_CACHE", {})
    monkeypatch.delenv("EMPORIA_X402_EVM_KEY_SOURCE")
    monkeypatch.setenv("EMPORIA_X402_PHRASE_SOURCE", f"cmd:{sys.executable} -c 'print(\"{HARDHAT}\")'")
    assert agent_wallet.address("linked") == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    assert agent_wallet.solana_address("linked") == "oeYf6KAJkLYhBuR8CiGc6L4D4Xtfepr85fuDgA9kq96"
    monkeypatch.setattr(agent_wallet, "_SOURCE_CACHE", {})
    monkeypatch.setenv("EMPORIA_X402_PHRASE_SOURCE", "cmd:definitely-not-a-password-manager read x")
    with pytest.raises(agent_wallet.AgentWalletError, match="not installed"):
        agent_wallet.address("linked")


def test_wallet_link_stores_the_reference_not_the_key(profiles, monkeypatch, capsys):
    from eth_account import Account
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "kappa", "--keys", "files"])
    acct = Account.create()
    with (root / "kappa" / ".env").open("a") as f:      # in the agent's own .env, where it will look
        f.write(f"KAPPA_KEY={acct.key.hex()}\n")
    assert cli.main(["wallet", "import", "kappa", "--link", "env:KAPPA_KEY"]) == 0
    dotenv = (root / "kappa" / ".env").read_text()
    assert "EMPORIA_X402_EVM_KEY_SOURCE=env:KAPPA_KEY" in dotenv
    assert dotenv.count(acct.key.hex()) == 1, "linked where it was, not copied"
    out = capsys.readouterr().out
    assert acct.address in out and acct.key.hex() not in out
    assert not list((root / "kappa").rglob("*.x402.secp256k1")), "linked, not copied"
    assert cli.main(["wallet", "import", "kappa", "--link", "env:NOT_SET_ANYWHERE"]) == 1
    # Copying from an environment variable is the other choice: a key file, made once.
    monkeypatch.setenv("KAPPA_SOL", json.dumps(list(bytes(__import__("solders.keypair", fromlist=["Keypair"]).Keypair()))))
    assert cli.main(["wallet", "import", "kappa", "--solana", "--from-env", "KAPPA_SOL"]) == 0
    assert list((root / "kappa").rglob("*.x402.ed25519"))


# -- P164: identity and encryption keys in a password manager ------------------

def test_an_identity_key_links_only_to_the_same_key(profiles, monkeypatch, capsys):
    import sys
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "lam", "--keys", "files"])
    keys = root / "lam" / "home" / ".hermes" / "keys"
    with patch.object(identity, "KEY_DIR", keys):
        identity.generate_or_load_keypair("lam")
    key_file = keys / "lam.priv"
    # Stand-ins for `op read …`: commands that print a key.
    reader = keys.parent / "print_key.py"
    reader.write_text(f"print(open({str(key_file)!r}, 'rb').read().hex())")
    wrong = keys.parent / "print_other.py"
    wrong.write_text(f"print({'11' * 32!r})")
    good, other = f"cmd:{sys.executable} {reader}", f"cmd:{sys.executable} {wrong}"
    assert cli.main(["identity", "link", "lam", "--key", "identity", "--source", other]) == 1
    assert "different identity key" in capsys.readouterr().out
    assert cli.main(["identity", "link", "lam", "--key", "identity", "--source", good]) == 0
    assert "EMPORIA_IDENTITY_KEY_SOURCE=cmd:" in (root / "lam" / ".env").read_text()
    # The agent now signs with the linked key, even with the file gone.
    want = identity.ed25519.Ed25519PrivateKey.from_private_bytes(key_file.read_bytes())
    monkeypatch.setattr(identity, "_LINKED", {})
    monkeypatch.setenv("EMPORIA_IDENTITY_KEY_SOURCE", f"env:LAM_ID")
    monkeypatch.setenv("LAM_ID", key_file.read_bytes().hex())
    key_file.rename(keys / "moved.away")
    priv, pub = identity.generate_or_load_keypair("lam")
    assert pub == want.public_key().public_bytes_raw()
    assert not key_file.exists(), "a linked key never writes a file"
    assert cli.main(["identity", "show", "lam"]) == 0
    assert "linked to" in capsys.readouterr().out


def test_the_dashboard_finds_an_agent_whose_key_is_linked(tmp_path, monkeypatch):
    from emporia.console import agents as console_agents
    keys = tmp_path / "profiles" / "mu" / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    raw = os.urandom(32)
    (tmp_path / "profiles" / "mu" / ".env").write_text(f"EMPORIA_IDENTITY_KEY_SOURCE=env:MU_KEY\nMU_KEY={raw.hex()}\n")
    found = console_agents.discover([keys])
    assert "mu" in found and found["mu"].identity_source == "env:MU_KEY"
    key = console_agents.load_identity_key(found["mu"])
    assert key.private_bytes_raw() == raw


# -- P165: keys in the profile .env -------------------------------------------

def _env_of(profile):
    return dict(line.split("=", 1) for line in (profile / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def test_a_new_agent_keeps_its_keys_in_its_env_by_default(profiles, monkeypatch, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "nu", "--wallet", "--solana"]) == 0
    env = _env_of(root / "nu")
    assert env["EMPORIA_KEY_STORAGE"] == "env"
    assert env["EMPORIA_X402_PHRASE_SOURCE"] == "env:EMPORIA_X402_PHRASE_SECRET"
    assert len(env["EMPORIA_X402_PHRASE_SECRET"].split()) == 12
    assert not list((root / "nu").rglob("*.x402.*")), "no wallet key files"
    out = capsys.readouterr().out
    assert env["EMPORIA_X402_PHRASE_SECRET"] not in out
    # Its identity key, made at first start, goes there too.
    keys = root / "nu" / "home" / ".hermes" / "keys"
    with patch.object(identity, "KEY_DIR", keys), \
         patch.dict(os.environ, {"EMPORIA_KEY_STORAGE": "env", "EMPORIA_DOTENV": str(root / "nu" / ".env")}):
        monkeypatch.setattr(identity, "_LINKED", {})
        _, pub = identity.generate_or_load_keypair("nu")
        assert not (keys / "nu.priv").exists()
        assert identity.generate_or_load_keypair("nu")[1] == pub, "the same key on the next start"
    env = _env_of(root / "nu")
    assert env["EMPORIA_IDENTITY_KEY_SOURCE"] == "env:EMPORIA_IDENTITY_PRIVATE_KEY"


def test_existing_key_files_move_into_the_env_with_the_same_addresses(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "xi", "--keys", "files", "--wallet", "--solana"])
    keys = root / "xi" / "home" / ".hermes" / "keys"
    with patch.object(identity, "KEY_DIR", keys):
        identity.generate_or_load_keypair("xi")
        before = (agent_wallet.address("xi"), agent_wallet.solana_address("xi"))
        pub_before = identity.read_public_key_hex("xi")
    assert cli.main(["identity", "store", "xi", "--to", "env", "--remove-files"]) == 0
    assert cli.main(["wallet", "store", "xi", "--remove-files"]) == 0
    assert not list(keys.glob("xi.*priv"))
    # P175: the keys move; the phrase is not moved but backed up and removed.
    assert [p.name for p in keys.glob("xi.x402.*")] == ["xi.x402.phrase"]
    assert "emporia wallet backup xi" in capsys.readouterr().out
    env = _env_of(root / "xi")
    with patch.object(identity, "KEY_DIR", keys), patch.dict(os.environ, env):
        agent_wallet._SOURCE_CACHE.clear()
        identity._LINKED.clear()
        assert (agent_wallet.address("xi"), agent_wallet.solana_address("xi")) == before
        assert identity.read_public_key_hex("xi") == pub_before
    assert list((root / "xi").glob(".env-backups/*/.env")), "backups under a protected name"
    assert not list((root / "xi").glob(".env.bak*"))


def test_an_agent_without_a_wallet_is_a_full_agent_that_cannot_pay(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "omi"]) == 0   # no wallet chosen
    assert "EMPORIA_X402_PAY_NETWORKS" not in (root / "omi" / ".env").read_text()
    assert cli.main(["wallet", "show", "omi", "--offline"]) == 0
    assert "none" in capsys.readouterr().out


# -- P168: roles ----------------------------------------------------------------

def test_a_marketplace_agent_has_no_tool_that_reaches_its_keys(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "pi", "--role", "marketplace", "--wallet"]) == 0
    off = cli._disabled_toolsets(root / "pi")
    assert set(cli.MARKETPLACE_OFF) <= set(off)
    out = capsys.readouterr().out
    assert "Summary for pi" in out and "Role        marketplace" in out


def test_a_developer_agent_gets_no_wallet_even_if_asked(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "rho", "--role", "developer", "--wallet"]) == 0
    assert "holds no wallet" in capsys.readouterr().out
    assert not cli._has_wallet(root / "rho", "rho")
    assert cli._disabled_toolsets(root / "rho") == []


def test_both_on_local_is_refused_without_a_yes_and_doctor_fails_it(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "sig", "--role", "marketplace", "--wallet", "--keys", "files"])
    assert cli.main(["role", "sig", "both"]) == 1, "no terminal to say yes, so nothing changed"
    assert cli.profile_role(root / "sig", True) == "marketplace"
    assert cli.main(["role", "sig", "both", "--yes"]) == 0
    assert cli.profile_role(root / "sig", True) == "both"
    assert cli._role_warnings("both", True, "local")
    assert cli.main(["role", "sig", "marketplace"]) == 0
    assert cli.profile_role(root / "sig", True) == "marketplace"


def test_a_role_keeps_toolsets_the_operator_turned_off(profiles):
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "tau", "--role", "developer"])
    cfg = cli.installer().load_yaml(root / "tau" / "config.yaml")
    cfg.setdefault("agent", {})["disabled_toolsets"] = ["tts"]
    cli.installer().save_yaml(root / "tau" / "config.yaml", cfg)
    cli.apply_role(root / "tau", "marketplace")
    assert "tts" in cli._disabled_toolsets(root / "tau")
    cli.apply_role(root / "tau", "developer")
    assert cli._disabled_toolsets(root / "tau") == ["tts"]


# -- P169: bring your own keys ----------------------------------------------------

def test_keys_already_on_the_machine_are_found_and_used(profiles, monkeypatch, tmp_path, capsys):
    from eth_account import Account
    from solders.keypair import Keypair
    root, _ = profiles
    evm, sol = Account.create(), Keypair()
    home = tmp_path / "home"
    (home / ".config" / "solana").mkdir(parents=True)
    (home / ".config" / "solana" / "id.json").write_text(json.dumps(list(bytes(sol))))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EVM_PRIVATE_KEY", evm.key.hex())
    found = cli.byo_candidates(root / "nothing-here")
    assert {(f["kind"], f["address"]) for f in found} == {("evm", evm.address), ("svm", str(sol.pubkey()))}
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "upsi", "--role", "marketplace", "--use-found-keys", "link"]) == 0
    out = capsys.readouterr().out
    assert evm.address in out and str(sol.pubkey()) in out
    assert evm.key.hex() not in out, "never the key"
    env = _env_of(root / "upsi")
    assert env.get("EMPORIA_X402_EVM_PRIVATE_KEY"), "only this shell had it, so it is copied"
    assert env.get("EMPORIA_X402_SOLANA_PRIVATE_KEY"), "the key file is copied (into the .env by default)"


def test_a_key_in_the_agents_own_env_is_linked_where_it_is(profiles, monkeypatch, tmp_path):
    from eth_account import Account
    root, _ = profiles
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "psi", "--role", "developer"])
    evm = Account.create()
    with (root / "psi" / ".env").open("a") as f:
        f.write(f"EVM_PRIVATE_KEY={evm.key.hex()}\n")
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "psi", "--role", "marketplace", "--use-found-keys", "link"]) == 0
    assert _env_of(root / "psi")["EMPORIA_X402_EVM_KEY_SOURCE"] == "env:EVM_PRIVATE_KEY"


def test_a_string_that_is_not_a_key_is_not_offered(profiles, monkeypatch, tmp_path):
    root, _ = profiles
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PRIVATE_KEY", "not-a-key")
    monkeypatch.setenv("MNEMONIC", "hello world")
    assert cli.byo_candidates(root / "x") == []


# -- P169: Stripe and the overview ------------------------------------------------

def test_setup_stripe_writes_the_key_from_an_env_var_and_never_prints_it(profiles, monkeypatch, capsys):
    _, op = profiles
    monkeypatch.setenv("MY_STRIPE", "sk_test_" + "k" * 24)
    assert cli.main(["setup", "stripe", "--stripe-key-env", "MY_STRIPE",
                     "--stripe-profile-id", "profile_test_x"]) == 0
    env = _env_of(op)
    assert env["STRIPE_SECRET_KEY"] == "sk_test_" + "k" * 24 and env["STRIPE_PROFILE_ID"] == "profile_test_x"
    out = capsys.readouterr().out
    assert "k" * 24 not in out and "Accounts v1" in out
    monkeypatch.setenv("NOT_STRIPE", "hello")
    assert cli.main(["setup", "stripe", "--stripe-key-env", "NOT_STRIPE"]) == 0
    assert "not a Stripe secret key" in capsys.readouterr().out
    assert _env_of(op)["STRIPE_SECRET_KEY"] == "sk_test_" + "k" * 24, "unchanged"


def test_the_overview_lists_every_component_and_reads_only(profiles, capsys):
    root, op = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "phi", "--role", "marketplace", "--wallet"])
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    for part in ("Relay", "USDC payments", "Escrow holds", "Cards (Stripe)", "Federation", "Risky switches",
                 "phi", "marketplace", "Emporia tab", "AWS KMS signer", "Key recovery"):
        assert part in out, part
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after, "the overview changes nothing"


def test_a_key_only_in_this_shell_is_copied_not_linked(profiles, monkeypatch, tmp_path, capsys):
    from eth_account import Account
    root, _ = profiles
    evm = Account.create()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("EVM_PRIVATE_KEY", evm.key.hex())    # the shell's, not the agent's .env
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "chi", "--role", "marketplace", "--use-found-keys", "link"]) == 0
    assert "copying it instead of linking" in capsys.readouterr().out
    env = _env_of(root / "chi")
    assert "EMPORIA_X402_EVM_KEY_SOURCE" in env and env["EMPORIA_X402_EVM_KEY_SOURCE"] != "env:EVM_PRIVATE_KEY"


# -- P172: a re-run never quietly changes what an agent is --------------------------

def test_rerunning_setup_keeps_the_role_and_key_storage(profiles):
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "dev1", "--role", "developer", "--keys", "files"])
        assert cli.profile_role(root / "dev1", False) == "developer"
        # e.g. what doctor suggests to update its plugin — no terminal, no flags
        assert cli.main(["setup", "agent", "dev1"]) == 0
    assert cli.profile_role(root / "dev1", False) == "developer", "its terminal is not taken away"
    assert "EMPORIA_KEY_STORAGE" not in (root / "dev1" / ".env").read_text(), "its key storage unchanged"


# -- P173: review fixes ------------------------------------------------------------

def test_a_phrase_in_the_env_is_never_overwritten(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setenv("EMPORIA_KEY_STORAGE", "env")
    monkeypatch.setenv("EMPORIA_DOTENV", str(env))
    monkeypatch.setattr(agent_wallet, "_SOURCE_CACHE", {})
    with patch.dict(os.environ):     # saving a key also sets it in this process; undo that after
        first = agent_wallet.generate_from_phrase("keep", HARDHAT)
        with pytest.raises(agent_wallet.AgentWalletError, match="not replaced"):
            agent_wallet.generate_from_phrase("keep")            # a second phrase, e.g. an import
        assert HARDHAT in env.read_text(), "the only copy is still there"
        assert agent_wallet.address("keep") == first["evm"]


def test_a_wallet_can_be_added_from_a_linked_phrase(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(agent_wallet, "_SOURCE_CACHE", {})
    monkeypatch.setenv("MY_PHRASE", HARDHAT)
    monkeypatch.setenv("EMPORIA_X402_PHRASE_SOURCE", "env:MY_PHRASE")
    assert agent_wallet.add_from_phrase("lnk", "svm") == "oeYf6KAJkLYhBuR8CiGc6L4D4Xtfepr85fuDgA9kq96"


def test_reading_for_a_profile_never_uses_the_callers_key(monkeypatch):
    from emporia import key_source
    monkeypatch.setenv("EMPORIA_IDENTITY_PRIVATE_KEY", "11" * 32)       # the caller's own
    alpha = {"EMPORIA_IDENTITY_PRIVATE_KEY": "22" * 32}
    assert key_source.key32("env:EMPORIA_IDENTITY_PRIVATE_KEY", alpha) == bytes.fromhex("22" * 32)
    with pytest.raises(key_source.KeySourceError):
        key_source.key32("env:EMPORIA_IDENTITY_PRIVATE_KEY", {})        # alpha has none: no fallback
    assert key_source.key32("env:EMPORIA_IDENTITY_PRIVATE_KEY") == bytes.fromhex("11" * 32), "the agent itself"


# -- P175: the recovery phrase is shown once and not kept ---------------------

def test_the_phrase_is_shown_once_and_three_words_confirm_it(monkeypatch, capsys):
    phrase = agent_wallet.new_phrase()
    words = phrase.split()
    _typing(monkeypatch, "", "", "")   # back up now: yes · Enter to show · Enter once written down
    monkeypatch.setattr("getpass.getpass", lambda q: words[int(q.split("#")[1].split()[0]) - 1])
    assert cli._show_phrase(phrase, "kappa")
    out = capsys.readouterr().out
    assert "emporia wallet backup kappa" in out, "says how to back it up later"
    assert cli.PHRASE_BEGIN in out and "\x1b[?1049h" in out and "\x1b[?1049l" in out, "on the alternate screen"
    _typing(monkeypatch, "", "", "", "n")              # wrong words · don't show again
    monkeypatch.setattr("getpass.getpass", lambda q: "wrong")
    assert not cli._show_phrase(phrase, "kappa")
    capsys.readouterr()
    _typing(monkeypatch, "n")                          # not now
    assert not cli._show_phrase(phrase, "kappa")
    later = capsys.readouterr().out
    assert cli.PHRASE_BEGIN not in later and "wallet backup kappa" in later


def test_no_terminal_or_an_agents_terminal_never_shows_it(monkeypatch):
    monkeypatch.setattr(cli, "_TTY", False)
    assert not cli._persons_terminal()
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    assert not cli._persons_terminal()


def test_a_phrase_nobody_saw_is_kept_until_backed_up(profiles, monkeypatch, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "pi", "--keys", "files", "--wallet", "--solana"]) == 0
    keys = root / "pi" / "home" / ".hermes" / "keys"
    assert (keys / "pi.x402.phrase").exists(), "no terminal: nobody has written it down yet"
    phrase = (keys / "pi.x402.phrase").read_text().strip()
    assert "emporia wallet backup pi" in capsys.readouterr().out
    before = {f: cli._wallet_address(root / "pi", "pi", f == "svm") for f in ("evm", "svm")}
    assert cli.main(["wallet", "backup", "pi"]) == 1, "refused without a person's terminal"
    assert (keys / "pi.x402.phrase").exists()
    _typing(monkeypatch)
    monkeypatch.setattr(cli, "_show_phrase", lambda p, name: p == phrase)
    assert cli.main(["wallet", "backup", "pi"]) == 0
    assert not (keys / "pi.x402.phrase").exists()
    assert {f: cli._wallet_address(root / "pi", "pi", f == "svm") for f in ("evm", "svm")} == before
    assert cli._phrase_state(root / "pi", "pi") == ""
    assert "keeps no recovery phrase" in (cli.main(["wallet", "backup", "pi"]) == 0 and capsys.readouterr().out)


def test_backup_of_a_phrase_kept_in_the_env_gives_its_wallets_their_keys(profiles, monkeypatch, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "rho", "--wallet", "--solana"]) == 0
    profile = root / "rho"
    env = _env_of(profile)
    phrase = env["EMPORIA_X402_PHRASE_SECRET"]
    # As P165 kept it: the phrase alone, both wallets read from it.
    from emporia.key_source import unset_env_lines
    with patch.dict(os.environ):
        unset_env_lines(profile / ".env", ["EMPORIA_X402_EVM_PRIVATE_KEY", "EMPORIA_X402_EVM_KEY_SOURCE",
                                           "EMPORIA_X402_SOLANA_PRIVATE_KEY", "EMPORIA_X402_SOLANA_KEY_SOURCE"])
    cli.merge_env_file(profile / ".env", {"EMPORIA_X402_MAX_PAYMENT_CENTS": "700"})   # leaves a backup
    assert any(phrase in b.read_text() for b in profile.glob(".env-backups/*/.env"))
    before = {f: cli._wallet_address(profile, "rho", f == "svm") for f in ("evm", "svm")}
    assert all(before.values())
    _typing(monkeypatch)
    monkeypatch.setattr(cli, "_show_phrase", lambda p, name: True)
    with patch.dict(os.environ):
        assert cli.main(["wallet", "backup", "rho"]) == 0
    env = _env_of(profile)
    assert "EMPORIA_X402_PHRASE_SECRET" not in env and "EMPORIA_X402_PHRASE_SOURCE" not in env
    assert env["EMPORIA_X402_EVM_PRIVATE_KEY"].startswith("0x")
    assert {f: cli._wallet_address(profile, "rho", f == "svm") for f in ("evm", "svm")} == before
    assert not any(phrase in b.read_text() for b in profile.glob(".env-backups/*/.env")), "no copy left behind"
    assert phrase not in capsys.readouterr().out


def test_a_solana_key_kept_in_the_env_reads_back(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("")
    env = {"EMPORIA_KEY_STORAGE": "env", "EMPORIA_DOTENV": str(dotenv)}
    with patch.object(identity, "KEY_DIR", tmp_path / "keys"), patch.dict(os.environ, env):
        agent_wallet._SOURCE_CACHE.clear()
        made = agent_wallet.generate_solana("sigma")
        agent_wallet._SOURCE_CACHE.clear()
        assert agent_wallet.solana_address("sigma") == made
    # P165 stored it as hex; that still reads.
    assert agent_wallet._parse_secret("svm", (b"\x07" * 32).hex()) == b"\x07" * 32


def test_the_other_wallet_joins_a_phrase_typed_again(tmp_path):
    phrase = agent_wallet.new_phrase()
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ):
        made = agent_wallet.generate_from_phrase("tau", phrase, ("evm",), keep_phrase=False)
        assert not agent_wallet.phrase_path("tau").exists()
        with pytest.raises(agent_wallet.AgentWalletError, match="type it again"):
            agent_wallet.add_from_phrase("tau", "svm")
        with pytest.raises(agent_wallet.AgentWalletError, match="does not give the wallet"):
            agent_wallet.add_from_phrase("tau", "svm", agent_wallet.new_phrase())
        sol = agent_wallet.add_from_phrase("tau", "svm", phrase)
        assert agent_wallet.address("tau") == made["evm"]
        from emporia import x402_svm
        assert sol == agent_wallet.solana_address("tau") == x402_svm.address_of(
            agent_wallet.derive_from_phrase(phrase)[1])


def test_doctor_says_a_phrase_is_not_backed_up(profiles, capsys, monkeypatch):
    root, _ = profiles
    with patch.dict(os.environ):
        cli.main(["setup", "agent", "upsilon", "--keys", "files", "--wallet", "--solana"])
    from emporia import nous_token
    monkeypatch.setattr(nous_token, "from_hermes", lambda profile=None: None)
    capsys.readouterr()
    cli.main(["doctor", "--profiles-root", str(root)])
    assert "emporia wallet backup upsilon" in capsys.readouterr().out


# -- P177: Solana only, mainnet locked, which relay ---------------------------

def test_setup_offers_a_solana_only_wallet(profiles, monkeypatch):
    root, _ = profiles
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    monkeypatch.setattr(cli, "_choose_relay", lambda current="": "")
    keys = [k for k, _ in cli.WALLET_MODES]
    assert keys[:4] == ["none", "evm", "phrase", "separate"] and "solana" in keys, "earlier answers keep their numbers"
    _typing(monkeypatch, "1", str(keys.index("solana") + 1), "", "5", "20", "2")
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "phi", "--keys", "files"]) == 0
    keys_dir = root / "phi" / "home" / ".hermes" / "keys"
    assert [p.name for p in keys_dir.glob("phi.x402.*")] == ["phi.x402.ed25519"]
    assert "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1" in _env_of(root / "phi")["EMPORIA_X402_PAY_NETWORKS"]


def test_mainnet_networks_are_refused_while_locked(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "chi", "--wallet", "--pay-networks", "base"]) == 1
        assert "mainnet (real money) is locked" in capsys.readouterr().out
        assert cli.main(["setup", "agent", "chi", "--wallet"]) == 0
        assert cli.main(["wallet", "limits", "chi", "--networks", "base,base-sepolia"]) == 1
        assert cli.main(["wallet", "mainnet", "chi"]) == 1, "only a person's terminal unlocks it"
        assert "own terminal" in capsys.readouterr().out
        cli.merge_env_file(root / "chi" / ".env", {"EMPORIA_X402_MAINNET": "1"})
        assert cli.main(["wallet", "limits", "chi", "--networks", "base,base-sepolia"]) == 0
        assert cli.main(["wallet", "mainnet", "chi", "--lock"]) == 0
    env = _env_of(root / "chi")
    assert env["EMPORIA_X402_MAINNET"] == "0" and env["EMPORIA_X402_PAY_NETWORKS"] == "eip155:84532"


def test_unlocking_mainnet_takes_a_warning_and_the_agents_name(profiles, monkeypatch, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "psi", "--wallet"]) == 0
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.delenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", raising=False)
    assert cli.main(["wallet", "mainnet", "psi"]) == 1                # P203: only from behind the signer
    assert "emporia signer adopt psi" in capsys.readouterr().out
    monkeypatch.setenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", "1")         # …or with the risk accepted
    _typing(monkeypatch, "2", "nope")                  # ask above $2 · wrong name
    assert cli.main(["wallet", "mainnet", "psi"]) == 1
    assert "Mainnet means real money" in capsys.readouterr().out
    assert _env_of(root / "psi").get("EMPORIA_X402_MAINNET", "") != "1"
    _typing(monkeypatch, "psi", "")                    # the name · networks: as ticked
    assert cli.main(["wallet", "mainnet", "psi"]) == 0
    assert _env_of(root / "psi")["EMPORIA_X402_MAINNET"] == "1"


def test_choose_relay_offers_the_one_in_use_then_local_remote_later(profiles, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_relays_in_use", lambda: {"https://relay.example": ["alpha"]})
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="":
                        {"version": "1", "x402_networks": ["eip155:84532"]} if url == "https://relay.example" else None)
    _typing(monkeypatch, "")                           # the default: the one in use
    assert cli._choose_relay() == "https://relay.example"
    out = capsys.readouterr().out
    assert "used by alpha" in out and "Base Sepolia (testnet)" in out and "Run a relay on this machine" in out
    _typing(monkeypatch, "3", "http://203.0.113.9:8088", "n", "https://relay.example")   # remote · plain http refused
    assert cli._choose_relay() == "https://relay.example"
    out = capsys.readouterr().out
    assert "Not https" in out and "SECURITY.md" in out
    _typing(monkeypatch, "6")      # its current one, the one in use, local, remote, host · later
    assert cli._choose_relay("https://old.example") == "https://old.example"
    assert "its current relay" in capsys.readouterr().out


def test_relay_use_moves_agents_and_keeps_their_keys(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "omega"]) == 0
    assert cli.main(["relay", "use", "https://relay.example", "omega"]) == 0
    cfg = cli.installer().load_yaml(root / "omega" / "config.yaml")
    assert cfg["mcp_servers"]["emporia"]["env"]["EMPORIA_RELAY_URL"] == "https://relay.example"
    assert _env_of(root / "omega")["EMPORIA_RELAY_URL"] == "https://relay.example"
    assert cli.main(["relay", "use", "https://relay.example", "nobody"]) == 1


def test_a_local_relay_starts_with_its_payments_or_keeps_the_current_one(profiles, monkeypatch, capsys):
    started, payments = [], []
    monkeypatch.setattr(cli, "_relays_in_use", lambda: {})
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    monkeypatch.setattr(cli, "cmd_relay", lambda a: started.append(a.action) or 0)
    monkeypatch.setattr(cli, "cmd_setup_relay", lambda a: payments.append(a.preset) or 0)
    access = []
    monkeypatch.setattr(cli, "cmd_relay_access", lambda a: access.append(a.url) or 0)
    with patch.dict(os.environ):
        _typing(monkeypatch, "", "", "")               # local (the default) · closed · payments yes
        assert cli._choose_relay() == cli.LOCAL_RELAY
        assert started == ["start"] and payments == ["base-sepolia"] and access == ["closed"]
        assert "emporia relay host" in capsys.readouterr().out
        monkeypatch.setattr(cli, "cmd_relay", lambda a: 1)
        _typing(monkeypatch, "", "3", "n")             # open · it does not start: keep the current one
        assert cli._choose_relay("https://old.example") == "https://old.example"


def test_the_relay_listens_beyond_this_machine_only_in_a_container(monkeypatch):
    from emporia import relay_server
    monkeypatch.setattr(relay_server, "in_container", lambda: False)
    assert relay_server.bind_refusal("127.0.0.1") == "" and relay_server.bind_refusal("") == ""
    assert "emporia relay host" in relay_server.bind_refusal("0.0.0.0")
    monkeypatch.setattr(relay_server, "in_container", lambda: True)
    assert relay_server.bind_refusal("0.0.0.0") == ""


def test_relay_host_writes_its_own_folder_and_refuses_an_agent(profiles, monkeypatch, capsys, tmp_path):
    import yaml
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "keeper"]) == 0
    monkeypatch.setattr(cli, "this_profile", lambda: root / "keeper")
    monkeypatch.setattr("shutil.which", lambda _: None)            # no Docker here
    folder = tmp_path / "emporia-relay"
    _typing(monkeypatch, "", "y", "0x" + "ab" * 20)              # Nous · payments · fee address
    assert cli.main(["relay", "host", "--run", "docker", "--dir", str(folder), "--domain", "relay.example.com",
                     "--admin", "keeper", "--access", "open"]) == 0
    out = capsys.readouterr().out
    assert "On the host" in out and "docker compose --profile admin up -d" in out
    compose = yaml.safe_load((folder / "compose.yaml").read_text())
    assert compose["services"]["relay"]["ports"] == ["127.0.0.1:8089:8088"]
    assert compose["services"]["https"]["ports"] == ["80:80", "443:443"]
    assert compose["services"]["admin"]["volumes"][0].endswith(":/opt/data")
    assert not compose["services"]["relay"]["build"].startswith("/")          # host paths differ
    assert "reverse_proxy relay:8088" in (folder / "Caddyfile").read_text()
    env = _env_of(folder)
    assert env["EMPORIA_RELAY_URL"] == "https://relay.example.com" and env["EMPORIA_REQUIRE_NOUS"] == "1"
    assert env["EMPORIA_ESCROW_FEE_RECEIVER"] == "0x" + "ab" * 20
    assert env["EMPORIA_AGENT_ID"] and oct((folder / ".env").stat().st_mode)[-3:] == "600"
    assert _env_of(root / "keeper")["EMPORIA_RELAY_URL"] == "http://relay:8088"
    assert "EMPORIA_ESCROW_FEE_RECEIVER" not in _env_of(root / "keeper")      # the relay's, in its own folder
    (folder / "compose.yaml").write_text("services: {}\n")            # someone else's: not replaced
    _typing(monkeypatch)
    assert cli.main(["relay", "host", "--run", "docker", "--dir", str(folder), "--domain", "relay.example.com",
                     "--admin", "", "--access", "open"]) == 1
    monkeypatch.setenv("HERMES_SESSION_ID", "x")                          # an agent's terminal
    assert cli.main(["relay", "host", "--dir", str(folder)]) == 1
    assert "only a person" in capsys.readouterr().out


def test_an_old_local_relay_is_named_with_how_to_restart_it(profiles, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_relays_in_use", lambda: {})
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="":
                        {"version": "0.0.1"} if url == cli.LOCAL_RELAY else None)
    _typing(monkeypatch, "")
    assert cli._choose_relay() == cli.LOCAL_RELAY
    assert "emporia relay stop && emporia relay start" in capsys.readouterr().out


def test_relay_host_as_a_service_on_a_server_of_its_own(profiles, monkeypatch, capsys, tmp_path):
    """No Docker: a systemd unit as its own user, its state in its folder, Caddy
    in front of 127.0.0.1 — and a Hermes agent on the same server as its operator."""
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "warden"]) == 0
    monkeypatch.setattr("shutil.which", lambda _: None)            # nothing runs here: commands are shown
    monkeypatch.setattr("emporia.relay_host.in_container", lambda: False)   # this test may run in one
    folder = tmp_path / "emporia-relay"
    _typing(monkeypatch, "", "n")                                  # Nous · no payments
    assert cli.main(["relay", "host", "--run", "service", "--dir", str(folder), "--domain", "relay.example.com",
                     "--admin", "warden", "--access", "closed"]) == 0
    out = capsys.readouterr().out
    assert "useradd --system" in out and "systemctl" in out and "caddyserver.com" in out
    unit = (folder / "emporia-relay.service").read_text()
    assert "User=emporia-relay" in unit and f"EnvironmentFile={folder / '.env'}" in unit
    assert f"ReadWritePaths={folder / 'data'}" in unit and "ProtectSystem=strict" in unit
    assert "reverse_proxy 127.0.0.1:8089" in (folder / "Caddyfile").read_text()
    env = _env_of(folder)
    assert env["EMPORIA_NO_DOTENV"] == "1" and env["EMPORIA_RELAY_HOST"] == "127.0.0.1"
    assert env["EMPORIA_KEYS_DIR"] == str(folder / "data" / "keys") and env["EMPORIA_AGENT_ID"]
    assert env["EMPORIA_RELAY_ACCESS"] == "closed" and len(env["EMPORIA_RELAY_MEMBERS"]) == 64   # warden
    assert _env_of(root / "warden")["EMPORIA_RELAY_URL"] == "http://127.0.0.1:8089"


def test_relay_host_asks_how_to_run_only_when_there_is_a_choice():
    from emporia import relay_host as rh
    assert rh.ways_to_run(True, True, True)[0] == ["docker"]        # Hermes in a container: Docker on the host
    assert rh.ways_to_run(False, True, False)[0] == ["docker"]
    assert rh.ways_to_run(False, False, True)[0] == ["service"]
    assert rh.ways_to_run(False, True, True) == (["docker", "service"], "")


def test_relay_link_unlink_and_links(profiles, monkeypatch, capsys, tmp_path):
    """P184: links live in the relay's .env, one-way until the other links back."""
    folder = tmp_path / "hosted"
    folder.mkdir()
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.main(["relay", "link", "https://b.example/", "--dir", str(folder)]) == 0
    assert cli.main(["relay", "link", "https://c.example", "--dir", str(folder)]) == 0
    assert _env_of(folder)["FEDERATED_RELAYS"] == "https://b.example,https://c.example"
    assert "one-way until" in capsys.readouterr().out
    assert cli.main(["relay", "unlink", "https://b.example", "--dir", str(folder)]) == 0
    assert _env_of(folder)["FEDERATED_RELAYS"] == "https://c.example"
    assert cli.main(["relay", "unlink", "https://nope.example", "--dir", str(folder)]) == 1
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    assert cli.main(["relay", "links", "--dir", str(folder)]) == 0
    assert "https://c.example — not answering" in capsys.readouterr().out


def test_doctor_warns_when_a2a_reaches_an_agent_with_a_wallet(profiles, tmp_path, monkeypatch):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "a2awallet", "--wallet"]) == 0
    cfg_path = root / "a2awallet" / "config.yaml"
    cfg = cli.installer().load_yaml(cfg_path)
    cfg.setdefault("gateway", {}).setdefault("platforms", {})["a2a"] = {"enabled": True}
    cli.installer().save_yaml(cfg_path, cfg)
    assert cli.a2a_state(root / "a2awallet") == (True, False)
    rep = cli.Report()
    import io
    from contextlib import redirect_stdout
    out = io.StringIO()
    with redirect_stdout(out):
        cli.doctor_a2a(rep, root / "a2awallet", True)
    assert "A2A_TRUSTED_PEERS" in out.getvalue()
    shadow = tmp_path / "home" / "plugins" / "platforms" / "a2a"
    shadow.mkdir(parents=True)
    (shadow / "plugin.yaml").write_text("id: hermes_comms\n")
    monkeypatch.setattr(cli, "hermes_home", lambda: tmp_path / "home")
    with redirect_stdout(out):
        cli.doctor_hermes_plugins(rep)
    assert "not Hermes's A2A plugin" in out.getvalue()


def test_emporia_visits_sets_where_an_agent_may_go(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "roamer"]) == 0
    assert cli.main(["visits", "roamer", "--policy", "listed", "--allow", "https://b.example/",
                     "--deny", "https://shady.example"]) == 0
    env = _env_of(root / "roamer")
    assert env["EMPORIA_VISIT_POLICY"] == "listed" and env["EMPORIA_VISIT_RELAYS"] == "https://b.example"
    assert env["EMPORIA_VISIT_DENY"] == "https://shady.example"
    assert cli.main(["visits", "roamer", "--deny", "https://b.example"]) == 0     # moves it across
    env = _env_of(root / "roamer")
    assert env["EMPORIA_VISIT_RELAYS"] == "" and "https://b.example" in env["EMPORIA_VISIT_DENY"]
    assert "only the relays allowed below" in capsys.readouterr().out


def test_relay_config_shows_checks_and_writes_settings(tmp_path, monkeypatch, capsys):
    """P199: one place — the relay's .env — and a command that checks before it writes."""
    folder = tmp_path / "hosted"
    folder.mkdir()
    monkeypatch.setattr(cli, "_TTY", False)
    run = lambda *a: cli.main(["relay", "config", *a, "--dir", str(folder)])   # noqa: E731
    assert run("set", "OPERATOR_FEE_BPS", "300") == 0 and _env_of(folder)["OPERATOR_FEE_BPS"] == "300"
    assert run("set", "OPERATOR_FEE_BPS", "lots") == 1
    assert run("set", "OPERATOR_FEE_BPS", "5000") == 1                       # at most 20%
    assert run("set", "EMPORIA_RELAY_ACCESS", "closed") == 0
    assert "Applies within seconds" in capsys.readouterr().out
    assert run("set", "EMPORIA_RELAY_ACCESS", "wide-open") == 1
    assert run("set", "FEDERATED_RELAYS", "https://b.example/,https://c.example") == 0
    assert _env_of(folder)["FEDERATED_RELAYS"] == "https://b.example,https://c.example"
    assert run("set", "STRIPE_SECRET_KEY", "sk_live_x") == 1                  # never on the command line
    assert "type it hidden" in capsys.readouterr().out and "sk_live_x" not in _env_of(folder).values()
    assert run("set", "NOT_A_SETTING", "x") == 1
    assert run("unset", "OPERATOR_FEE_BPS") == 0 and "OPERATOR_FEE_BPS" not in _env_of(folder)
    assert run("show") == 0
    out = capsys.readouterr().out
    assert "EMPORIA_RELAY_ACCESS" in out and "closed" in out and "250 (default)" in out


def test_relay_config_is_interactive_in_a_terminal(tmp_path, monkeypatch):
    folder = tmp_path / "hosted2"
    folder.mkdir()
    from emporia import relay_config as rc
    names = [s.name for s in rc.SETTINGS]
    fee = names.index("OPERATOR_FEE_BPS") + 1
    _typing(monkeypatch, str(fee), "400", str(len(names) + 1))   # pick the fee · 400 · done
    assert cli.main(["relay", "config", "--dir", str(folder)]) == 0
    assert _env_of(folder)["OPERATOR_FEE_BPS"] == "400"


def test_a_docker_relays_secret_is_a_file_typed_hidden(tmp_path, monkeypatch, capsys):
    """P200: a secret goes in secrets/NAME (0600), never its .env or argv."""
    from emporia import relay_host as rh
    folder = tmp_path / "docker-relay"
    rh.write_files(folder, tmp_path, "")
    (folder / ".env").write_text("STRIPE_SECRET_KEY=sk_test_old\n")
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "sk_test_new")
    assert cli.main(["relay", "config", "set", "STRIPE_SECRET_KEY", "--dir", str(folder)]) == 0
    secret = folder / "secrets" / "STRIPE_SECRET_KEY"
    assert secret.read_text().strip() == "sk_test_new" and oct(secret.stat().st_mode)[-3:] == "600"
    assert "STRIPE_SECRET_KEY" not in _env_of(folder)                     # the old copy is gone
    assert "sk_test_new" not in capsys.readouterr().out
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "not-a-key")
    assert cli.main(["relay", "config", "set", "STRIPE_WEBHOOK_SECRET", "--dir", str(folder)]) == 1
    assert cli.main(["relay", "config", "unset", "STRIPE_SECRET_KEY", "--dir", str(folder)]) == 0
    assert not secret.exists()
    assert "./secrets:/run/secrets:ro" in (folder / "compose.yaml").read_text()


def test_the_relay_reads_secrets_from_files_and_the_environment_wins(tmp_path, monkeypatch):
    from emporia import config
    (tmp_path / "STRIPE_SECRET_KEY").write_text("sk_test_from_file\n")
    (tmp_path / "STRIPE_WEBHOOK_SECRET").write_text("whsec_file\n")
    monkeypatch.setenv("EMPORIA_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_env")
    assert config.load_secret_files() == ["STRIPE_SECRET_KEY"]
    import os
    assert os.environ["STRIPE_SECRET_KEY"] == "sk_test_from_file" and os.environ["STRIPE_WEBHOOK_SECRET"] == "whsec_env"
    monkeypatch.delenv("STRIPE_SECRET_KEY")


def test_the_relay_drops_root_only_when_asked_and_only_as_root():
    import os

    from emporia import relay_server as rs
    assert rs.drop_root("") == ""                                      # not in the image: nothing to drop
    if os.getuid() != 0:
        assert rs.drop_root("emporia") == ""                           # not root: nothing to drop


def test_an_unlocked_in_process_wallet_still_pays_testnets_only(tmp_path):
    """P203: mainnet from an agent's own process needs the signer (or the risk accepted)."""
    env = {"EMPORIA_X402_PAY_NETWORKS": f"eip155:8453,{BASE}", "EMPORIA_X402_MAINNET": "1"}
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, env):
        os.environ.pop("EMPORIA_ALLOW_HOT_AGENT_WALLET", None)
        assert agent_wallet.allowed_networks() == [BASE]
        os.environ["EMPORIA_X402_PAY_NETWORKS"] = "eip155:8453"
        with pytest.raises(agent_wallet.AgentWalletError, match="emporia signer adopt"):
            agent_wallet.allowed_networks()
