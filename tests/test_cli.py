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


def test_setup_agent_makes_a_slim_profile_with_its_own_wallet(profiles, capsys):
    root, _ = profiles
    with patch.dict(os.environ):
        rc = cli.main(["setup", "agent", "gamma", "--keys", "files", "--wallet", "--max-daily-cents", "700"])
    assert rc == 0
    prof = root / "gamma"
    mcp_env = yaml.safe_load((prof / "config.yaml").read_text())["mcp_servers"]["emporia"]["env"]
    assert mcp_env["EMPORIA_DOTENV"] == str(prof / ".env")
    dotenv = (prof / ".env").read_text()
    assert "sk-or-x" not in dotenv and "STRIPE_SECRET_KEY" not in dotenv, "no credential is copied"
    assert "EMPORIA_X402_PAY_NETWORKS=eip155:84532" in dotenv
    assert "EMPORIA_X402_MAX_DAILY_CENTS=700" in dotenv
    wallet = prof / "home" / ".hermes" / "keys" / "gamma.x402.secp256k1"
    assert wallet.exists() and stat.S_IMODE(wallet.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "x402 wallet: 0x" in out


def test_setup_agent_on_an_existing_profile_keeps_its_model_credentials_and_identity(profiles, monkeypatch, capsys):
    """The live deploy: setup on alpha/beta switched nous to openrouter, copied the person's provider key and
    would have changed the identity source. Setup again, with and without --sync-model, changes none of it."""
    root, _ = profiles
    prof = root / "alpha"
    (prof / "home" / ".hermes" / "keys").mkdir(parents=True)
    (prof / "config.yaml").write_text(yaml.dump({"model": {"provider": "nous", "default": "nous-model"},
                                                 "mcp_servers": {"emporia": {"env": {"EMPORIA_AGENT_ID": "alpha"}}}}))
    own_env = ("EMPORIA_AGENT_ID=alpha\nEMPORIA_IDENTITY_KEY_SOURCE=env:EMPORIA_IDENTITY_PRIVATE_KEY\n"
               "EMPORIA_IDENTITY_PRIVATE_KEY=" + "cd" * 32 + "\n")
    (prof / ".env").write_text(own_env)
    for extra in ([], ["--sync-model"], ["--no-sync-model", "--keys", "env"]):
        with patch.dict(os.environ):
            assert cli.main(["setup", "agent", "alpha", "--agent-id", "alpha", *extra]) == 0
        cfg = yaml.safe_load((prof / "config.yaml").read_text())
        assert cfg["model"] == {"provider": "nous", "default": "nous-model"}, extra
        env = (prof / ".env").read_text()
        assert "sk-or-x" not in env and "OPENROUTER_API_KEY" not in env, extra
        assert "EMPORIA_IDENTITY_KEY_SOURCE=env:EMPORIA_IDENTITY_PRIVATE_KEY" in env, extra
        assert "EMPORIA_IDENTITY_PRIVATE_KEY=" + "cd" * 32 in env, extra
        assert not list((prof / "home" / ".hermes" / "keys").glob("alpha.priv")), extra
    assert "never changes an existing profile's model" in capsys.readouterr().out


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
    monkeypatch.setattr(cli, "hermes_home", lambda: root / "no-hermes")
    rc = cli.main(["doctor", "--profiles-root", str(root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "✗ not answering" in out and "emporia relay start" in out
    assert "lacks EMPORIA_DOTENV, EMPORIA_KEYS_DIR, EMPORIA_HERMES_BIN" in out
    assert "holds the relay's settings: STRIPE_SECRET_KEY" in out
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
    assert "EMPORIA_FEE_ADDRESS=0x" + "ab" * 20 in dotenv
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
    assert "emporia:two-thirds:v2" in cli._relay_code_behind(
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
    cli.main(["doctor", "--all", "--profiles-root", str(root)])
    return capsys.readouterr().out


def test_doctor_checks_api_server_and_turn_cap(profiles, capsys, monkeypatch):
    import json
    import time
    root, _ = profiles
    prof = _agent_profile(root, env="API_SERVER_KEY=" + "k" * 20 + "\nAPI_SERVER_HOST=0.0.0.0\nEMPORIA_DAILY_TURNS=2\n")
    now = time.time()
    (prof / "emporia_turns.json").write_text(json.dumps({"turns": [now - 10, now - 100, now - 90000], "notified": 0}))
    out = _doctor(root, capsys, monkeypatch)
    assert "api_server listens on 0.0.0.0" in out and "make zed act and pay" in out
    assert "relay-started turns in 24 h: 2 / 2 — at the cap" in out
    assert "k" * 20 not in out

    (prof / ".env").write_text("API_SERVER_KEY=" + "k" * 20 + "\n")   # key alone: enabled, loopback default
    out = _doctor(root, capsys, monkeypatch)
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


def test_the_plugin_passes_the_catalogs_own_validation_and_registers_everything(tmp_path):
    """P295: `hermes plugins validate` is the catalog's CI gate — manifest, load,
    declared hooks against registrations, security scan, no core override. Then,
    under Hermes's interpreter, what register() hands Hermes."""
    import json
    import shutil
    import subprocess
    hermes, hermes_python = shutil.which("hermes"), Path("/opt/hermes/.venv/bin/python")
    if not hermes or not hermes_python.exists():
        pytest.skip("Hermes is not installed here")
    plugin = cli.REPO / "hermes_plugin" / "emporia"
    out = subprocess.run([hermes, "plugins", "validate", str(plugin)], capture_output=True, text=True,
                         timeout=300, env={**os.environ, "HERMES_HOME": str(tmp_path)})
    assert out.returncode == 0 and "Validation passed" in out.stdout, out.stdout[-3000:]
    probe = f"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("emporia_plugin", {str(plugin / "__init__.py")!r},
                                              submodule_search_locations=[{str(plugin)!r}])
m = importlib.util.module_from_spec(spec); sys.modules["emporia_plugin"] = m; spec.loader.exec_module(m)
got = {{}}
class Ctx:
    def register_hook(self, name, fn): got.setdefault("hooks", []).append(name)
    def register_platform(self, **kw): got["platform"] = kw["name"]
    def register_skill(self, name, path, description=""):
        assert path.is_file() and description, name
        got.setdefault("skills", []).append(name)
    def register_cli_command(self, **kw): got["cli"] = kw["name"]
m.register(Ctx())
print(json.dumps(got))
"""
    out = subprocess.run([str(hermes_python), "-c", probe], cwd="/opt/hermes", capture_output=True, text=True,
                         timeout=60, env={**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": "/opt/hermes"})
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {
        "hooks": ["pre_tool_call", "transform_tool_result", "pre_llm_call", "post_llm_call", "pre_tool_call", "pre_llm_call",
                  "pre_llm_call"], "platform": "emporia",
        "skills": ["emporia", "emporia-setup"], "cli": "emporia"}


def test_the_agent_id_is_the_profile_name_except_for_the_default_profile(tmp_path, monkeypatch, capsys):
    """P295: every Hermes user's root profile is called `default`, so on a public relay
    its agent needs a name of its own; an existing agent keeps the one it has."""
    import argparse
    monkeypatch.setattr(cli, "_mcp_env", lambda profile: {})
    a = argparse.Namespace(agent_id="")
    assert cli._choose_agent_id(a, "alpha", tmp_path, False) == "alpha"
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli._choose_agent_id(a, "default", tmp_path, False) == ""
    assert "needs --agent-id" in capsys.readouterr().out
    assert cli._choose_agent_id(argparse.Namespace(agent_id="Mira-Bot"), "default", tmp_path, False) == "mira-bot"
    assert cli._choose_agent_id(argparse.Namespace(agent_id="x!"), "default", tmp_path, False) == ""
    monkeypatch.setattr(cli, "_mcp_env", lambda profile: {"EMPORIA_AGENT_ID": "kept"})
    assert cli._choose_agent_id(argparse.Namespace(agent_id="other"), "default", tmp_path, True) == "kept"



def test_a_taken_name_is_asked_again_before_anything_is_written(tmp_path, monkeypatch, capsys):
    """P296: setup checks a new agent's name with its relay first. Taken: in a terminal
    it asks for another; without one it stops, having written nothing."""
    import argparse
    monkeypatch.setattr(cli, "_mcp_env", lambda profile: {})
    taken = {"alpha", "alpha2"}
    monkeypatch.setattr(cli, "_name_on_relay", lambda relay, aid: (aid not in taken, "taken" if aid in taken else ""))
    a = argparse.Namespace(agent_id="")
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli._choose_agent_id(a, "alpha", tmp_path, False, "http://r") == ""
    assert "--agent-id" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_TTY", True)
    answers = iter(["alpha2", "alpha3"])
    monkeypatch.setattr(cli, "prompt", lambda *_a, **_k: next(answers))
    assert cli._choose_agent_id(a, "alpha", tmp_path, False, "http://r") == "alpha3"
    # a relay that cannot say does not stop setup; registering checks again
    monkeypatch.setattr(cli, "_name_on_relay", lambda relay, aid: (None, "the relay is not answering"))
    assert cli._choose_agent_id(a, "beta", tmp_path, False, "http://r") == "beta"


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


def test_from_the_bridge_the_relay_gets_none_of_what_the_plugin_handed_it(relay_here, tmp_path, monkeypatch):
    """Catalog rule 11: the bridge reads no .env (the plugin hands it the agent's values and
    names them in EMPORIA_HANDED_NAMES), so the relay it restarts is kept clean by those names,
    without the agent's .env being opened."""
    pid_file, state = relay_here
    monkeypatch.setenv("EMPORIA_DOTENV", str(tmp_path / "never-read.env"))     # does not exist
    monkeypatch.setenv("EMPORIA_HANDED_NAMES", "EMPORIA_KEYS_DIR,VAULT_ADDR,BW_SESSION")
    monkeypatch.setenv("EMPORIA_KEYS_DIR", "/agent/keys")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.alpha")
    monkeypatch.setenv("BW_SESSION", "agent-session")
    monkeypatch.setenv("PATH_FOR_TEST", "kept")
    monkeypatch.setattr(cli, "_dotenv_names", lambda path: (_ for _ in ()).throw(AssertionError("read")))
    pid_file.write_text("999999999")
    pid_file.with_suffix(".url").write_text("http://127.0.0.1:8088")
    assert cli.ensure_local_relay("http://localhost:8088") == "started"
    env = state["started"][0]
    assert env["PATH_FOR_TEST"] == "kept"
    assert not [k for k in env if k.startswith("EMPORIA_")]
    assert "VAULT_ADDR" not in env and "BW_SESSION" not in env


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


def test_doctor_finds_two_identity_keys_and_names_the_one_the_relay_knows(tmp_path, monkeypatch):
    """11-04: a key source in the .env won over the key file, so the agent's tools signed with
    one key and the console with another — and the relay knew only the file's."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from emporia import cli
    from emporia.identity import _keyfile_stem

    file_key, env_key = (ed25519.Ed25519PrivateKey.generate() for _ in range(2))
    (tmp_path / f"{_keyfile_stem('ag')}.priv").write_bytes(file_key.private_bytes_raw())
    pub = lambda k: k.public_key().public_bytes_raw().hex()  # noqa: E731

    class Resp:
        status_code = 200

        def __init__(self, key):
            self.key = key

        def json(self):
            return {"public_key_hex": self.key}

    lines = []

    class Rep:
        def ok(self, m, *a): lines.append(("ok", m))
        def fail(self, m, *a): lines.append(("fail", m, *a))
        def warn(self, m, *a): lines.append(("warn", m))

    dotenv = {"EMPORIA_IDENTITY_PRIVATE_KEY": env_key.private_bytes_raw().hex()}
    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp(pub(file_key)))
    cli.doctor_identity_key(Rep(), "ag", "ag", tmp_path, "env:EMPORIA_IDENTITY_PRIVATE_KEY", dotenv,
                            "http://r", True)
    [(kind, msg, fix)] = lines
    assert kind == "fail" and "two identity keys" in msg and "the key file is the one its relay knows" in msg
    assert "remove EMPORIA_IDENTITY_KEY_SOURCE" in fix
    assert env_key.private_bytes_raw().hex() not in msg + fix                       # public halves only

    lines.clear()                                                                    # one key, the relay's
    cli.doctor_identity_key(Rep(), "ag", "ag", tmp_path, "", {}, "http://r", True)
    assert lines[0][0] == "ok"
    lines.clear()                                                                    # one key, not the relay's
    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp(pub(env_key)))
    cli.doctor_identity_key(Rep(), "ag", "ag", tmp_path, "", {}, "http://r", True)
    assert lines[0][0] == "fail" and "does not know" in lines[0][1]
    lines.clear()                                                                    # a command source is not run
    cli.doctor_identity_key(Rep(), "ag", "ag", tmp_path, "cmd:op read x", {}, "http://r", True)
    assert lines[0][0] == "ok" and "does not open" in lines[0][1]


def test_a_profile_the_plugin_just_made_is_set_up_as_a_new_agent(profiles, capsys):
    """Found by the end-to-end run: since P296 the plugin makes the profile (and its
    config.yaml) before setup agent runs, which then took it for an existing one —
    role read from its full tools (developer), keys left in files."""
    root, _ = profiles
    made = root / "kappa"
    made.mkdir()
    (made / "config.yaml").write_text(yaml.dump({"plugins": {"enabled": ["emporia"]}}))
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "kappa", "--new-profile"]) == 0
    out = capsys.readouterr().out
    assert "New agent: kappa" in out and "existing keys are still files" not in out
    assert cli.profile_role(made, False) == "marketplace"
    assert "EMPORIA_KEY_STORAGE=env" in (made / ".env").read_text()
    assert not list(made.rglob("kappa.priv")), "its new identity key moved into its .env"


def test_doctor_checks_each_agent_against_its_own_relay(profiles, capsys, monkeypatch):
    """Found by the end-to-end run: every agent was checked against this process's
    EMPORIA_RELAY_URL (default localhost:8088), not the relay its profile names."""
    root, _ = profiles
    for name, relay in (("ann", "http://127.0.0.1:7001"), ("bob", "http://127.0.0.1:7002"), ("cy", "")):
        prof = _agent_profile(root, name)
        cfg = yaml.safe_load((prof / "config.yaml").read_text())
        if relay:
            cfg["mcp_servers"]["emporia"]["env"]["EMPORIA_RELAY_URL"] = relay
        (prof / "config.yaml").write_text(yaml.dump(cfg))
    (root / "cy" / ".env").write_text("EMPORIA_RELAY_URL=http://127.0.0.1:7003\n")    # only in its .env
    asked = []
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: asked.append(cli.relay_url()) or None)
    with patch.dict(os.environ, {"EMPORIA_RELAY_URL": "http://127.0.0.1:8088"}):
        out = _doctor(root, capsys, monkeypatch)
        assert os.environ["EMPORIA_RELAY_URL"] == "http://127.0.0.1:8088", "put back afterwards"
    assert "8088" not in "".join(asked) and "8088" not in out
    for name, port in (("ann", 7001), ("bob", 7002), ("cy", 7003)):
        relay_at, agent_at = out.index(f"Relay  http://127.0.0.1:{port}"), out.index(f"Agent  {name}")
        assert relay_at < agent_at, "each relay's section comes before its agents"


@pytest.mark.parametrize("cards,says", [(True, "no wallet — can pay by card only"),
                                        (False, "no wallet — it cannot pay here (this relay takes no cards)")])
def test_doctor_says_card_only_only_where_the_relay_takes_cards(profiles, capsys, monkeypatch, cards, says):
    root, _ = profiles
    _agent_profile(root)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: {"stripe_enabled": cards, "version": "x"})
    monkeypatch.setattr(cli, "_signed_get", lambda *a, **k: (404, None))
    out = _doctor(root, capsys, monkeypatch)
    assert says in out
    assert ("by card only" in out) is cards


def test_doctor_says_where_the_plugin_came_from(tmp_path):
    """Found by the end-to-end run: a file:// install was said to be "installed from the catalog"."""
    import json
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    meta = plugins / ".install-metadata.json"
    assert cli._plugin_origin(tmp_path).startswith("installed (Hermes keeps no record")
    meta.write_text(json.dumps({"emporia": {"source": "file:///src/emporia#hermes_plugin/emporia"}}))
    assert cli._plugin_origin(tmp_path) == "installed from file:///src/emporia#hermes_plugin/emporia"
    meta.write_text(json.dumps({"emporia": {"source": "https://x", "catalog": {"name": "emporia"}}}))
    assert cli._plugin_origin(tmp_path) == "installed from the catalog"


def test_new_profile_is_refused_for_a_profile_that_holds_an_agent(profiles, capsys):
    """Security audit (11-05): `--new-profile` (bcb4d18) makes setup treat a profile as
    brand new — its limits, approval threshold, role and key storage from the defaults,
    not its own. The flag is hidden but anyone may type it, an agent with a terminal
    included (with its person's yes to a harmless-looking `setup agent`): passed for a
    profile that already holds an agent, its person's limits and role were replaced by
    the defaults. It is refused there; the plugin passes it only for a profile it made
    a moment ago."""
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "lambda", "--role", "both", "--keys", "files", "--wallet",
                         "--max-daily-cents", "300", "--max-payment-cents", "100",
                         "--approve-above-cents", "0"]) == 0
    prof = root / "lambda"
    keydir = prof / "home" / ".hermes" / "keys"
    before = (prof / ".env").read_text(), cli.profile_role(prof, True)
    keys = {f.name: f.read_bytes() for f in keydir.iterdir() if f.is_file()}
    capsys.readouterr()
    with patch.dict(os.environ):
        rc = cli.main(["setup", "agent", "lambda", "--new-profile"])
    after = (prof / ".env").read_text(), cli.profile_role(prof, True)
    assert (rc, after) == (1, before), after
    assert "already" in capsys.readouterr().out
    assert {f.name: f.read_bytes() for f in keydir.iterdir() if f.is_file()} == keys
    # Review pass (11-05): the plugin passes --new-profile, so its refusal is read by a
    # plugin-only user — who has `hermes emporia setup`, not `emporia setup agent`.
    from emporia import cli_hints
    with patch.object(cli_hints, "via_plugin", lambda runtime=None: True), patch.dict(os.environ):
        assert cli.main(["setup", "agent", "lambda", "--new-profile"]) == 1
    out = capsys.readouterr().out
    assert "hermes emporia setup --agent-profile lambda" in out and "`emporia setup agent" not in out, out


def test_setup_agent_is_the_wizard_not_the_check_before_it():
    """Found merging the security audit: its `_holds_an_agent` landed between `@wizard`
    and `cmd_setup_agent`, so setup agent lost going back a question, and the check was
    wrapped in a wizard."""
    assert hasattr(cli.cmd_setup_agent, "__wrapped__")
    assert not hasattr(cli._holds_an_agent, "__wrapped__")


def test_a_catalog_runtime_is_never_pulled_or_synced_by_emporia_update(monkeypatch, capsys):
    """Catalog rule 3: the runtime the catalog plugin installed moves only with the plugin's pin.
    `emporia update` (git pull, uv/pip sync, plugin refresh) refuses it before doing anything."""
    import argparse
    ran = []
    monkeypatch.setattr(cli.installer(), "from_catalog", lambda: True)
    monkeypatch.setattr(cli, "_run", lambda argv, cwd=None, timeout=None: ran.append(argv) or True)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: ran.append(a) or None)
    assert cli.cmd_update(argparse.Namespace(no_pull=False)) == 1
    assert cli.cmd_update(argparse.Namespace(no_pull=True)) == 1
    assert ran == []
    assert "moves only with it" in capsys.readouterr().out
