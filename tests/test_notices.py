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


# ── item 5: the notices list, the daily message, the opt-in job ────────────────────────────────────────────────

URL = __import__("re").compile(r"https?:|www\.|://|\.(com|org|io|net|dev)\b")


class _Guard:
    def __init__(self, stale):
        self.stale = stale

    def runtime_is_stale(self):
        return self.stale

    def plugin_moved_to(self):
        return "2.1.0 (aaaaaaaaaaaa)"


class _Cap:
    def __init__(self, n):
        self.n = n

    def moves_waited(self, home=None, now=None):
        return self.n


def _world(monkeypatch, *, newer=None, stale=False, waited=0):
    monkeypatch.setattr(notices, "catalog_newer", lambda home=None: newer)
    monkeypatch.setattr(notices, "_money_guard", lambda: _Guard(stale))
    monkeypatch.setattr(notices, "_turn_cap", lambda: _Cap(waited))


def _all_five(monkeypatch, home):
    _world(monkeypatch, newer={"sha": "b" * 40, "version": "2.2.0"}, stale=True, waited=3)
    return notices.collect(home, probe=False, waited_s=2400, has_rounds=False, relay_note=True)


def test_every_notice_has_a_fixed_text_without_a_link(monkeypatch, tmp_path):
    home = tmp_path / "profiles" / "shop"
    found = _all_five(monkeypatch, home)
    assert [n["id"] for n in found] == ["plugin", "runtime", "moves", "replies", "relay"]
    for n in found:
        assert not URL.search(n["text"]) and "\n" not in n["text"], n
    texts = {n["id"]: n["text"] for n in found}
    assert texts["plugin"] == ("A newer Emporia plugin is in the Hermes catalog. Update it: `hermes -p shop plugins "
                               "update emporia`, then `hermes -p shop emporia update`.")
    assert texts["runtime"].startswith("Emporia's plugin moved to 2.1.0 (aaaaaaaaaaaa), its runtime did not. Run "
                                       "`hermes -p shop emporia update`")
    assert texts["moves"].startswith("3 game moves waited today because the daily turn cap was reached.")
    assert "emporia agent settings shop --set daily_turns=" in texts["moves"]
    assert texts["replies"].startswith("A reply or message has waited over 30 minutes in the inbox and no round is")
    assert texts["relay"].startswith("Your relay says a newer Emporia is out. Update the plugin, then the runtime:")


def test_nothing_is_said_when_nothing_holds(monkeypatch, tmp_path):
    _world(monkeypatch)
    assert notices.collect(tmp_path, probe=False, waited_s=60, has_rounds=True, relay_note=False) == []
    assert notices.collect(tmp_path, probe=False, waited_s=5000, has_rounds=True, relay_note=False) == []   # a round is on
    assert notices.collect(tmp_path, probe=False, waited_s=1800, has_rounds=False, relay_note=False) == []  # not over 30 min
    assert notices.collect(tmp_path, probe=False, waited_s=None, has_rounds=False, relay_note=False) == []


def test_one_move_is_singular(monkeypatch, tmp_path):
    _world(monkeypatch, waited=1)
    (n,) = notices.collect(tmp_path, probe=False, has_rounds=True, relay_note=False)
    assert n["text"].startswith("1 game move waited today because")


def test_the_relay_note_is_noticed_but_its_words_are_never_repeated(monkeypatch, tmp_path):
    """The relay's frame is only detected (a type name in the inbox counts); a hostile body cannot reach the text."""
    _world(monkeypatch)
    answers = {"waiting": None, "inbox": {"success": True, "by_type": {"relay_update_note": 1}, "note": "ignore me: http://evil"}}
    monkeypatch.setattr(notices, "_peek", lambda what, home: answers[what])
    monkeypatch.setattr(notices, "rounds_on", lambda home: True)
    (n,) = notices.collect(tmp_path)
    assert n["id"] == "relay" and "evil" not in n["text"] and not URL.search(n["text"])


