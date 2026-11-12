"""Catalog rule 11: a Hermes profile's .env is read and written through Hermes, never opened
by Emporia, whenever Hermes is installed; a catalog runtime with no Hermes fails closed."""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from emporia import hermes_settings, profile_env

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "profiles" / "zed"
    p.mkdir(parents=True)
    (p / "config.yaml").write_text("model: {}\n")
    return p


@pytest.fixture
def through_hermes(monkeypatch):
    """A Hermes that keeps each profile's settings in a dict: what reached it, and how."""
    store: dict[str, dict[str, str]] = {}
    calls = []

    def read(home, fresh=False):
        calls.append(("read", Path(home).name))
        return dict(store.get(str(home), {}))

    def write(home, updates=None, unset=()):
        calls.append(("write", Path(home).name, dict(updates or {}), list(unset)))
        s = store.setdefault(str(home), {})
        s.update(updates or {})
        for n in unset:
            s.pop(n, None)

    monkeypatch.setattr(hermes_settings, "available", lambda: True)
    monkeypatch.setattr(hermes_settings, "read", read)
    monkeypatch.setattr(hermes_settings, "write", write)
    return store, calls


def test_a_plain_folders_env_is_a_file(tmp_path):
    env = tmp_path / "relay" / ".env"
    env.parent.mkdir()
    env.write_text("# a relay's own settings\nEMPORIA_RELAY_ACCESS=open\nexport A=\"1\"\n")
    assert profile_env.read(env) == {"EMPORIA_RELAY_ACCESS": "open", "A": "1"}
    assert profile_env.update(env, {"EMPORIA_RELAY_ACCESS": "closed"}, unset=["A"]) == ["EMPORIA_RELAY_ACCESS", "A"]
    assert env.read_text() == "# a relay's own settings\nEMPORIA_RELAY_ACCESS=closed\n"
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_a_profiles_env_goes_through_hermes_and_is_never_opened(profile, through_hermes, monkeypatch):
    store, calls = through_hermes
    (profile / ".env").write_text("SENTINEL=never-read\n")
    real_read_text = Path.read_text

    def guarded(self, *a, **k):
        assert self.name != ".env", f"Emporia opened {self}"
        return real_read_text(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", guarded)
    store[str(profile)] = {"EMPORIA_AGENT_ID": "zed", "OLD": "x"}
    assert profile_env.read(profile / ".env") == {"EMPORIA_AGENT_ID": "zed", "OLD": "x"}
    assert profile_env.update(profile / ".env", {"EMPORIA_AGENT_ID": "zed", "NEW": "1"}, unset=["OLD"]) == ["NEW", "OLD"]
    assert store[str(profile)] == {"EMPORIA_AGENT_ID": "zed", "NEW": "1"}
    assert calls[-1] == ("write", "zed", {"NEW": "1"}, ["OLD"])         # only what changed
    assert profile_env.replace(profile / ".env", ["# kept by Hermes", "NEW=2"]) == ["NEW", "EMPORIA_AGENT_ID"]
    assert store[str(profile)] == {"NEW": "2"}
    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert (profile / ".env").read_text() == "SENTINEL=never-read\n", "the file itself was not touched"


def test_the_runtimes_readers_and_writers_go_through_it(profile, through_hermes):
    store, _ = through_hermes
    store[str(profile)] = {"EMPORIA_SIGNER_URL": "unix:/s", "EMPORIA_PLAYBOOK": "scout"}
    (profile / ".env").write_text("")          # the file Hermes keeps (its content is Hermes's to read)
    from emporia import cli, key_source
    assert cli.installer().harvest_env(profile)["EMPORIA_SIGNER_URL"] == "unix:/s"
    key_source.set_env_lines(profile / ".env", {"EMPORIA_KEY_STORAGE": "env"})
    key_source.unset_env_lines(profile / ".env", ["EMPORIA_SIGNER_URL"])
    assert cli.merge_env_file(profile / ".env", {"EMPORIA_PLAYBOOK": "host"}) == ["EMPORIA_PLAYBOOK"]
    assert store[str(profile)] == {"EMPORIA_KEY_STORAGE": "env", "EMPORIA_PLAYBOOK": "host"}
    assert not (profile / ".env-backups").exists(), "no copy of a Hermes profile's .env"
    assert cli._dotenv_names(str(profile / ".env")) == set(store[str(profile)])


def test_a_catalog_runtime_without_hermes_reads_and_writes_nothing(profile, monkeypatch):
    (profile / ".env").write_text("EMPORIA_AGENT_ID=zed\n")
    monkeypatch.setattr(hermes_settings, "available", lambda: False)
    monkeypatch.setattr(profile_env, "_from_catalog", lambda: True)
    with pytest.raises(profile_env.NoRoute):
        profile_env.read(profile / ".env")
    with pytest.raises(profile_env.NoRoute):
        profile_env.update(profile / ".env", {"X": "1"})
    assert (profile / ".env").read_text() == "EMPORIA_AGENT_ID=zed\n"


def test_no_module_opens_a_profiles_env_but_profile_env():
    """The one place: every other module asks profile_env (or reads a relay's own folder)."""
    opener = re.compile(r"""(\.env['"]\s*\)?\.(?:read_text|read_bytes|write_text|open)\b"""
                        r"""|(?:env_file|env_path|dotenv_file)\.(?:read_text|read_bytes|write_text)\b)""")
    offenders = []
    for path in [*sorted((REPO / "src" / "emporia").rglob("*.py")), REPO / "installer" / "install.py",
                 *sorted((REPO / "hermes_plugin" / "emporia").rglob("*.py"))]:
        if path.name == "profile_env.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if opener.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    # The signer's compose folder writes its own .env once (not a Hermes profile's).
    offenders = [o for o in offenders if not o.startswith("src/emporia/signer_host.py")]
    assert not offenders, "\n".join(offenders)


HERMES = shutil.which("hermes")


@pytest.mark.skipif(not HERMES or not Path("/opt/hermes/.venv/bin/python").exists(),
                    reason="Hermes is not installed here")
def test_through_the_installed_hermes_for_real(profile, monkeypatch):
    """The real route, for a throwaway profile: only declared names are read (a provider key in the
    same .env never reaches Emporia), a setting is written by `hermes config set`, a secret's value
    goes on stdin."""
    monkeypatch.setenv("EMPORIA_HERMES_BIN", HERMES)
    hermes_settings.forget()
    assert hermes_settings.available()
    (profile / ".env").write_text("OPENROUTER_API_KEY=sk-sentinel-never-read\nEMPORIA_AGENT_ID=zed\n")
    assert hermes_settings.read(profile, fresh=True) == {"EMPORIA_AGENT_ID": "zed"}
    hermes_settings.write(profile, {"EMPORIA_PLAYBOOK": "scout", "EMPORIA_SIGNER_TOKEN": "two words"})
    assert hermes_settings.read(profile, fresh=True) == {"EMPORIA_AGENT_ID": "zed", "EMPORIA_PLAYBOOK": "scout",
                                                         "EMPORIA_SIGNER_TOKEN": "two words"}
    hermes_settings.write(profile, unset=["EMPORIA_SIGNER_TOKEN", "EMPORIA_PLAYBOOK"])
    assert hermes_settings.read(profile, fresh=True) == {"EMPORIA_AGENT_ID": "zed"}
    assert "OPENROUTER_API_KEY=sk-sentinel-never-read" in (profile / ".env").read_text()   # left alone
    hermes_settings.forget()


def _stub_hermes(tmp_path, monkeypatch, *, blocked=()):
    """A Hermes whose `config set` refuses the names in `blocked` as its .env lock does (it says why
    and exits 0 without writing), whose secret scope holds the profile's JSON .env, and which records
    every argv it was started with."""
    root = tmp_path / "stub-hermes"
    (root / "agent").mkdir(parents=True)
    (root / "hermes_cli").mkdir()
    (root / "agent" / "__init__.py").write_text("")
    (root / "hermes_cli" / "__init__.py").write_text("")
    (root / "agent" / "secret_scope.py").write_text(
        "import json\n"
        "_S = {}\n"
        "def build_profile_secret_scope(home):\n"
        "    try:\n"
        "        return json.loads((home / '.env').read_text())\n"
        "    except OSError:\n"
        "        return {}\n"
        "def set_secret_scope(m, profile_home=None):\n"
        "    _S.update(m)\n"
        "def get_secret(n, d=None):\n"
        "    return _S.get(n, d)\n"
        "def reset_secret_scope(t):\n"
        "    _S.clear()\n")
    (root / "hermes_cli" / "config.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "def _p():\n"
        "    return Path(os.environ['HERMES_HOME']) / '.env'\n"
        "def _load():\n"
        "    return json.loads(_p().read_text()) if _p().exists() else {}\n"
        "def save_env_value(k, v):\n"
        f"    if k in {sorted(blocked)!r}:\n"
        "        return\n"
        "    d = _load(); d[k] = v; _p().write_text(json.dumps(d))\n"
        "def remove_env_value(k):\n"
        f"    if k in {sorted(blocked)!r}:\n"
        "        return False\n"
        "    d = _load(); d.pop(k, None); _p().write_text(json.dumps(d)); return True\n")
    launcher = tmp_path / "hermes"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        f"log = {str(tmp_path / 'argv.jsonl')!r}\n"
        "open(log, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "from hermes_cli.config import save_env_value, remove_env_value\n"
        "a = sys.argv[1:]\n"
        "a = a[2:] if a[:1] == ['-p'] else a\n"
        "a = a[:2] + a[3:] if a[2:3] == ['--'] else a\n"
        "if a[:2] == ['config', 'set']:\n"
        "    save_env_value(a[2], a[3])\n"
        "elif a[:2] == ['config', 'unset']:\n"
        "    sys.exit(0 if remove_env_value(a[2]) else 1)\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("EMPORIA_HERMES_BIN", str(launcher))
    monkeypatch.setattr(hermes_settings, "_hermes", lambda: (Path(sys.executable), str(root)))
    hermes_settings.forget()
    return tmp_path / "argv.jsonl"


def test_a_write_hermes_refused_is_a_failure_that_names_the_setting_not_its_value(profile, tmp_path,
                                                                                  monkeypatch):
    """Found integrating the catalog work: a write Hermes skips (its .env lock refuses it) was
    reported as written, so a spending limit or a key that never landed went unnoticed. Each write
    is read back; one that did not land fails, by name."""
    _stub_hermes(tmp_path, monkeypatch, blocked={"EMPORIA_X402_MAX_DAILY_CENTS", "EMPORIA_SIGNER_TOKEN"})
    hermes_settings.write(profile, {"EMPORIA_AGENT_ID": "zed"})
    assert hermes_settings.read(profile, fresh=True) == {"EMPORIA_AGENT_ID": "zed"}
    with pytest.raises(hermes_settings.SettingsUnavailable) as caught:
        hermes_settings.write(profile, {"EMPORIA_X402_MAX_DAILY_CENTS": "300",
                                        "EMPORIA_SIGNER_TOKEN": "tok-sentinel-not-shown"})
    msg = str(caught.value)
    assert "EMPORIA_SIGNER_TOKEN" in msg and "EMPORIA_X402_MAX_DAILY_CENTS" in msg
    assert "tok-sentinel-not-shown" not in msg and "300" not in msg
    (profile / ".env").write_text('{"EMPORIA_SIGNER_TOKEN": "x"}')     # (the stub's .env is JSON)
    with pytest.raises(hermes_settings.SettingsUnavailable, match="did not take EMPORIA_SIGNER_TOKEN"):
        hermes_settings.write(profile, unset=["EMPORIA_SIGNER_TOKEN"])     # a removal refused, too
    hermes_settings.forget()


def test_settings_are_written_by_hermes_config_set_and_anything_else_never_goes_in_argv(profile, tmp_path, monkeypatch):
    """Review: write with `hermes -p <profile> config set -- NAME VALUE`, not Hermes's internals; the child
    gets a minimal environment. Only a name on the explicit PLAIN_SETTINGS list goes in argv (readable by any
    process on the machine): keys, tokens, URLs that may carry a provider key, DSNs, sessions, key-source
    references and any name nobody listed go on stdin."""
    log = _stub_hermes(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-sentinel-not-handed")
    seen = []
    real = hermes_settings.subprocess.run
    monkeypatch.setattr(hermes_settings.subprocess, "run",
                        lambda *a, **k: seen.append(k.get("env", {})) or real(*a, **k))
    held = {"EMPORIA_PLAYBOOK": "scout", "EMPORIA_IDENTITY_PRIVATE_KEY": "ab" * 32,
            "EMPORIA_X402_RPC_URL": "https://rpc.example/v2/apikey-sentinel", "EMPORIA_IDENTITY_KEY_SOURCE": "cmd:op read x",
            "SENTRY_DSN": "https://dsn-sentinel@sentry.example/1", "BW_SESSION": "bw-sentinel",
            "EMPORIA_SOMETHING_NEW": "unknown-sentinel", "EMPORIA_SIGNER_URL": "https://signer.example/k-sentinel"}
    hermes_settings.write(profile, held)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["-p", "zed", "config", "set", "--", "EMPORIA_PLAYBOOK", "scout"] in calls
    argv = " ".join(" ".join(c) for c in calls)
    assert all(v not in argv for k, v in held.items() if k != "EMPORIA_PLAYBOOK"), "a non-plain value reached argv"
    assert all("--" in c for c in calls if "set" in c), "NAME and VALUE come after `--`"
    assert seen and all("OPENROUTER_API_KEY" not in e and e.get("HERMES_HOME") == str(profile) for e in seen)
    got = hermes_settings.read(profile, fresh=True)
    assert got["EMPORIA_IDENTITY_PRIVATE_KEY"] == "ab" * 32 and got["EMPORIA_X402_RPC_URL"].endswith("apikey-sentinel")
    hermes_settings.forget()


def test_an_unchanged_value_is_not_written_again_and_a_failed_write_drops_the_cache(profile, tmp_path, monkeypatch):
    log = _stub_hermes(tmp_path, monkeypatch, blocked={"EMPORIA_WAKE"})
    hermes_settings.write(profile, {"EMPORIA_PLAYBOOK": "scout"})
    hermes_settings.write(profile, {"EMPORIA_PLAYBOOK": "scout"})          # already so: no second `hermes` launch
    assert sum('"set"' in line for line in log.read_text().splitlines()) == 1
    hermes_settings.read(profile, fresh=True)
    with pytest.raises(hermes_settings.SettingsUnavailable):
        hermes_settings.write(profile, {"EMPORIA_WAKE": "open"})
    assert not hermes_settings._cache, "a write that failed partway left a stale cache"
    hermes_settings.forget()


def test_a_root_home_is_written_with_dash_p_default_whatever_the_sticky_profile(tmp_path, monkeypatch):
    """B2: without a selector `hermes config set` follows the sticky `active_profile` file, so a write meant for
    the root could land in another profile. Always `-p default` for a root home, as Hermes's own gateway does."""
    root = tmp_path / "root"
    (root / "profiles" / "zed").mkdir(parents=True)
    (root / "config.yaml").write_text("model: {}\n")
    (root / "profiles" / "zed" / "config.yaml").write_text("model: {}\n")
    (root / "active_profile").write_text("zed\n")
    log = _stub_hermes(tmp_path, monkeypatch)
    hermes_settings.write(root, {"EMPORIA_PLAYBOOK": "scout"})
    assert ["-p", "default", "config", "set", "--", "EMPORIA_PLAYBOOK", "scout"] in [
        json.loads(line) for line in log.read_text().splitlines()]
    hermes_settings.forget()


@pytest.mark.skipif(not HERMES or not Path("/opt/hermes/.venv/bin/python").exists(),
                    reason="Hermes is not installed here")
def test_with_a_sticky_active_profile_a_root_write_stays_in_the_root_for_real(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "profiles" / "zed").mkdir(parents=True)
    (root / "config.yaml").write_text("model: {}\n")
    (root / "profiles" / "zed" / "config.yaml").write_text("model: {}\n")
    (root / "active_profile").write_text("zed\n")
    monkeypatch.setenv("EMPORIA_HERMES_BIN", HERMES)
    hermes_settings.forget()
    hermes_settings.write(root, {"EMPORIA_PLAYBOOK": "scout"})
    assert "EMPORIA_PLAYBOOK=scout" in (root / ".env").read_text()
    assert not (root / "profiles" / "zed" / ".env").exists() or "EMPORIA_PLAYBOOK" not in (root / "profiles" / "zed" / ".env").read_text()
    hermes_settings.forget()


def test_only_the_vault_a_key_source_names_is_asked_for_and_env_links_only_from_key_sources(profile, monkeypatch):
    """S2: not every vault variable, and `env:NAME` is followed only from the five *_SOURCE settings."""
    asked = []
    monkeypatch.setattr(hermes_settings, "_run", lambda code, home, *a, stdin="": (
        asked.append(json.loads(stdin)) or json.dumps({"EMPORIA_IDENTITY_KEY_SOURCE": "cmd:op read op://v/i/f"})
        if len(asked) == 0 else asked.append(json.loads(stdin)) or "{}"))
    hermes_settings.read(profile, fresh=True)
    assert len(asked) == 2
    names, second = set(asked[0]["names"]), set(asked[1]["names"])
    assert asked[0]["sources"] == list(hermes_settings.SOURCE_NAMES)
    assert not any(n.startswith(("OP_", "BW_", "AWS_")) for n in names)
    assert second and all(n.startswith("OP_") for n in second), "only the 1Password family"
    hermes_settings.forget()


def test_a_write_is_judged_against_what_the_profile_holds_now(profile, monkeypatch):
    """A value read earlier in the process (cached) may have changed since: a set judged
    "already so" from the cache was skipped, and the profile kept the other value."""
    held = {"EMPORIA_PLAYBOOK": "host"}
    writes = []
    monkeypatch.setattr(hermes_settings, "available", lambda: True)
    monkeypatch.setattr(hermes_settings, "_fetch", lambda home, names, vault=True: dict(held))
    monkeypatch.setattr(hermes_settings, "_hermes_config", lambda home, *a: writes.append(a) or type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    hermes_settings.forget()
    assert profile_env.read(profile / ".env") == {"EMPORIA_PLAYBOOK": "host"}     # now cached
    held["EMPORIA_PLAYBOOK"] = "player"                                           # changed elsewhere
    held_after = {"EMPORIA_PLAYBOOK": "host"}
    monkeypatch.setattr(hermes_settings, "_fetch", lambda home, names, vault=True: dict(held) if not writes else held_after)
    assert profile_env.update(profile / ".env", {"EMPORIA_PLAYBOOK": "host"}) == ["EMPORIA_PLAYBOOK"]
    assert writes == [("set", "--", "EMPORIA_PLAYBOOK", "host")]
    hermes_settings.forget()


def test_the_runtime_asks_hermes_for_declared_names_only():
    """Review: the profile's whole .env is never read into the runtime: what Hermes is asked for is
    the declared list (settings, keys, vault variables), which holds no provider key."""
    names = set(hermes_settings.declared_names())
    assert {"EMPORIA_AGENT_ID", "EMPORIA_X402_MAINNET", "EMPORIA_IDENTITY_PRIVATE_KEY"} <= names
    from emporia import agent_env
    assert not set(agent_env.VAULT_NAMES) & names, "vault variables only for a linked vault"
    assert not {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "HERMES_YOLO_MODE"} & names
    src = Path(hermes_settings.__file__).read_text()
    assert "load_env_file" not in src


def test_relay_urls_and_labels_are_not_plain_settings():
    """S5: a URL may carry credentials, and the label is an identity: stdin, not argv."""
    for name in ("EMPORIA_RELAY_URL", "EMPORIA_BACKUP_RELAYS", "EMPORIA_VISIT_RELAYS", "EMPORIA_OPERATOR_LABEL",
                 "EMPORIA_X402_RPC_URL", "EMPORIA_SIGNER_URL", "SENTRY_DSN"):
        assert name not in hermes_settings.PLAIN_SETTINGS, name


def test_an_unset_fails_unless_hermes_says_the_name_is_not_set(profile, monkeypatch, capsys):
    """N1: only "Config key not set" with the name still readable means a secret source supplies it; any other
    non-zero exit fails the write, by name only."""
    held = {"EMPORIA_PLAYBOOK": "x"}
    monkeypatch.setattr(hermes_settings, "_fetch", lambda home, names, vault=True: dict(held))

    def config(code, text):
        return lambda home, *a: type("R", (), {"returncode": code, "stdout": "", "stderr": text})()
    monkeypatch.setattr(hermes_settings, "_hermes_config", config(2, "Error: could not write config.yaml"))
    with pytest.raises(hermes_settings.SettingsUnavailable, match="could not unset EMPORIA_PLAYBOOK"):
        hermes_settings.write(profile, unset=["EMPORIA_PLAYBOOK"])
    monkeypatch.setattr(hermes_settings, "_hermes_config", config(1, "Config key not set: EMPORIA_PLAYBOOK"))
    hermes_settings.write(profile, unset=["EMPORIA_PLAYBOOK"])
    assert "secret sources" in capsys.readouterr().err
    held.clear()
    hermes_settings.write(profile, unset=["EMPORIA_PLAYBOOK"])


def test_the_visit_deny_list_goes_on_stdin_too():
    assert "EMPORIA_VISIT_DENY" not in hermes_settings.PLAIN_SETTINGS
