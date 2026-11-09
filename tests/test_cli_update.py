"""P252: `emporia update` — pull, then bring everything that runs from the checkout up to date."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from emporia import cli


class _Calls:
    """subprocess.run, recorded: git answers as told, everything else succeeds."""

    def __init__(self, dirty: str = "", heads: tuple[str, str] = ("a" * 40, "b" * 40), pull_ok: bool = True):
        self.argv: list[list[str]] = []
        self.dirty, self.heads, self.pull_ok = dirty, list(heads), pull_ok

    def __call__(self, argv, cwd=None, capture_output=False, text=False, timeout=None, **kw):
        self.argv.append([str(a) for a in argv])
        out, code = "", 0
        if argv[1:3] == ["status", "--porcelain"]:
            out = self.dirty
        elif argv[1:2] == ["rev-parse"]:
            out = self.heads.pop(0)
        elif argv[1:2] == ["pull"]:
            code = 0 if self.pull_ok else 1
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def ran(self, *words: str) -> bool:
        return any(all(any(w == x or x.endswith("/" + w) for x in a) for w in words) for a in self.argv)


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "REPO", tmp_path / "repo")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "dashboard").mkdir()
    monkeypatch.setattr(cli, "hermes_home", lambda: tmp_path / "hermes")
    monkeypatch.setattr(cli, "profiles_root", lambda: tmp_path / "hermes" / "profiles")
    monkeypatch.setattr(cli, "health", lambda **kw: None)
    monkeypatch.setattr(cli, "installer", lambda: type("Inst", (), {
        "install_dashboard_plugin": lambda self, *a: None, "print_restart_instructions": lambda self, p: None,
        "from_catalog": lambda self: False, "load_yaml": lambda self, p: {}})())
    rec = _Calls()
    monkeypatch.setattr(subprocess, "run", rec)
    return rec


def test_a_checkout_with_changes_of_its_own_is_not_pulled(calls, capsys):
    calls.dirty = " M src/emporia/cli.py"
    assert cli.main(["update"]) == 1
    assert "changes of its own" in capsys.readouterr().out
    assert not calls.ran("pull")


def test_a_pull_that_is_not_a_fast_forward_stops_everything(calls, capsys):
    calls.pull_ok = False
    assert cli.main(["update"]) == 1
    assert calls.ran("pull", "--ff-only") and not calls.ran("--no-pull")


def test_after_the_pull_the_rest_runs_as_the_new_code(calls):
    assert cli.main(["update"]) == 0
    assert calls.ran("pull", "--ff-only")
    assert [sys.executable, "-m", "emporia.cli", "update", "--no-pull"] in calls.argv


def test_no_pull_updates_packages_agents_plugins_and_relay(calls, monkeypatch, tmp_path, capsys):
    """P295: each agent's plugin carries the dashboard tab, so refreshing the agents updates it;
    no npm, and nothing written to a profile that is not an Emporia agent."""
    profile = tmp_path / "hermes" / "profiles" / "alpha"
    pid = tmp_path / "relay.pid"
    pid.write_text("1")
    monkeypatch.setattr(cli, "_relay_files", lambda: (pid, tmp_path / "relay.log"))
    monkeypatch.setattr(cli, "_agent_profiles", lambda root: [(profile, {})])
    seen: dict = {}

    class Inst:
        def from_catalog(self):
            return False

        def install_dashboard_plugin(self, *a):
            seen["tab"] = a

        def print_restart_instructions(self, profiles):
            seen["restart"] = True

    monkeypatch.setattr(cli, "installer", lambda: Inst())
    monkeypatch.setattr(cli, "cmd_agent_refresh", lambda name: seen.setdefault("refresh", name) and 0)
    monkeypatch.setattr(cli, "cmd_relay", lambda a: seen.setdefault("relay", a.action) and 0)

    assert cli.main(["update", "--no-pull"]) == 0
    assert not calls.ran("git") and not calls.ran("npm")
    assert calls.ran("pip", "install", "-e") or calls.ran("uv", "sync")
    assert seen == {"refresh": "all", "relay": "restart", "restart": True}
    assert "gateway restart" in capsys.readouterr().out


def test_a_relay_not_started_here_is_named_not_killed(calls, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_relay_files", lambda: (tmp_path / "none.pid", tmp_path / "relay.log"))
    monkeypatch.setattr(cli, "health", lambda **kw: {"ok": True})
    monkeypatch.setattr(cli, "cmd_relay", lambda a: pytest.fail("must not restart a relay it did not start"))
    assert cli.main(["update", "--no-pull"]) == 0
    assert "not started by `emporia relay start`" in capsys.readouterr().out


def test_failed_packages_stop_before_the_plugins_change(calls, monkeypatch):
    def run(argv, **kw):
        calls.argv.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(cli, "cmd_agent_refresh", lambda name: pytest.fail("plugins changed after a failed install"))
    assert cli.main(["update", "--no-pull"]) == 1
    assert not Path(cli.REPO / "dashboard" / "node_modules").exists()


def test_without_uv_the_packages_go_in_with_pip_for_the_python_running_emporia(calls, monkeypatch):
    """The Hermes container has no uv: pip, as the interpreter `emporia` itself runs on."""
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "uv" else f"/usr/bin/{name}")
    assert cli.main(["update", "--no-pull"]) == 0
    assert [sys.executable, "-m", "pip", "install", "-q", "-e", f"{cli.REPO}[x402,relay]"] in calls.argv  # the x402 extra (11-06), and a checkout's relay one
    assert not calls.ran("uv")