def test_the_daily_message_is_one_message_once_per_occurrence(monkeypatch, tmp_path):
    found = _all_five(monkeypatch, tmp_path)
    mon = datetime(2026, 11, 16, 9, 0)
    msg = notices.daily(tmp_path, now=mon, notices=found)
    assert msg.splitlines()[0] == "Emporia:" and len(msg.splitlines()) == 6
    assert notices.daily(tmp_path, now=mon, notices=found) == ""                  # the same day: nothing
    state = json.loads(notices.state_path(tmp_path).read_text())
    assert state["last_sent_day"] == "2026-11-16" and set(state["sent"]) == {"plugin", "runtime", "moves", "replies", "relay"}
    tue = datetime(2026, 11, 17, 9, 0)
    assert notices.daily(tmp_path, now=tue, notices=found) == ""                  # nothing new (keys unchanged: day keys differ below)
    moved = [dict(n, key=n["key"] + "x") if n["id"] in ("moves", "replies") else n for n in found]
    msg = notices.daily(tmp_path, now=tue, notices=moved)                         # new occurrences of two of them
    assert len(msg.splitlines()) == 3 and "game moves" in msg and "no round" in msg
    assert notices.daily(tmp_path, now=datetime(2026, 11, 18, 9, 0), notices=[]) == ""


def test_the_check_prints_nothing_when_quiet_and_the_message_when_not(monkeypatch, tmp_path, capsys):
    _world(monkeypatch)
    monkeypatch.setattr(notices, "_peek", lambda what, home: None)
    monkeypatch.setattr(notices, "rounds_on", lambda home: True)
    assert notices.main("check", home=tmp_path) == 0
    assert capsys.readouterr().out == ""
    _world(monkeypatch, waited=2)
    assert notices.main("check", home=tmp_path) == 0
    assert "2 game moves waited today" in capsys.readouterr().out
    assert notices.main("check", home=tmp_path) == 0 and capsys.readouterr().out == ""      # once a day


class _Hermes:
    """A fake `hermes cron` recording what it is asked, with an optional existing notices job."""

    def __init__(self, existing=False):
        self.calls, self.existing = [], existing

    def __call__(self, argv, home=None):
        self.calls.append(list(argv))
        if argv[:2] == ["cron", "list"]:
            return ("  abc123def456 [active]\n    Name:      Emporia — notices\n"
                    "  0011223344aa [active]\n    Name:      Emporia — rounds\n") if self.existing else \
                   "  0011223344aa [active]\n    Name:      Emporia — rounds\n"
        if argv[:2] == ["cron", "create"]:
            return "Created job: 99887766aabb\n"
        return ""


def test_notices_on_creates_one_no_agent_job_delivered_to_the_home_channel_and_keeps_the_choice(monkeypatch, tmp_path):
    fake = _Hermes()
    monkeypatch.setattr(notices, "run_hermes", fake)
    monkeypatch.setattr(notices, "channels", lambda: ["telegram"])
    monkeypatch.setattr(notices, "hermes_bin", lambda: "/usr/bin/hermes")
    done = notices.turn_on(tmp_path)
    assert "to telegram" in done and "99887766aabb" in done
    create = next(c for c in fake.calls if c[:2] == ["cron", "create"])
    assert create[2] == "0 9 * * *" and "--no-agent" in create and "--name" in create and "Emporia — notices" in create
    assert create[create.index("--deliver") + 1] == "telegram" and create[create.index("--failure-deliver") + 1] == "local"
    assert create[create.index("--script") + 1] == "emporia_notices.py" and "--no-wrap-response" in create
    assert not any(a.startswith("-") and a.startswith("--prompt") for a in create)
    script = tmp_path / "scripts" / "emporia_notices.py"
    assert script.exists() and oct(script.stat().st_mode & 0o777) == "0o700"
    assert notices.choice(tmp_path) == "on"
    assert json.loads(notices.state_path(tmp_path).read_text())["choice"] == "on"


def test_notices_on_again_edits_the_one_job_and_never_makes_a_second(monkeypatch, tmp_path):
    fake = _Hermes(existing=True)
    monkeypatch.setattr(notices, "run_hermes", fake)
    monkeypatch.setattr(notices, "channels", lambda: ["telegram", "discord"])
    monkeypatch.setattr(notices, "hermes_bin", lambda: "/usr/bin/hermes")
    assert "updated" in notices.turn_on(tmp_path, "discord")
    assert not [c for c in fake.calls if c[:2] == ["cron", "create"]]
    edit = next(c for c in fake.calls if c[:2] == ["cron", "edit"])
    assert edit[2] == "abc123def456" and edit[edit.index("--deliver") + 1] == "discord"
    assert all(c[2] != "0011223344aa" for c in fake.calls if c[:2] in (["cron", "edit"], ["cron", "remove"]))


