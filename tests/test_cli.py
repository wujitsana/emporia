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
    [backup] = tmp_path.glob(".env-backups/*/.env")   # P165: a name Hermes's tools refuse to read
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
        rc = cli.main(["setup", "agent", "gamma", "--keys", "files", "--wallet", "--max-daily-cents", "700"])
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


# --- hardening checks: skill lock, exposed api_server, relay-turn cap -------

def _agent_profile(root, name="zed", env="", cfg=None):
    prof = root / name
    keys = prof / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (keys / f"{name}.priv").write_bytes(b"x" * 32)
    (prof / "config.yaml").write_text(yaml.dump({"mcp_servers": {"emporia": {"env": {
        "EMPORIA_AGENT_ID": name, "EMPORIA_DOTENV": str(prof / ".env"),
        "EMPORIA_KEYS_DIR": str(keys), "EMPORIA_HERMES_BIN": "/x/hermes"}}}, **(cfg or {})}))
    (prof / ".env").write_text(env)
    return prof


def _doctor(root, capsys, monkeypatch):
    from emporia import nous_token
    monkeypatch.setattr(nous_token, "from_hermes", lambda profile=None: None)
    cli.main(["doctor", "--all", "--profiles-root", str(root)])
    return capsys.readouterr().out


def test_doctor_checks_skill_lock_api_server_and_turn_cap(profiles, capsys, monkeypatch):
    import json
    import time
    root, _ = profiles
    prof = _agent_profile(root, env="API_SERVER_KEY=" + "k" * 20 + "\nAPI_SERVER_HOST=0.0.0.0\nEMPORIA_DAILY_TURNS=2\n")
    now = time.time()
    (prof / "emporia_turns.json").write_text(json.dumps({"turns": [now - 10, now - 100, now - 90000], "notified": 0}))
    out = _doctor(root, capsys, monkeypatch)
    assert "skills not locked (emporia, emporia-setup)" in out and "emporia agent refresh zed" in out
    assert "api_server listens on 0.0.0.0" in out and "make zed act and pay" in out
    assert "relay-started turns in 24 h: 2 / 2 — at the cap" in out
    assert "k" * 20 not in out

    inst = cli.installer()                                    # P284: real copies, not links (the fixture stubs the installer's)
    for n in inst.EMPORIA_SKILL_FILES:
        inst._copy_skill(inst._EMPORIA_DIR / "skills" / n, prof / "skills" / n)
    assert not (prof / "skills" / "emporia").is_symlink() and (prof / "skills" / "emporia" / "SKILL.md").exists()
    (prof / "skills" / ".usage.json").write_text(json.dumps({"emporia": {"locked": True}, "emporia-setup": {"locked": True}}))
    (prof / ".env").write_text("API_SERVER_KEY=" + "k" * 20 + "\n")   # key alone: enabled, loopback default
    out = _doctor(root, capsys, monkeypatch)
    assert "Emporia's skills current and locked" in out
    assert "api_server listens" not in out
    assert "relay-started turns in 24 h: 2 / 300" in out


def test_doctor_api_server_off_or_loopback_is_quiet(profiles, capsys, monkeypatch):
    root, _ = profiles
    prof = _agent_profile(root, cfg={"platforms": {"api_server": {"enabled": True, "extra": {"host": "localhost"}}}})
    assert "api_server listens" not in _doctor(root, capsys, monkeypatch)
    cfg = yaml.safe_load((prof / "config.yaml").read_text())
    cfg["platforms"]["api_server"]["extra"]["host"] = "10.0.0.5"
    (prof / "config.yaml").write_text(yaml.dump(cfg))
    assert "api_server listens on 10.0.0.5" in _doctor(root, capsys, monkeypatch)
    cfg["platforms"]["api_server"]["enabled"] = False
    (prof / "config.yaml").write_text(yaml.dump(cfg))
    assert "api_server listens" not in _doctor(root, capsys, monkeypatch)


def test_lock_emporia_skill_runs_hermes_for_the_profile_and_only_warns_on_failure(tmp_path, monkeypatch, capsys):
    import subprocess
    prof = tmp_path / "zed"
    prof.mkdir()
    monkeypatch.setenv("EMPORIA_HERMES_BIN", "/x/hermes")
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "locked\n", "")
    monkeypatch.setattr(cli.subprocess, "run", fake)
    assert cli.lock_emporia_skill(prof) is True
    assert calls == [["/x/hermes", "-p", "zed", "curator", "lock", s] for s in ("emporia", "emporia-setup")]

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "no skill named\n", ""))
    assert cli.lock_emporia_skill(prof) is False
    assert "Could not lock" in capsys.readouterr().out

    def boom(*a, **k):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.lock_emporia_skill(prof) is False
    assert "hermes -p zed curator lock emporia" in capsys.readouterr().out


def test_setup_agent_and_secure_lock_the_skill(profiles, monkeypatch):
    root, _ = profiles
    locked = []
    monkeypatch.setattr(cli, "lock_emporia_skill", lambda profile, *a: locked.append(profile.name) or True)
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "gamma", "--keys", "files"]) == 0
    assert cli.main(["agent", "secure", "gamma"]) == 0
    assert locked == ["gamma", "gamma"]


