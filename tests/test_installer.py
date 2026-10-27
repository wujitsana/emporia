"""Installer tests. `installer/` is not a packaged module (see pyproject.toml's
`packages = ["src/emporia"]`) and has no prior test coverage — load install.py
directly by file path rather than building out a full installer test harness,
which is out of scope for the small, pure function under test here (N10-5)."""

import importlib.util
import shutil
import sys
from pathlib import Path

import yaml

_INSTALLER_PATH = Path(__file__).resolve().parent.parent / "installer" / "install.py"
_spec = importlib.util.spec_from_file_location("emporia_installer", _INSTALLER_PATH)
installer = importlib.util.module_from_spec(_spec)
sys.modules["emporia_installer"] = installer
_spec.loader.exec_module(installer)


def test_warn_if_a2a_enabled_prints_warning_when_enabled(capsys):
    """N10-5 (R5): detection only. If gateway.platforms.a2a.enabled is truthy,
    warn about mcp-emporia exposure — do not touch the config."""
    config = {"gateway": {"platforms": {"a2a": {"enabled": True}}}}
    installer._warn_if_a2a_enabled(config)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "mcp-emporia" in out
    assert config == {"gateway": {"platforms": {"a2a": {"enabled": True}}}}  # untouched


def test_warn_if_a2a_enabled_silent_when_absent(capsys):
    for config in ({}, {"gateway": {}}, {"gateway": {"platforms": {"a2a": {"enabled": False}}}}):
        installer._warn_if_a2a_enabled(config)
    out = capsys.readouterr().out
    assert out == ""


def test_guardrails_are_the_regex_firewall_only():
    """P151: the NVIDIA-hosted semantic layer is gone. Setting a profile up
    writes only the firewall's mode, drops the old NeMo setting from a profile
    that had it, and leaves an NVIDIA_API_KEY alone — it may be the key an
    agent's own model runs on."""
    env_block = {"EMPORIA_NEMO_GUARDRAILS_ENABLED": "1", "NVIDIA_API_KEY": "nvapi-model-key"}
    installer._apply_guardrails_env(env_block, disabled=False)
    assert env_block == {"EMPORIA_GUARDRAILS_MODE": "enforce", "NVIDIA_API_KEY": "nvapi-model-key"}
    off: dict[str, str] = {}
    installer._apply_guardrails_env(off, disabled=True)
    assert off == {"EMPORIA_GUARDRAILS_MODE": "off"}
    assert installer._guardrails_env_lines() == ["EMPORIA_GUARDRAILS_MODE=enforce"]
    assert installer._guardrails_env_lines(disabled=True) == ["EMPORIA_GUARDRAILS_MODE=off"]


def test_the_installer_writes_no_gateway_platform_entry():
    """P63: `platforms: - platform: emporia` was written into every install and
    did nothing.

    Hermes discovers plugins through the `hermes_agent.plugins` entry point;
    this package declares none, and nothing in the Hermes tree knows the name,
    so the gateway's registry lookup misses and it logs "No adapter for
    'emporia' -- is the plugin installed?". The entry made `config.yaml` claim
    an integration that was not there.

    It is also the *gateway/desktop* half of Hermes's plugin surface — the same
    family as `register_dashboard_auth_provider` and the Telegram/Slack
    handlers — rather than the agent-running half (`register_tool`,
    `register_hook`). Emporia's agent integration is the MCP server, which is
    registered and enabled, and that is deliberately unchanged.

    This guards the deletion: writing the entry back is a decision that should
    come with a real plugin, not a line reappearing in a refactor.
    """
    from pathlib import Path

    src = Path(__file__).parent.parent / "installer" / "install.py"
    text = src.read_text()
    offenders = [
        line.strip() for line in text.splitlines()
        if '"platform": "emporia"' in line
        or ('config["platforms"]' in line and not line.strip().startswith("#"))
    ]
    assert not offenders, (
        "the installer is writing a gateway platform entry again; it is inert "
        f"until a real plugin exists: {offenders}"
    )


