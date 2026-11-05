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
    store[str(profile)] = {"EMPORIA_SIGNER_URL": "unix:/s", "STRIPE_PROFILE_ID": "profile_test_abc"}
    (profile / ".env").write_text("")          # the file Hermes keeps (its content is Hermes's to read)
    from emporia import cli, key_source
    from emporia.stripe_profile_discovery import scan_stripe_profile_id_in_tree
    assert cli.installer().harvest_env(profile)["EMPORIA_SIGNER_URL"] == "unix:/s"
    key_source.set_env_lines(profile / ".env", {"EMPORIA_KEY_STORAGE": "env"})
    key_source.unset_env_lines(profile / ".env", ["EMPORIA_SIGNER_URL"])
    assert cli.merge_env_file(profile / ".env", {"EMPORIA_PLAYBOOK": "host"}) == ["EMPORIA_PLAYBOOK"]
    assert store[str(profile)] == {"STRIPE_PROFILE_ID": "profile_test_abc", "EMPORIA_KEY_STORAGE": "env",
                                   "EMPORIA_PLAYBOOK": "host"}
    assert not (profile / ".env-backups").exists(), "no copy of a Hermes profile's .env"
    assert cli._dotenv_names(str(profile / ".env")) == set(store[str(profile)])
    assert scan_stripe_profile_id_in_tree(profile) == "profile_test_abc"


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
    """The real route: Hermes's own load_env_file and save_env_value/remove_env_value, for a
    throwaway profile; values on stdin and a pipe, never argv."""
    monkeypatch.setenv("EMPORIA_HERMES_BIN", HERMES)
    hermes_settings.forget()
    assert hermes_settings.available()
    hermes_settings.write(profile, {"EMPORIA_AGENT_ID": "zed", "EMPORIA_T_SECRET": "two words"})
    assert hermes_settings.read(profile, fresh=True) == {"EMPORIA_AGENT_ID": "zed", "EMPORIA_T_SECRET": "two words"}
    hermes_settings.write(profile, unset=["EMPORIA_T_SECRET"])
    assert hermes_settings.read(profile, fresh=True) == {"EMPORIA_AGENT_ID": "zed"}
    assert oct((profile / ".env").stat().st_mode)[-3:] == "600"
    hermes_settings.forget()


def _stub_hermes(tmp_path, monkeypatch, *, blocked=()):
    """A Hermes whose env writer refuses the names in `blocked` as Hermes's does: it says why
    on stderr and returns, the call itself succeeding."""
    root = tmp_path / "stub-hermes"
    (root / "agent").mkdir(parents=True)
    (root / "hermes_cli").mkdir()
    (root / "agent" / "__init__.py").write_text("")
    (root / "hermes_cli" / "__init__.py").write_text("")
    (root / "agent" / "secret_scope.py").write_text(
        "import json\n"
        "def load_env_file(p):\n"
        "    try:\n"
        "        return json.loads(p.read_text())\n"
        "    except OSError:\n"
        "        return {}\n")
    (root / "hermes_cli" / "config.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"BLOCKED = {sorted(blocked)!r}\n"
        "def get_env_path():\n"
        "    return Path(os.environ['HERMES_HOME']) / '.env'\n"
        "def _load():\n"
        "    p = get_env_path()\n"
        "    return json.loads(p.read_text()) if p.exists() else {}\n"
        "def save_env_value(k, v):\n"
        "    if k in BLOCKED:\n"
        "        print(f'{k}: this .env is locked', file=sys.stderr)\n"
        "        return\n"
        "    d = _load(); d[k] = v; get_env_path().write_text(json.dumps(d))\n"
        "def remove_env_value(k):\n"
        "    if k in BLOCKED:\n"
        "        return False\n"
        "    d = _load(); d.pop(k, None); get_env_path().write_text(json.dumps(d))\n"
        "    return True\n")
    monkeypatch.setattr(hermes_settings, "_hermes", lambda: (Path(sys.executable), str(root)))
    hermes_settings.forget()


def test_a_write_hermes_refused_is_a_failure_that_names_the_setting_not_its_value(profile, tmp_path,
                                                                                  monkeypatch):
    """Found integrating the catalog work: Hermes's save_env_value skips a write its .env lock
    refuses (it prints why and returns), so a spending limit or a key that never landed was
    reported as written. Each write is read back; one that did not land fails, by name."""
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


def test_a_write_is_judged_against_what_the_profile_holds_now(profile, monkeypatch):
    """A value read earlier in the process (cached) may have changed since: a set judged
    "already so" from the cache was skipped, and the profile kept the other value."""
    held = {"EMPORIA_PLAYBOOK": "host"}
    writes = []
    monkeypatch.setattr(hermes_settings, "available", lambda: True)
    monkeypatch.setattr(hermes_settings, "_run", lambda code, home, *a, stdin="": (
        json.dumps(held) if not stdin else writes.append(json.loads(stdin)) or ""))
    hermes_settings.forget()
    assert profile_env.read(profile / ".env") == {"EMPORIA_PLAYBOOK": "host"}     # now cached
    held["EMPORIA_PLAYBOOK"] = "player"                                           # changed elsewhere
    assert profile_env.update(profile / ".env", {"EMPORIA_PLAYBOOK": "host"}) == ["EMPORIA_PLAYBOOK"]
    assert writes == [{"set": {"EMPORIA_PLAYBOOK": "host"}, "unset": []}]
    hermes_settings.forget()
