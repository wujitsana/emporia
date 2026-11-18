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