def test_no_setup_command_prunes_a_contributors_venv():
    """P64: a bare `uv sync` installs the default set and *removes* everything
    else, including the `dev` and `x402` extras CI installs with
    `uv sync --frozen --extra dev --extra x402`.

    Both the installer and `scripts/local_relay.py` ran one — and
    `local_relay.py`'s runs on every relay start. Measured after a single
    `--local-demo`: `import ruff` and `import eth_account` both gone. The next
    `uv run python -m ruff check` (CI's own lint command) then failed with "No
    module named ruff", with nothing connecting it back to the setup step.

    `--inexact` installs what is needed and leaves the rest alone.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    offenders = []
    for rel in ("installer/install.py", "scripts/local_relay.py"):
        text = (root / rel).read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if '"sync"' in line and "--inexact" not in text[
                max(0, text.find(line) - 200): text.find(line) + 400
            ]:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        f"`uv sync` without --inexact deletes the dev toolchain: {offenders}"
    )


def test_the_seed_is_told_which_relay_to_seed():
    """P64: `_run_demo_seed()` took `relay_url`, used it for the health check,
    and then launched the seed subprocess without it.

    `seed_demo_relay.py` reads `EMPORIA_RELAY_URL` and defaults to
    `localhost:8088`, so `--local-demo --relay-url http://127.0.0.1:8099`
    started a relay on 8099 and seeded whatever answered on 8088. The seed's own
    split-brain guard caught it and reported a database mismatch — the right
    error for the wrong reason: the databases matched, the *relay* was wrong.
    """
    from pathlib import Path

    text = (Path(__file__).parent.parent / "installer" / "install.py").read_text()
    assert 'seed_env["EMPORIA_RELAY_URL"] = relay_url' in text, (
        "the seed subprocess must be told which relay to seed, not left to "
        "default to localhost:8088"
    )


def test_the_testnet_runner_refuses_a_mainnet_chain_id():
    """P66: `docs/testnet.md` says "never point `EMPORIA_TESTNET_*` at mainnet"
    and nothing enforced it — the harness would configure whatever chain id it
    was handed and sign against it.

    A mistyped or mainnet chain id looks exactly like an intended one at the
    point a transfer gets signed, so the runner keeps an allowlist and exits
    non-zero on anything else. Ethereum and Base mainnet are the two that would
    actually cost someone money.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).parent.parent
    for chain_id in ("eip155:1", "eip155:8453"):
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "testnet.py"),
             "--network", chain_id, "--check"],
            capture_output=True, text=True, cwd=str(root),
        )
        assert result.returncode != 0, f"{chain_id} must be refused, not configured"
        assert "Refusing to configure" in (result.stderr + result.stdout), result.stderr[:200]

    # And a real testnet is still accepted.
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "testnet.py"),
         "--network", "eip155:84532", "--check"],
        capture_output=True, text=True, cwd=str(root),
    )
    assert result.returncode == 0, result.stderr[-400:]


# P92 -- the Hermes platform plugin ------------------------------------------

