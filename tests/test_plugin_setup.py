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
    return argparse.Namespace(**{"profile": None, "here": False, "relay": None, "rest": [], **kw})


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


def test_it_runs_setup_agent_for_the_chosen_profile_only(monkeypatch):
    ran = []
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "default")
    monkeypatch.setattr(setup_cli, "_emporia", lambda rt, *a: ran.append(a) or 0)
    assert setup_cli.checkout() == REPO                          # in this repo the checkout is the runtime
    assert setup_cli.cmd_setup(_args(profile="shop", relay="https://r.test", rest=["--", "--role", "marketplace"])) == 0
    assert ran == [("setup", "agent", "shop", "--relay-url", "https://r.test", "--role", "marketplace")]


def test_a_catalog_runtime_is_pinned_to_a_full_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_cli, "SOURCE_FILE", tmp_path / "s.json")
    (tmp_path / "s.json").write_text('{"repo": "https://github.com/x/y", "sha": "abc"}')
    try:
        setup_cli.install_runtime(say=lambda *a: None)
        raise AssertionError("a short sha must be refused")
    except setup_cli.SetupFailed as exc:
        assert "pinned commit" in str(exc)