def test_agent_skills_are_copied_and_refreshed_but_the_dev_profile_keeps_links(tmp_path, capsys):
    """P284: Hermes cannot lock a symlinked skill, so an agent profile gets a copy."""
    inst = cli.installer()
    prof = tmp_path / "zed"
    (prof / "skills").mkdir(parents=True)
    (prof / "skills" / "emporia").symlink_to(inst._EMPORIA_DIR / "skills" / "emporia")   # an older install
    inst._install_skill_link(prof)
    for name in inst.EMPORIA_SKILL_FILES:
        dst = prof / "skills" / name
        assert dst.exists() and not dst.is_symlink(), name
        assert inst.skill_copy_current(inst._EMPORIA_DIR / "skills" / name, dst)
    (prof / "skills" / "emporia" / "SKILL.md").write_text("tampered")
    assert not inst.skill_copy_current(inst._EMPORIA_DIR / "skills" / "emporia", prof / "skills" / "emporia")
    inst._install_skill_link(prof)                                # refresh puts Emporia's back
    assert inst.skill_copy_current(inst._EMPORIA_DIR / "skills" / "emporia", prof / "skills" / "emporia")

    other = tmp_path / "other"
    (other / "skills" / "emporia").mkdir(parents=True)            # a folder Emporia did not put there
    (other / "skills" / "emporia" / "SKILL.md").write_text("theirs")
    inst._install_skill_link(other)
    assert (other / "skills" / "emporia" / "SKILL.md").read_text() == "theirs"
    assert "not Emporia's copy" in capsys.readouterr().out

    dev = inst._EMPORIA_DIR.parent                                # the profile the repo sits in
    assert inst.is_dev_profile(dev) and not inst.is_dev_profile(prof)


# -- P285: the relay this machine runs comes back with the gateway ----------------

@pytest.fixture
def relay_here(tmp_path, monkeypatch):
    pid_file = tmp_path / "emporia-relay.pid"
    monkeypatch.setattr(cli, "_relay_files", lambda: (pid_file, tmp_path / ".relay.log"))
    state = {"up": False, "started": []}
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": {"status": "ok"} if state["up"] else None)

    def start(env=None, url=None, say=print):
        state["started"].append(env)
        state["up"] = True
        return 0
    monkeypatch.setattr(cli, "_start_relay", start)
    return pid_file, state


def test_a_relay_started_here_comes_back_once_with_a_clean_environment(relay_here, tmp_path, monkeypatch):
    pid_file, state = relay_here
    agent_env = tmp_path / "alpha.env"
    agent_env.write_text("EMPORIA_IDENTITY_PRIVATE_KEY=secret\nOPENROUTER_API_KEY=k\n# c\nexport NOUS_X=1\n")
    monkeypatch.setenv("EMPORIA_DOTENV", str(agent_env))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("NOUS_X", "1")
    monkeypatch.setenv("EMPORIA_KEYS_DIR", "/agent/keys")
    monkeypatch.setenv("PATH_FOR_TEST", "kept")
    pid_file.write_text("999999999")                      # left by a relay that died with the container
    pid_file.with_suffix(".url").write_text("http://127.0.0.1:8088")
    assert cli.ensure_local_relay("http://localhost:8088") == "started"
    env = state["started"][0]
    assert env["PATH_FOR_TEST"] == "kept"
    assert not [k for k in env if k.startswith("EMPORIA_")]
    assert "OPENROUTER_API_KEY" not in env and "NOUS_X" not in env   # nothing of the agent's .env
    assert cli.ensure_local_relay("http://localhost:8088") == "up"     # the next agent finds it up
    assert len(state["started"]) == 1


def test_a_stopped_or_remote_relay_is_left_alone(relay_here):
    pid_file, state = relay_here
    assert "not started here" in cli.ensure_local_relay("http://localhost:8088")   # no pid file: stopped
    pid_file.write_text("999999999")
    assert cli.ensure_local_relay("https://relay.example.com") == "not on this machine"
    pid_file.with_suffix(".url").write_text("http://127.0.0.1:8088")
    assert cli.ensure_local_relay("http://127.0.0.1:18321") == "not the relay `emporia relay start` runs here"
    assert state["started"] == []


def test_stop_does_not_kill_a_reused_pid(tmp_path, monkeypatch, capsys):
    import os
    pid_file = tmp_path / "emporia-relay.pid"
    pid_file.write_text(str(os.getpid()))                 # this test process: not a relay
    monkeypatch.setattr(cli, "_relay_files", lambda: (pid_file, tmp_path / ".relay.log"))
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda *a: killed.append(a))
    assert cli.main(["relay", "stop"]) == 1
    assert killed == [] and not pid_file.exists()


def test_a_reused_pid_is_not_taken_for_the_signer_or_relay():
    """P285: `signer stop` and `relay stop` signal only their own process (the live suite stops a real one)."""
    import os
    assert not cli._is_our_pid(os.getpid(), b"signer serve")
    assert not cli._is_relay_pid(os.getpid()) and not cli._is_relay_pid(999999999)
    assert cli._is_our_pid(os.getpid(), b"pytest")