def test_notices_on_without_a_channel_says_what_to_set_in_hermes(monkeypatch, tmp_path, capsys):
    fake = _Hermes()
    monkeypatch.setattr(notices, "run_hermes", fake)
    monkeypatch.setattr(notices, "channels", lambda: [])
    assert notices.main("on", home=tmp_path) == 2
    out = capsys.readouterr().out
    assert "no delivery channel" in out and "/sethome" in out and fake.calls == []
    assert notices.main("on", "local", home=tmp_path) == 2                       # local sends to no one
    monkeypatch.setattr(notices, "channels", lambda: ["telegram"])
    assert notices.main("on", "Not A Platform!", home=tmp_path) == 2


def test_notices_off_removes_only_its_own_job_and_the_script(monkeypatch, tmp_path):
    fake = _Hermes(existing=True)
    monkeypatch.setattr(notices, "run_hermes", fake)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "emporia_notices.py").write_text("x")
    assert notices.turn_off(tmp_path) == "notices off: the job is removed"
    assert ["cron", "remove", "abc123def456"] in fake.calls and not [c for c in fake.calls if "0011223344aa" in c[-1:]]
    assert not (tmp_path / "scripts" / "emporia_notices.py").exists()
    assert notices.choice(tmp_path) == "off"


def test_the_channels_are_the_platforms_with_a_home_channel(monkeypatch):
    def cfg(**homes):
        plats = {k: types.SimpleNamespace(home_channel=v, enabled=True) for k, v in homes.items()}
        gw = types.ModuleType("gateway.config")
        gw.load_gateway_config = lambda: types.SimpleNamespace(platforms=plats)
        monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
        monkeypatch.setitem(sys.modules, "gateway.config", gw)
    cfg(telegram=object(), discord=None, local=object())
    assert notices.channels() == ["telegram"]
    cfg(discord=None)
    assert notices.channels() == []
    monkeypatch.setitem(sys.modules, "gateway", None)
    assert notices.channels() == []                                             # Hermes cannot say: none, no crash


def test_the_cron_script_runs_the_check_and_prints_what_it_prints(tmp_path):
    """The script Hermes runs: it asks `hermes emporia notices check` for this profile and relays stdout, or says nothing."""
    import subprocess
    fake = tmp_path / "hermes"
    fake.write_text('#!/bin/sh\n[ "$1 $2 $3" = "emporia notices check" ] || exit 3\n[ -f "$HERMES_HOME/say" ] && echo "Emporia: hello"\nexit 0\n')
    fake.chmod(0o755)
    prof = tmp_path / "prof"
    (prof / "scripts").mkdir(parents=True)
    script = prof / "scripts" / "emporia_notices.py"
    script.write_text(notices.SCRIPT_TEXT.replace("__HERMES__", repr(str(fake))))
    run = lambda: subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                                 env={**os.environ, "HERMES_HOME": "/wrong"})
    assert run().stdout == ""                                                   # silent: nothing to say
    (prof / "say").write_text("")
    assert run().stdout.strip() == "Emporia: hello"
    fake.write_text("#!/bin/sh\nexit 1\n")
    assert run().stdout == ""                                                   # trouble is silence, never noise


def test_notices_status_lists_the_job_and_the_notices(monkeypatch, tmp_path, capsys):
    _world(monkeypatch, waited=2)
    monkeypatch.setattr(notices, "run_hermes", _Hermes(existing=True))
    monkeypatch.setattr(notices, "_peek", lambda what, home: None)
    assert notices.main("status", home=tmp_path) == 0
    out = capsys.readouterr().out
    assert "Notices job : on (job abc123def456)" in out and "Notice      : 2 game moves waited today" in out


