"""P295: `hermes emporia setup` — which profile becomes the agent, and what it writes."""
import argparse
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("emporia_setup_cli", REPO / "hermes_plugin" / "emporia" / "setup_cli.py")
setup_cli = importlib.util.module_from_spec(spec)
sys.modules["emporia_setup_cli"] = setup_cli
spec.loader.exec_module(setup_cli)


def _args(**kw):
    return argparse.Namespace(**{"profile": None, "here": False, "relay": None, "rest": [],
                                 "plugin_source": "emporia", "runtime_from": None, **kw})


def test_it_asks_current_or_new_and_recommends_new():
    answers = iter(["", ""])                                     # Enter, Enter: the recommended new profile
    assert setup_cli.choose_profile(_args(), "default", True, lambda q: next(answers)) == ("emporia-agent", True)
    answers = iter(["2"])
    assert setup_cli.choose_profile(_args(), "alpha", True, lambda q: next(answers)) == ("alpha", False)
    answers = iter(["1", "Default", "shop"])                     # `default` is the root: asked again
    assert setup_cli.choose_profile(_args(), "default", True, lambda q: next(answers)) == ("shop", True)


def test_without_a_terminal_it_asks_nothing_and_needs_the_choice_as_a_flag(capsys):
    assert setup_cli.choose_profile(_args(), "default", False) is None
    assert setup_cli.choose_profile(_args(here=True), "default", False) == ("default", False)
    assert setup_cli.choose_profile(_args(profile="shop"), "default", False) == ("shop", True)
    assert setup_cli.choose_profile(_args(profile="alpha"), "alpha", False) == ("alpha", False)
    monkeypatch_tty = type("NoTty", (), {"isatty": staticmethod(lambda: False)})()
    real, sys.stdin = sys.stdin, monkeypatch_tty
    try:
        assert setup_cli.cmd_setup(_args()) == 2
    finally:
        sys.stdin = real
    assert "--profile" in capsys.readouterr().out


def test_it_runs_setup_agent_for_the_chosen_profile_only(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "default")
    monkeypatch.setattr(setup_cli, "_emporia", lambda rt, *a: ran.append(a) or 0)
    assert setup_cli.checkout() == REPO                          # in this repo the checkout is the runtime
    assert setup_cli.cmd_setup(_args(profile="shop", relay="https://r.test", rest=["--", "--role", "marketplace"])) == 0
    assert ran == [("setup", "agent", "shop", "--relay-url", "https://r.test", "--role", "marketplace")]
    out = capsys.readouterr().out
    assert "hermes -p shop emporia status" in out and "hermes -p shop dashboard" in out   # P296: what next


def test_a_catalog_runtime_is_pinned_to_a_full_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cli, "SOURCE_FILE", tmp_path / "s.json")
    (tmp_path / "s.json").write_text('{"repo": "https://github.com/x/y", "sha": "abc"}')
    try:
        setup_cli.install_runtime(say=lambda *a: None)
        raise AssertionError("a short sha must be refused")
    except setup_cli.SetupFailed as exc:
        assert "pinned commit" in str(exc)


def test_the_pinned_runtime_is_unset_or_a_full_commit_of_this_repo():
    """P295: scripts/pin_plugin_runtime.py writes it at release; between releases it is empty."""
    import json
    data = json.loads((REPO / "hermes_plugin" / "emporia" / "emporia_source.json").read_text())
    assert data["repo"] == "https://github.com/wujitsana/emporia"
    assert data["sha"] == "" or (len(data["sha"]) == 40 and set(data["sha"]) <= set("0123456789abcdef"))


def test_a_release_can_be_tried_from_this_machine_before_it_is_pushed(tmp_path, monkeypatch):
    """P296: `--runtime-from REPO[@SHA]` clones the runtime from a repo here (or a URL)
    instead of the pinned GitHub commit; the pinned source itself stays https-only."""
    import subprocess
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    assert setup_cli.parse_runtime_from(str(REPO)) == (f"file://{REPO}", head)
    assert setup_cli.parse_runtime_from(f"https://example.test/e@{head}") == ("https://example.test/e", head)
    for bad in ("https://example.test/e", "ftp://x@" + head, "relative/path"):
        try:
            setup_cli.parse_runtime_from(bad)
            raise AssertionError(bad)
        except setup_cli.SetupFailed:
            pass
    ran = []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: ran.append(argv) or "")
    monkeypatch.setattr(setup_cli, "runtime_dir", lambda: tmp_path / "plugin-data" / "emporia" / "runtime")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    setup_cli.install_runtime(say=lambda *a: None, pin=(f"file://{REPO}", head))
    assert ran[0][:2] == ["git", "clone"] and f"file://{REPO}" in ran[0]
    assert ["git", "-C", str(tmp_path / "plugin-data" / "emporia" / "runtime"), "checkout", "--quiet",
            "--detach", head] in ran
    monkeypatch.setattr(setup_cli, "SOURCE_FILE", tmp_path / "s.json")
    (tmp_path / "s.json").write_text(f'{{"repo": "file://{REPO}", "sha": "{head}"}}')
    try:
        setup_cli.install_runtime(say=lambda *a: None)
        raise AssertionError("the pinned source must be https")
    except setup_cli.SetupFailed:
        pass


def test_status_says_whether_the_relay_has_the_agent(monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "runtime", lambda: REPO)
    settings = {"EMPORIA_AGENT_ID": "shop", "EMPORIA_RELAY_URL": "https://r.test"}
    monkeypatch.setattr(setup_cli, "_setting", lambda n: settings.get(n, ""))
    monkeypatch.setattr(setup_cli, "_registered", lambda relay, agent: False)
    assert setup_cli.cmd_status(_args()) == 1
    assert "not registered yet" in capsys.readouterr().out
    monkeypatch.setattr(setup_cli, "_registered", lambda relay, agent: True)
    assert setup_cli.cmd_status(_args()) == 0
    assert "Relay   : registered" in capsys.readouterr().out


def test_a_fresh_clone_is_checked_out_even_when_its_head_is_the_pinned_commit(tmp_path, monkeypatch):
    """P296, found by the end-to-end run: a --no-checkout clone has no files yet, and a
    release's pinned commit is its branch's tip, so HEAD already matches."""
    sha = "a" * 40
    ran = []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: ran.append(argv) or "")
    monkeypatch.setattr(setup_cli, "_git_head", lambda rt: sha)
    monkeypatch.setattr(setup_cli, "runtime_dir", lambda: tmp_path / "plugin-data" / "emporia" / "runtime")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    setup_cli.install_runtime(say=lambda *a: None, pin=("https://example.test/e", sha))
    assert any(a[3:5] == ["checkout", "--quiet"] for a in ran), ran


def test_hermess_own_python_settings_never_reach_emporias_processes(monkeypatch):
    """P296, found by the end-to-end run: the `hermes` launcher exports PYTHONPATH to its own
    packages; pip then skipped Emporia's dependencies, and Emporia's Python imported Hermes's."""
    monkeypatch.setenv("PYTHONPATH", "/opt/hermes:/opt/hermes/site")
    monkeypatch.setenv("PIP_TARGET", "/somewhere")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = setup_cli._clean_env()
    assert "PYTHONPATH" not in env and "PIP_TARGET" not in env and env["PATH"] == "/usr/bin"
