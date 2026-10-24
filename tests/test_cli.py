"""P127: the `emporia` command — doctor, setup agent/relay, the .env merge."""
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from emporia import cli, identity


def test_merge_env_file_changes_only_what_it_sets_and_keeps_a_backup(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# mine\nKEEP=1\nEMPORIA_X402_ENABLED=0\n")
    changed = cli.merge_env_file(env, {"EMPORIA_X402_ENABLED": "1", "NEW": "x"})
    assert changed == ["EMPORIA_X402_ENABLED", "NEW"]
    assert env.read_text() == "# mine\nKEEP=1\nEMPORIA_X402_ENABLED=1\nNEW=x\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    [backup] = tmp_path.glob(".env.bak-*")
    assert "EMPORIA_X402_ENABLED=0" in backup.read_text()
    assert cli.merge_env_file(env, {"NEW": "x"}) == [], "no change, no new backup"


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    """A relay profile (`op`) holding the checkout, under a temporary profiles root."""
    root = tmp_path / "profiles"
    op = root / "op"
    op.mkdir(parents=True)
    (op / "config.yaml").write_text(yaml.dump(
        {"model": {"provider": "openrouter", "default": "deepseek/deepseek-v4-flash-0731"}}))
    (op / ".env").write_text("OPENROUTER_API_KEY=sk-or-x\nSTRIPE_SECRET_KEY=sk_test_relay\n")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: None)
    inst = cli.installer()
    monkeypatch.setattr(inst, "_hermes_profile_create", lambda name: (root / name).mkdir())
    monkeypatch.setattr(inst, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_install_skill_link", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_TOUCHED_PROFILES", [])
    return root, op


def test_setup_agent_makes_a_slim_profile_with_its_own_wallet(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        rc = cli.main(["setup", "agent", "gamma", "--wallet", "--max-daily-cents", "700"])
    assert rc == 0
    prof = root / "gamma"
    mcp_env = yaml.safe_load((prof / "config.yaml").read_text())["mcp_servers"]["emporia"]["env"]
    assert mcp_env["EMPORIA_DOTENV"] == str(prof / ".env")
    dotenv = (prof / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-x" in dotenv and "STRIPE_SECRET_KEY" not in dotenv
    assert "EMPORIA_X402_PAY_NETWORKS=eip155:84532" in dotenv
    assert "EMPORIA_X402_MAX_DAILY_CENTS=700" in dotenv
    wallet = prof / "home" / ".hermes" / "keys" / "gamma.x402.secp256k1"
    assert wallet.exists() and stat.S_IMODE(wallet.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "x402 wallet: 0x" in out


def test_doctor_names_each_problem_with_its_fix(profiles, capsys, monkeypatch):
    root, _ = profiles
    bad = root / "legacy"
    bad.mkdir()
    (bad / "config.yaml").write_text(yaml.dump(
        {"mcp_servers": {"emporia": {"env": {"EMPORIA_AGENT_ID": "legacy"}}}}))
    (bad / ".env").write_text("STRIPE_SECRET_KEY=sk_live_relays\n")
    keys = bad / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (keys / "legacy.priv").write_bytes(b"x" * 32)
    from emporia import nous_token
    monkeypatch.setattr(nous_token, "from_hermes", lambda profile=None: None)
    monkeypatch.setattr(cli, "hermes_home", lambda: root / "no-hermes")
    rc = cli.main(["doctor", "--profiles-root", str(root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "✗ not answering" in out and "emporia relay start" in out
    assert "lacks EMPORIA_DOTENV, EMPORIA_KEYS_DIR, EMPORIA_HERMES_BIN" in out
    assert "holds the relay's settings: STRIPE_SECRET_KEY" in out
    assert "emporia login --profile legacy" in out
    assert "sk_live_relays" not in out, "doctor never prints a secret"


def test_setup_relay_writes_the_testnet_preset_and_makes_an_operator_key(profiles, monkeypatch):
    _, op = profiles
    keys = op / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    monkeypatch.setattr(identity, "KEY_DIR", keys)
    with patch.dict(os.environ, {"EMPORIA_OPERATOR_PRIVATE_KEY": "", "EMPORIA_OPERATOR_ADDRESS": ""}):
        assert cli.main(["setup", "relay", "--fee-receiver", "0x" + "ab" * 20, "--yes"]) == 0
    dotenv = (op / ".env").read_text()
    assert "STRIPE_SECRET_KEY=sk_test_relay" in dotenv, "other lines are kept"
    assert "EMPORIA_ESCROW_CONTRACT=0xf96815976523E00e65Be8f34cA5e64b4f41EB19c" in dotenv
    assert "EMPORIA_ESCROW_FEE_RECEIVER=0x" + "ab" * 20 in dotenv
    assert (keys / "relay_operator.secp256k1").exists()
    assert "EMPORIA_OPERATOR_ADDRESS=0x" in dotenv
    assert "PRIVATE" not in dotenv.replace("EMPORIA_OPERATOR_PRIVATE_KEY", ""), "no key in the .env"
    assert cli.main(["setup", "relay", "--fee-receiver", "not-an-address", "--yes"]) == 1


def test_doctor_says_when_the_relay_needs_a_restart():
    """P139: code changes need a relay restart, and nothing said when one was owed."""
    from emporia.module_sdk import MODULE_REGISTRY
    from emporia.schema import SCHEMA_VERSION
    import emporia.relay_server  # noqa: F401  (registers the bundled games)
    bundled = [t for t in MODULE_REGISTRY if t.startswith("emporia:")]
    assert cli._relay_code_behind({"schema_version": SCHEMA_VERSION, "modules": bundled}) == ""
    assert "schema" in cli._relay_code_behind({"schema_version": SCHEMA_VERSION - 1, "modules": bundled})
    assert "emporia:two-thirds:v1" in cli._relay_code_behind(
        {"schema_version": SCHEMA_VERSION, "modules": [t for t in bundled if "two-thirds" not in t]})
    assert cli._relay_code_behind({"modules": bundled}) == "it reports no schema version"