def test_the_platform_plugin_is_installed_enabled_and_configured(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text("plugins:\n  enabled: [other/plugin]\n")
    (profile / ".env").write_text("EMPORIA_ALLOWED_USERS=trusted_agent\nOTHER=1\n")

    installer.install_platform_plugin(profile)
    installer.install_platform_plugin(profile)   # idempotent

    dst = profile / "plugins" / "platforms" / "emporia"
    assert (dst / "plugin.yaml").exists() and (dst / "adapter.py").exists()
    assert not (dst / "__pycache__").exists()
    config = installer.load_yaml(profile / "config.yaml")
    assert config["plugins"]["enabled"] == ["other/plugin", "platforms/emporia"]
    env = dict(ln.split("=", 1) for ln in (profile / ".env").read_text().splitlines())
    assert env["EMPORIA_ALLOWED_USERS"] == "trusted_agent,emporia:relay", (
        "the operator's own allowlist entries must survive"
    )
    assert env["EMPORIA_PYTHON"] == installer._venv_python()
    assert env["OTHER"] == "1"


def test_hermes_discovers_the_installed_plugin(tmp_path):
    """Placed where Hermes's own discovery finds a profile's plugins, under the
    key `plugins.enabled` names."""
    import json
    import os
    import subprocess

    import pytest

    hermes_python = Path("/opt/hermes/.venv/bin/python")
    if not hermes_python.exists():
        pytest.skip("Hermes is not installed here")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    installer.install_platform_plugin(home)
    out = subprocess.run(
        [str(hermes_python), "-c",
         "import json; from hermes_cli.plugins_discovery import collect_directory_manifests, "
         "manifest_key; print(json.dumps([[manifest_key(m), m.kind] for m in "
         "collect_directory_manifests() if m.source == 'user']))"],
        cwd="/opt/hermes", capture_output=True, text=True, timeout=60,
        env={**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": "/opt/hermes"},
    )
    assert out.returncode == 0, out.stderr[-1500:]
    assert ["platforms/emporia", "platform"] in json.loads(out.stdout.strip().splitlines()[-1])


# P102 -- one key directory and one database per profile ----------------------

def test_the_profile_key_dir_is_always_named_even_before_it_exists(tmp_path):
    """It was named only when the directory already existed, so a first install
    left the .env without it and every non-MCP process used ~/.hermes/keys."""
    env = installer._profile_runtime_env(tmp_path / "fresh_profile")
    assert env["EMPORIA_KEYS_DIR"] == str(tmp_path / "fresh_profile" / "home" / ".hermes" / "keys")
    assert env["EMPORIA_DB_PATH"].startswith(str(tmp_path / "fresh_profile" / "home" / ".hermes"))


def test_the_installer_generates_the_profile_key_where_the_mcp_server_signs_from(tmp_path):
    """It generated into ~/.hermes/keys while the MCP server signed from the
    profile's own key dir -- two different keys under one agent's name."""
    from unittest.mock import patch

    from emporia import identity

    keys_dir = tmp_path / "profile" / "home" / ".hermes" / "keys"
    with patch.object(identity, "KEY_DIR", identity.KEY_DIR):
        pub_hex, path = installer.generate_keypair("p102_agent", keys_dir=keys_dir)
    assert path == str(keys_dir / "p102_agent.priv")
    assert (keys_dir / "p102_agent.priv").exists()
    assert len(bytes.fromhex(pub_hex)) == 32


def test_an_agent_profile_never_gets_the_relays_stripe_settings(tmp_path, monkeypatch):
    """P107: create_profile() copied the relay's STRIPE_SECRET_KEY (live, on the
    deployment that found this), STRIPE_PROFILE_ID and STRIPE_API_VERSION into
    every agent profile. An agent reads none of them — it pays through the relay
    with an SPT or its x402 wallet — so they are never written, and an existing
    agent profile that has them loses them on the next run."""
    import yaml

    for k in ("STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID", "STRIPE_API_VERSION"):
        monkeypatch.delenv(k, raising=False)
    relay = tmp_path / "profiles" / "relay_profile"
    agent = tmp_path / "profiles" / "agent_one"
    for d in (relay, agent):
        d.mkdir(parents=True)
    relay_secrets = ("STRIPE_SECRET_KEY=sk_live_relay_only\nSTRIPE_PROFILE_ID=profile_relay\n"
                     "STRIPE_API_VERSION=2026-04-22.preview\nOPENROUTER_API_KEY=or-shared\n")
    (relay / ".env").write_text(relay_secrets)
    (relay / "config.yaml").write_text(yaml.dump({"env": {"STRIPE_SECRET_KEY": "sk_live_relay_only"}}))
    (agent / "config.yaml").write_text(yaml.dump({"env": {"STRIPE_SECRET_KEY": "sk_live_old_copy",
                                                          "STRIPE_PROFILE_ID": "profile_old"}}))
    (agent / ".env").write_text("STRIPE_SECRET_KEY=sk_live_old_copy\n")
    monkeypatch.setattr(installer, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_install_skill_link", lambda *a, **k: None)

    import os
    from unittest.mock import patch

    # create_profile() exports EMPORIA_AGENT_ID & co. into this process for the
    # installer's later steps; patch.dict puts the suite's environment back.
    with patch.dict(os.environ):
        installer.create_profile(name="agent_one", relay_url="http://127.0.0.1:8088",
                                 current_profile_dir=relay)

    env_text = (agent / ".env").read_text()
    config_env = yaml.safe_load((agent / "config.yaml").read_text())["env"]
    for k in ("STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID", "STRIPE_API_VERSION"):
        assert f"{k}=" not in env_text, k
        assert k not in config_env, k
    assert "sk_live" not in env_text + yaml.dump(config_env)
    assert config_env["EMPORIA_AGENT_ID"] == "agent_one"
    assert "OPENROUTER_API_KEY=or-shared" in env_text, "unrelated settings still inherit"
    assert "OPENROUTER_API_KEY" not in config_env, "P201: secrets only in the .env"
    assert oct((agent / ".env").stat().st_mode)[-3:] == "600" == oct((agent / "config.yaml").stat().st_mode)[-3:]


def test_the_freshest_nous_login_wins(tmp_path, monkeypatch):
    """P108: a fresh login at the data root lost to an expired profile file
    because the profile file was checked first."""
    import os

    profile = tmp_path / "profile"
    profile.mkdir()
    stale, fresh = profile / "auth.json", tmp_path / "data" / "auth.json"
    fresh.parent.mkdir()
    stale.write_text('{"providers": {"nous": {"access_token": "old"}}}')
    fresh.write_text('{"providers": {"nous": {"access_token": "new"}}}')
    # Far-future times: /opt/data/auth.json and ~/auth.json are fixed
    # candidates too, and a real one on this machine must never win here.
    future = 4_000_000_000
    os.utime(stale, (future, future))
    os.utime(fresh, (future + 100, future + 100))
    monkeypatch.setenv("HERMES_DATA_DIR", str(fresh.parent))
    assert installer._find_auth_json(profile) == fresh
    os.utime(fresh, (future - 100, future - 100))
    assert installer._find_auth_json(profile) == stale


def test_each_agent_profile_shares_the_relay_but_owns_its_identity(tmp_path, monkeypatch):
    """P109: one relay, separate identities. create_profile() built the MCP
    entry without the profile dir, so it carried no key dir or .env; Hermes
    gives an MCP server only that `env`, and the server then loaded the .env
    beside its code — the relay profile's keys, Nous token and Stripe secret."""
    import os
    from unittest.mock import patch

    import yaml

    relay = tmp_path / "profiles" / "relay_profile"
    relay.mkdir(parents=True)
    (relay / "config.yaml").write_text("{}")
    monkeypatch.setattr(installer, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_install_skill_link", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_hermes_profile_create", lambda name: None)

    entries = {}
    with patch.dict(os.environ):
        for name in ("alpha", "beta"):
            installer.create_profile(name=name, relay_url="http://127.0.0.1:8088",
                                     current_profile_dir=relay,
                                     x402_pay_networks="eip155:84532", x402_max_payment_cents=500)
            prof = tmp_path / "profiles" / name
            entries[name] = (yaml.safe_load((prof / "config.yaml").read_text())
                             ["mcp_servers"]["emporia"]["env"], (prof / ".env").read_text())

    for name, (mcp_env, dotenv) in entries.items():
        prof = tmp_path / "profiles" / name
        assert mcp_env["EMPORIA_RELAY_URL"] == "http://127.0.0.1:8088"
        assert mcp_env["EMPORIA_AGENT_ID"] == name
        assert mcp_env["EMPORIA_KEYS_DIR"] == str(prof / "home" / ".hermes" / "keys")
        assert mcp_env["EMPORIA_DOTENV"] == str(prof / ".env")
        if shutil.which("hermes"):
            assert mcp_env["EMPORIA_HERMES_BIN"] == shutil.which("hermes")
        assert f"EMPORIA_DOTENV={prof / '.env'}" in dotenv
        assert "EMPORIA_X402_PAY_NETWORKS=eip155:84532" in dotenv
        assert "EMPORIA_X402_MAX_PAYMENT_CENTS=500" in dotenv
        assert (prof / "home" / ".hermes" / "keys" / f"{name}.priv").exists()
    assert entries["alpha"][0]["EMPORIA_KEYS_DIR"] != entries["beta"][0]["EMPORIA_KEYS_DIR"]


def test_emporia_dotenv_names_the_only_env_file_loaded(tmp_path, monkeypatch):
    """P109: with EMPORIA_DOTENV set, that file and nothing else — not the
    .env beside the code, which belongs to whichever profile holds the checkout."""
    import os
    from unittest.mock import patch

    from emporia import config

    own = tmp_path / "agent.env"
    own.write_text("P109_FROM_OWN=yes\n")
    beside_code = []
    monkeypatch.setattr(config, "_merge_dotenv",
                        lambda p, _orig=config._merge_dotenv: (beside_code.append(p), _orig(p)))
    with patch.dict(os.environ, {"EMPORIA_DOTENV": str(own), "EMPORIA_NO_DOTENV": ""}):
        monkeypatch.setattr(config, "_dotenv_loaded", False)
        config.load_profile_dotenv()
        assert os.environ.get("P109_FROM_OWN") == "yes"
    assert beside_code == [own]


def test_re_running_the_installer_keeps_the_operators_own_env_lines(tmp_path):
    """P115: create_profile() wrote the .env whole, dropping anything added by
    hand. Managed keys are replaced, other lines kept, relay Stripe keys never."""
    env = tmp_path / ".env"
    env.write_text("# my note\nMY_OWN=1\nEMPORIA_AGENT_ID=old\nSTRIPE_SECRET_KEY=sk_live_x\n")
    merged = installer._merge_profile_env(env, ["EMPORIA_AGENT_ID=alpha", "EMPORIA_RELAY_URL=u"])
    assert merged == ["EMPORIA_AGENT_ID=alpha", "EMPORIA_RELAY_URL=u", "# my note", "MY_OWN=1"]
    assert installer._merge_profile_env(tmp_path / "absent.env", ["A=1"]) == ["A=1"]


def test_sync_model_makes_an_existing_profile_follow_the_operators_model(tmp_path, monkeypatch):
    """P116: an existing profile kept its own model on re-install; --sync-model
    switches it to the current profile's."""
    import os
    from unittest.mock import patch

    import yaml

    relay = tmp_path / "profiles" / "relay_profile"
    agent = tmp_path / "profiles" / "agent_m"
    for d in (relay, agent):
        d.mkdir(parents=True)
    want = {"provider": "openrouter", "default": "deepseek/deepseek-v4-flash-0731",
            "base_url": "https://openrouter.ai/api/v1"}
    (relay / "config.yaml").write_text(yaml.dump({"model": want}))
    (agent / "config.yaml").write_text(yaml.dump({"model": {"provider": "nvidia", "default": "old"}}))
    monkeypatch.setattr(installer, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_install_skill_link", lambda *a, **k: None)

    def model():
        return yaml.safe_load((agent / "config.yaml").read_text())["model"]
    with patch.dict(os.environ):
        installer.create_profile(name="agent_m", relay_url="http://127.0.0.1:8088",
                                 current_profile_dir=relay)
        assert model()["default"] == "old", "an existing profile keeps its model by default"
        installer.create_profile(name="agent_m", relay_url="http://127.0.0.1:8088",
                                 current_profile_dir=relay, sync_model=True)
        assert model()["default"] == "deepseek/deepseek-v4-flash-0731"
        assert model()["provider"] == "openrouter"


def test_a_new_agent_profile_gets_its_models_key_and_nothing_else(tmp_path, monkeypatch):
    """P117: profiles are created bare (Hermes will not combine --no-skills with
    --clone-from), so the installer copies the model and the one API key its
    provider needs. Without the key Hermes opens with "No inference provider is
    configured". The relay's other secrets stay behind."""
    import os
    from unittest.mock import patch

    import yaml

    for k in ("OPENROUTER_API_KEY", "STRIPE_SECRET_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    relay = tmp_path / "profiles" / "relay_profile"
    agent = tmp_path / "profiles" / "fresh_agent"
    relay.mkdir(parents=True)
    (relay / "config.yaml").write_text(yaml.dump({"model": {
        "provider": "openrouter", "default": "deepseek/deepseek-v4-flash-0731"}}))
    (relay / ".env").write_text("OPENROUTER_API_KEY=sk-or-test\nSTRIPE_SECRET_KEY=sk_live_x\n"
                                "ANTHROPIC_API_KEY=sk-ant-unrelated\n")
    monkeypatch.setattr(installer, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_install_skill_link", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_hermes_profile_create", lambda name: agent.mkdir(parents=True))

    with patch.dict(os.environ):
        installer.create_profile(name="fresh_agent", relay_url="http://127.0.0.1:8088",
                                 current_profile_dir=relay, inherit_env=False)
    dotenv = (agent / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-test" in dotenv
    assert "sk_live_x" not in dotenv and "sk-ant-unrelated" not in dotenv
    assert yaml.safe_load((agent / "config.yaml").read_text())["model"]["provider"] == "openrouter"


def test_the_installer_says_which_restart_a_change_needs(tmp_path, monkeypatch, capsys):
    """P119: a new or changed gateway plugin needs a full `hermes gateway restart`
    (Hermes loads plugins once, in one shared process); an unchanged one needs
    only a per-profile restart and /reload-mcp."""
    monkeypatch.setattr(installer, "_GATEWAY_RESTART_NEEDED", False)
    monkeypatch.setattr(installer, "_TOUCHED_PROFILES", [])
    prof = tmp_path / "agent_r"
    prof.mkdir()
    installer.install_platform_plugin(prof)
    installer.print_restart_instructions(installer._TOUCHED_PROFILES)
    first = capsys.readouterr().out
    assert "RESTART REQUIRED" in first and "hermes gateway restart" in first

    monkeypatch.setattr(installer, "_GATEWAY_RESTART_NEEDED", False)
    installer.install_platform_plugin(prof)                    # same files again
    installer.print_restart_instructions(installer._TOUCHED_PROFILES)
    again = capsys.readouterr().out
    assert "RESTART REQUIRED" not in again
    assert "hermes -p agent_r gateway restart" in again and "/reload-mcp" in again


def test_the_dashboard_tab_installs_with_its_config_out_of_git_and_enabled(tmp_path, monkeypatch):
    """P126: --hermes-dashboard puts the Emporia tab where the Hermes dashboard
    loads user plugins, with its console config, excluded from any git repo it
    lands in (via .git/info/exclude, never a tracked file), and enabled."""
    import json
    import subprocess as sp

    home = tmp_path / "hermes_home"
    home.mkdir()
    sp.run(["git", "init", "-q", str(home)], check=True)
    profile = tmp_path / "profiles" / "op"
    profile.mkdir(parents=True)
    bundle = installer._EMPORIA_DIR / "dashboard" / "dist-plugin"
    if not (bundle / "index.js").exists():
        import pytest
        pytest.skip("dashboard bundle not built (npm run build:hermes-plugin)")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)     # no npm rebuild
    calls = []
    real_run = installer.subprocess.run

    def fake_run(argv, **kw):
        if argv[:3] == ["hermes", "plugins", "enable"]:
            calls.append((argv, kw["env"]["HERMES_HOME"]))
            return sp.CompletedProcess(argv, 0, "", "")
        return real_run(argv, **kw)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    monkeypatch.setattr(installer, "_DASHBOARD_RESTART_NEEDED", False)

    installer.install_dashboard_plugin(profile, "http://relay.example", home)
    dst = home / "plugins" / "emporia"
    assert (dst / "plugin.yaml").exists() and (dst / "dashboard" / "manifest.json").exists()
    assert (dst / "dashboard" / "dist" / "index.js").exists()
    cfg = json.loads((dst / "dashboard" / "emporia.json").read_text())
    assert cfg["relay_url"] == "http://relay.example"
    assert cfg["profiles_root"] == str(profile.parent)
    assert cfg["runtime_dir"] == str(profile / "home" / ".hermes" / "emporia-console")
    assert "/plugins/emporia/" in (home / ".git" / "info" / "exclude").read_text()
    status = real_run(["git", "-C", str(home), "status", "--porcelain"],
                      capture_output=True, text=True).stdout
    assert "plugins" not in status, "the tab is invisible to the repo's git status"
    assert calls == [(["hermes", "plugins", "enable", "emporia"], str(home))]
    assert installer._DASHBOARD_RESTART_NEEDED


def test_secrets_move_out_of_config_yaml_into_the_env(tmp_path):
    """P201: Hermes never reads config.yaml's `env:`; any file tool can."""
    profile = tmp_path / "profiles" / "p201"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(yaml.dump({
        "env": {"EMPORIA_NOUS_JWT": "jwt-in-config", "OPENROUTER_API_KEY": "or-config", "EMPORIA_RELAY_URL": "u",
                "EMPORIA_PUBLIC_KEY": "ab"},
        "mcp_servers": {"emporia": {"env": {"EMPORIA_NOUS_JWT": "jwt-in-config", "EMPORIA_KEYS_DIR": "/k"}},
                        "other": {"env": {"SOME_TOKEN": "${SOME_TOKEN}"}}}}))
    (profile / "config.yaml").chmod(0o644)
    (profile / ".env").write_text("OPENROUTER_API_KEY=or-env\n")
    moved = installer.secure_profile(profile)
    assert moved == ["EMPORIA_NOUS_JWT", "OPENROUTER_API_KEY"]
    cfg = yaml.safe_load((profile / "config.yaml").read_text())
    assert cfg["env"] == {"EMPORIA_RELAY_URL": "u", "EMPORIA_PUBLIC_KEY": "ab"}
    assert cfg["mcp_servers"]["emporia"]["env"] == {"EMPORIA_NOUS_JWT": "${EMPORIA_NOUS_JWT}", "EMPORIA_KEYS_DIR": "/k"}
    env = (profile / ".env").read_text()
    assert "EMPORIA_NOUS_JWT=jwt-in-config" in env and "OPENROUTER_API_KEY=or-env" in env   # the .env's wins
    assert "or-config" not in env
    for f in ("config.yaml", ".env"):
        assert oct((profile / f).stat().st_mode)[-3:] == "600"
    assert installer.secure_profile(profile) == []                    # nothing left


def test_a_docker_key_is_copied_only_for_a_profile_that_cannot_read_it(tmp_path, monkeypatch, capsys):
    """P201: a multiplexed profile reads only its own .env, so a key docker-compose
    gives the container must be copied; one with its own gateway reads it."""
    profiles = tmp_path / "profiles"
    shared, alone = profiles / "shared", profiles / "alone"
    for p, cfg in ((shared, {}), (alone, {"gateway": {"standalone": True}})):
        p.mkdir(parents=True)
        (p / "config.yaml").write_text(yaml.dump(cfg))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-from-compose")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    model = {"provider": "openrouter"}
    assert installer.is_multiplexed(shared) and not installer.is_multiplexed(alone)
    assert installer._provider_key_line(model, profile_dir=shared) == "OPENROUTER_API_KEY=or-from-compose"
    assert "each profile here reads only its own .env" in capsys.readouterr().out
    assert installer._provider_key_line(model, profile_dir=alone) is None
    assert "nothing copied" in capsys.readouterr().out
    assert installer._provider_key_line(model, {"OPENROUTER_API_KEY": "or-file"},
                                        profile_dir=shared) == "OPENROUTER_API_KEY=or-file"   # a .env first


def test_in_a_terminal_the_person_picks_the_standard_key_or_another(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "asked"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-standard")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    model = {"provider": "openrouter"}
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert installer._provider_key_line(model, profile_dir=profile) == "OPENROUTER_API_KEY=or-standard"
    monkeypatch.setattr("builtins.input", lambda *a: "o")
    monkeypatch.setattr("getpass.getpass", lambda *a: "or-its-own")
    assert installer._provider_key_line(model, profile_dir=profile) == "OPENROUTER_API_KEY=or-its-own"
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert installer._provider_key_line(model, profile_dir=profile) is None
