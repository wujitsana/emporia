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
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "zeta", "--keys", "files"]) == 0
    keys = root / "zeta" / "home" / ".hermes" / "keys"
    assert {p.name for p in keys.glob("zeta.x402.*")} == {"zeta.x402.phrase", "zeta.x402.secp256k1",
                                                         "zeta.x402.ed25519"}
    dotenv = (root / "zeta" / ".env").read_text()
    assert "EMPORIA_X402_MAX_PAYMENT_CENTS=500" in dotenv and "EMPORIA_X402_MAX_DAILY_CENTS=2000" in dotenv
    assert "EMPORIA_PAYMENT_APPROVAL_CENTS=200" in dotenv
    out = capsys.readouterr().out
    assert "one recovery phrase" in out and "Emporia never prints it" in out
    phrase = (keys / "zeta.x402.phrase").read_text().strip()
    assert phrase not in out, "the phrase is never printed"


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
    monkeypatch.setenv("KAPPA_KEY", acct.key.hex())
    assert cli.main(["wallet", "import", "kappa", "--link", "env:KAPPA_KEY"]) == 0
    dotenv = (root / "kappa" / ".env").read_text()
    assert "EMPORIA_X402_EVM_KEY_SOURCE=env:KAPPA_KEY" in dotenv and acct.key.hex() not in dotenv
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
    assert not list(keys.glob("xi.*priv")) and not list(keys.glob("xi.x402.*"))
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
    assert env["EMPORIA_X402_EVM_KEY_SOURCE"] == "env:EVM_PRIVATE_KEY", "the env var is linked, not copied"
    assert env.get("EMPORIA_X402_SOLANA_PRIVATE_KEY"), "the key file is copied (into the .env by default)"


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