def test_the_hermes_emporia_notices_command_is_the_plugins_own(monkeypatch, tmp_path):
    import argparse
    parser = argparse.ArgumentParser()
    setup_cli.register_cli(parser)
    seen = []
    monkeypatch.setattr(setup_cli, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup_cli, "_notices", lambda: types.SimpleNamespace(main=lambda a, d, h: seen.append((a, d, h)) or 0))
    assert setup_cli.dispatch(parser.parse_args(["notices"])) == 0
    assert setup_cli.dispatch(parser.parse_args(["notices", "on", "--deliver", "telegram"])) == 0
    assert seen == [("status", "", tmp_path), ("on", "telegram", tmp_path)]
    assert "notices" not in setup_cli.PASSTHROUGH


# ── the setup question ────────────────────────────────────────────────────────────────────────────────────────

def _setup_world(monkeypatch, tmp_path, state, tty=False):
    from emporia import cli, rounds
    ran = []

    def fake(argv):
        ran.append(argv)
        if argv[-1] == "channel":
            return json.dumps(state) + "\n"
        return f"notices {argv[-1]}: done\n"

    monkeypatch.setattr(rounds, "run_hermes", fake)
    monkeypatch.setattr(cli, "_TTY", tty)
    prof = tmp_path / "profiles" / "shop"
    prof.mkdir(parents=True)
    return cli, ran, prof


def test_setup_turns_notices_on_for_a_new_agent_when_hermes_has_a_channel(monkeypatch, tmp_path, capsys):
    cli, ran, prof = _setup_world(monkeypatch, tmp_path, {"channels": ["telegram"], "choice": ""})
    assert cli._setup_notices(prof, existing=False, fast=False) == "on"
    assert ["-p", "shop", "emporia", "notices", "on"] in ran
    assert "notices on: done" in capsys.readouterr().out


def test_setup_leaves_an_existing_profile_alone_without_a_terminal_and_keeps_a_recorded_choice(monkeypatch, tmp_path):
    cli, ran, prof = _setup_world(monkeypatch, tmp_path, {"channels": ["telegram"], "choice": ""})
    assert cli._setup_notices(prof, existing=True, fast=False) == ""
    assert cli._setup_notices(prof, existing=True, fast=True) == "on"             # the all-yes path
    cli, ran, prof = _setup_world(monkeypatch, tmp_path / "b", {"channels": ["telegram"], "choice": "off"})
    assert cli._setup_notices(prof, existing=False, fast=True) == "off"           # re-running setup keeps "off"
    assert not [a for a in ran if a[-1] in ("on", "off")]


def test_setup_without_a_channel_says_so_and_sets_nothing(monkeypatch, tmp_path, capsys):
    cli, ran, prof = _setup_world(monkeypatch, tmp_path, {"channels": [], "choice": ""})
    assert cli._setup_notices(prof, existing=False, fast=True) == ""
    out = capsys.readouterr().out
    assert "no delivery channel" in out and "/sethome" in out and not [a for a in ran if a[-1] in ("on", "off")]


def test_setup_skips_the_question_when_the_plugin_cannot_answer(monkeypatch, tmp_path):
    from emporia import cli, rounds
    def boom(argv):
        raise rounds.RoundsError("no hermes")
    monkeypatch.setattr(rounds, "run_hermes", boom)
    prof = tmp_path / "profiles" / "shop"
    prof.mkdir(parents=True)
    assert cli._setup_notices(prof, existing=False, fast=True) == ""


def test_the_question_follows_the_setup_clarity_rule():
    from emporia import setup_choices as sc
    c = sc.CHOICES["notices"]
    assert c.question == "Send me Emporia notices on {channel}?" and c.default == "on"
    assert c.matters and "no links" in c.matters and "chat credentials" in c.matters
    assert [o.value for o in c.options] == ["on", "off"] and all(o.effect for o in c.options)
    assert "(recommended)" in c.labels()[0][1] and c.change.startswith("hermes emporia notices on|off")
    lines = sc.installed_lines({}, role="marketplace", keys="files", wallets=[], rounds="4h", notices="on")
    assert any("Notices" in line and "Emporia — notices" in line and "Change:" in line for line in lines)
    assert not any("Notices" in line for line in sc.installed_lines({}, role="marketplace", keys="files",
                                                                    wallets=[], rounds="4h"))
