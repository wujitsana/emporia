"""Updates and notices (2.1): the catalog look, `hermes emporia update` naming the plugin command, the notices
list, the daily message and the opt-in job. Nothing here starts Hermes, a gateway or a model."""
import importlib.util
import json
import os
import sys
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "hermes_plugin" / "emporia"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


notices = _load("emporia_notices_t", "notices.py")
setup_cli = _load("emporia_setup_cli_n", "setup_cli.py")


def _fake_hermes_catalog(monkeypatch, sha, version="2.1.0", fresh=True, tmp=None):
    """A stand-in for hermes_cli.plugin_catalog: one entry, offline, a cache file whose age we choose."""
    cache = tmp / "plugin-catalog.json"
    cache.write_text("{}")
    if not fresh:
        old = time.time() - 10 ** 6
        os.utime(cache, (old, old))
    entry = types.SimpleNamespace(name="emporia", sha=sha, version=version)
    pc = types.ModuleType("hermes_cli.plugin_catalog")
    pc._live_cache_path = lambda: cache
    pc.LIVE_CATALOG_TTL_SECONDS = 3600
    pc.load_catalog_live = lambda: [entry]
    pc.load_catalog = lambda: [types.SimpleNamespace(name="emporia", sha="c" * 40, version="2.0.1")]
    pkg = types.ModuleType("hermes_cli")
    pkg.plugin_catalog = pc
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_catalog", pc)


def _install_record(home, *, sha, pin=None, pinned=True):
    d = home / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    row = {"pinned": pinned, "revision": sha,
           "catalog": {"name": "emporia", "sha": sha, "pin": sha if pin is None else pin, "tier": "community"}}
    (d / ".install-metadata.json").write_text(json.dumps({"emporia": row}))


# ── item 3: update prints the plugin command first and never runs it ───────────────────────────────────────────

def test_the_catalog_look_names_a_newer_pin_and_ignores_the_same_one_and_a_ref_hold(monkeypatch, tmp_path):
    _fake_hermes_catalog(monkeypatch, "b" * 40, tmp=tmp_path)
    _install_record(tmp_path, sha="a" * 40)
    assert notices.catalog_newer(tmp_path) == {"sha": "b" * 40, "version": "2.1.0"}
    _install_record(tmp_path, sha="b" * 40)
    assert notices.catalog_newer(tmp_path) is None                       # already at the catalog's pin
    _install_record(tmp_path, sha="a" * 40, pin="")                      # pinned, empty catalog pin: held with --ref
    assert notices.catalog_newer(tmp_path) is None                       # theirs, not "behind"


def test_the_catalog_look_without_hermes_or_a_record_is_silent(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)                  # import fails
    _install_record(tmp_path, sha="a" * 40)
    assert notices.catalog_newer(tmp_path) is None
    assert notices.catalog_newer(tmp_path / "nowhere") is None


def test_a_stale_cache_falls_back_to_the_catalog_that_ships_with_hermes(monkeypatch, tmp_path):
    _fake_hermes_catalog(monkeypatch, "b" * 40, fresh=False, tmp=tmp_path)
    _install_record(tmp_path, sha="a" * 40)
    assert notices.catalog_newer(tmp_path)["sha"] == "c" * 40            # in-tree entry, no fetch


def _update_env(monkeypatch, calls):
    monkeypatch.setattr(setup_cli, "runtime", lambda: Path("/rt"))
    monkeypatch.setattr(setup_cli, "from_catalog", lambda rt: True)
    monkeypatch.setattr(setup_cli, "_state", lambda rt: ("a", "a"))
    monkeypatch.setattr(setup_cli, "install_runtime", lambda target=None: calls.append("runtime") or Path("/rt"))
    monkeypatch.setattr(setup_cli, "source", lambda: {"sha": "a" * 40})
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda *a, **k: calls.append("subprocess"))


def test_update_prints_the_plugin_command_first_then_updates_only_the_runtime(monkeypatch, capsys):
    calls = []
    _update_env(monkeypatch, calls)
    monkeypatch.setattr(setup_cli, "_catalog_notice", lambda: "A newer Emporia plugin is in the catalog: "
                        "hermes -p shop plugins update emporia")
    assert setup_cli.cmd_update(types.SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "A newer Emporia plugin is in the catalog: hermes -p shop plugins update emporia"
    assert "nothing to do" in out and calls == ["runtime"]               # the plugin command is printed, never run


def test_update_says_nothing_extra_when_the_plugin_is_current(monkeypatch, capsys):
    _update_env(monkeypatch, [])
    monkeypatch.setattr(setup_cli, "_catalog_notice", lambda: "")
    assert setup_cli.cmd_update(types.SimpleNamespace()) == 0
    assert "newer Emporia plugin" not in capsys.readouterr().out


def test_the_catalog_notice_is_the_command_for_this_profile(monkeypatch, tmp_path):
    _fake_hermes_catalog(monkeypatch, "b" * 40, tmp=tmp_path)
    home = tmp_path / "profiles" / "shop"
    _install_record(home, sha="a" * 40)
    monkeypatch.setattr(setup_cli, "hermes_home", lambda: home)
    monkeypatch.setattr(setup_cli, "HERE", PLUGIN)
    assert setup_cli._catalog_notice() == "A newer Emporia plugin is in the catalog: hermes -p shop plugins update emporia"
    assert notices.plugin_update_command(tmp_path) == "hermes plugins update emporia"
    assert notices.runtime_update_command(home) == "hermes -p shop emporia update"


# ── item 4: a plugin that moved ahead of its runtime ───────────────────────────────────────────────────────────

def _moved_world(tmp_path, *, head, pinned, plugin_version="2.1.0", checkout=False):
    """A profile with its plugin files and a runtime clone under plugin-data/emporia/runtime at `head`."""
    prof = tmp_path / "prof"
    plugin = prof / "plugins" / "emporia"
    plugin.mkdir(parents=True)
    (plugin / "emporia_source.json").write_text(json.dumps({"sha": pinned}))
    (plugin / "plugin.yaml").write_text(f"name: emporia\nversion: {plugin_version}\n")
    root = tmp_path / ("checkout" if checkout else "prof/plugin-data/emporia/runtime")
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(head + "\n")
    (prof / ".env").write_text("")
    return prof, root


def test_whats_next_gets_a_notice_when_the_pin_moved_and_the_runtime_did_not(tmp_path):
    from emporia import plugin_notice
    prof, root = _moved_world(tmp_path, head="b" * 40, pinned="a" * 40)
    line = plugin_notice.stale_notice(str(prof / ".env"), root)
    assert line.startswith("Emporia's plugin moved to 2.1.0 (aaaaaaaaaaaa): run `hermes emporia update`")
    assert "\n" not in line and "http" not in line


def test_no_notice_when_current_a_checkout_a_newer_runtime_or_nothing_readable(tmp_path):
    from emporia import plugin_notice
    prof, root = _moved_world(tmp_path, head="a" * 40, pinned="a" * 40)
    assert plugin_notice.stale_notice(str(prof / ".env"), root) == ""                       # at the pin
    prof, root = _moved_world(tmp_path / "x", head="b" * 40, pinned="a" * 40, checkout=True)
    assert plugin_notice.stale_notice(str(prof / ".env"), root) == ""                       # a checkout
    prof, root = _moved_world(tmp_path / "y", head="b" * 40, pinned="a" * 40, plugin_version="2.0.1")
    assert plugin_notice.stale_notice(str(prof / ".env"), root) == ""                       # runtime newer than plugin
    assert plugin_notice.stale_notice("", root) == ""                                       # no profile named
    assert plugin_notice.stale_notice(str(tmp_path / "none" / ".env"), root) == ""


def test_whats_next_carries_the_notice_field(monkeypatch):
    from emporia import mcp_server, plugin_notice
    src = Path(mcp_server.__file__).read_text()
    assert 'out["notice"] = line' in src and "plugin_notice.stale_notice()" in src
    monkeypatch.delenv("EMPORIA_DOTENV", raising=False)
    assert plugin_notice.stale_notice() == ""


def test_status_and_doctor_say_the_plugin_moved(monkeypatch, tmp_path, capsys):
    notice = "Emporia's plugin moved to 2.1.0 (aaaaaaaaaaaa): run `hermes emporia update`, then restart the gateway."
    monkeypatch.setattr(setup_cli, "runtime", lambda: tmp_path)
    ran = []
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: ran.append(a) or 0)
    monkeypatch.setattr(setup_cli, "_stale_notice", lambda: notice)
    monkeypatch.setattr(setup_cli, "_catalog_notice", lambda: "")
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "default")
    assert setup_cli.cmd_doctor(types.SimpleNamespace(all=False)) == 0
    out = capsys.readouterr().out
    assert out.startswith("Notice: Emporia's plugin moved to") and ran == [("doctor",)]
    monkeypatch.setattr(setup_cli, "_stale_notice", lambda: "")
    setup_cli.cmd_doctor(types.SimpleNamespace(all=False))
    assert capsys.readouterr().out == ""


def test_the_gateway_logs_the_notice_once_per_start_from_the_same_check():
    src = (PLUGIN / "adapter.py").read_text()
    assert src.count("money_guard.stale_runtime_notice()") == 1 and src.count('logger.warning("Emporia: %s", notice)') == 1


def test_a_new_plugin_loads_and_registers_on_the_previous_runtime():
    """N-1: a plugin update without the runtime update must never crash the gateway. The plugin imports no module
    of Emporia (it holds no symbol that only a new runtime has), and everything it starts in the runtime was already
    in 2.0.1: the bridge module and its flags, and the CLI module. Anything else it asks of the runtime is a
    subprocess whose failure is caught (peek, doctor)."""
    import ast
    import subprocess
    for path in PLUGIN.rglob("*.py"):
        if "node_modules" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
            assert not [n for n in names if n == "emporia" or n.startswith("emporia.")], (path.name, names)
    prev = "be98e561"          # the 2.0.1 release commit
    git = ["git", "-C", str(REPO)]
    if subprocess.run([*git, "cat-file", "-e", prev], capture_output=True).returncode != 0:
        pytest.skip("the 2.0.1 release commit is not in this clone")
    adapter = (PLUGIN / "adapter.py").read_text()
    module = next(line.split('"')[1] for line in adapter.splitlines() if line.startswith("BRIDGE_MODULE ="))
    bridge = subprocess.run([*git, "show", f"{prev}:src/{module.replace('.', '/')}.py"], capture_output=True, text=True)
    assert bridge.returncode == 0, f"{module} is not in 2.0.1"
    for flag in ("--relay-url", "--agent-id", "--profile-id"):
        assert flag in bridge.stdout, flag
    assert subprocess.run([*git, "cat-file", "-e", f"{prev}:src/emporia/cli.py"], capture_output=True).returncode == 0
