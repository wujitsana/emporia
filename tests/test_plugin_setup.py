"""P295: `hermes emporia setup` — which profile becomes the agent, and what it writes."""
import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("emporia_setup_cli", REPO / "hermes_plugin" / "emporia" / "setup_cli.py")
setup_cli = importlib.util.module_from_spec(spec)
sys.modules["emporia_setup_cli"] = setup_cli
spec.loader.exec_module(setup_cli)


def _args(**kw):
    return argparse.Namespace(**{"profile": None, "here": False, "relay": None, "rest": [],
                                 "plugin_source": "emporia", **kw})


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
    """P295: scripts/pin_plugin_runtime.py writes it at release; between releases it is empty.
    Catalog rule 3: with the commit, the tree it holds."""
    import json
    data = json.loads((REPO / "hermes_plugin" / "emporia" / "emporia_source.json").read_text())
    assert data["repo"] == "https://github.com/wujitsana/emporia"
    full = set("0123456789abcdef")
    assert data["sha"] == "" or (len(data["sha"]) == 40 and set(data["sha"]) <= full)
    assert data["tree"] == "" or (len(data["tree"]) == 40 and set(data["tree"]) <= full)
    assert bool(data["sha"]) == bool(data["tree"])


def test_the_catalog_build_has_no_way_to_run_another_runtime():
    """Catalog rule 3: `--runtime-from` is gone from setup and update; the runtime comes only
    from the https source and commit emporia_source.json pins."""
    import inspect
    parser = argparse.ArgumentParser()
    setup_cli.register_cli(parser)
    for argv in (["setup", "--runtime-from", "/somewhere"], ["update", "--runtime-from", "/somewhere"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    assert list(inspect.signature(setup_cli.install_runtime).parameters) == ["say", "target"]
    src = (REPO / "hermes_plugin" / "emporia" / "setup_cli.py").read_text()
    assert "runtime-from" not in src and "runtime_from" not in src


def test_the_pin_script_records_the_commit_and_its_tree(tmp_path, monkeypatch):
    """Catalog rule 3: scripts/pin_plugin_runtime.py writes the commit and the tree it holds,
    which setup checks after checkout."""
    import json
    import subprocess
    spec = importlib.util.spec_from_file_location("pin_script", REPO / "scripts" / "pin_plugin_runtime.py")
    pin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin)
    sha, tree = "1" * 40, "2" * 40
    answers = {("status", "--porcelain", "--untracked-files=no"): "", ("rev-parse", "HEAD"): sha,
               ("rev-parse", f"{sha}^{{tree}}"): tree}
    monkeypatch.setattr(pin, "git", lambda *a: answers[a])
    monkeypatch.setattr(pin.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="origin/main\n", stderr=""))
    source = tmp_path / "emporia_source.json"
    source.write_text('{"repo": "https://github.com/wujitsana/emporia", "sha": "", "tree": ""}\n')
    monkeypatch.setattr(pin, "SOURCE", source)
    assert pin.main([]) == 0
    assert json.loads(source.read_text()) == {"repo": "https://github.com/wujitsana/emporia", "sha": sha,
                                              "tree": tree}


def _repo(tmp_path):
    """A small git repo with one commit: (path, sha, tree)."""
    import subprocess
    repo = tmp_path / "src-repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,  # noqa: E731
                                    check=True).stdout.strip()
    run("init", "-q")
    (repo / "code.py").write_text("print('reviewed')\n")
    run("add", "code.py")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    return repo, run("rev-parse", "HEAD"), run("rev-parse", "HEAD^{tree}")


def test_the_runtime_is_verified_against_the_pinned_commit_and_tree(tmp_path):
    """Catalog rule 3: after checkout, HEAD is the pinned commit, its tree is the recorded tree,
    and no tracked file was changed; anything else refuses to run."""
    repo, sha, tree = _repo(tmp_path)
    setup_cli.verify_runtime(repo, sha, tree)                                   # exactly the pin
    setup_cli.verify_runtime(repo, sha)                                         # no tree recorded yet
    with pytest.raises(setup_cli.SetupFailed, match="not the pinned"):
        setup_cli.verify_runtime(repo, "b" * 40, tree)
    with pytest.raises(setup_cli.SetupFailed, match="tree"):
        setup_cli.verify_runtime(repo, sha, "c" * 40)
    (repo / "code.py").write_text("print('changed here')\n")
    with pytest.raises(setup_cli.SetupFailed, match="differ from the pinned commit"):
        setup_cli.verify_runtime(repo, sha, tree)


def test_status_says_when_the_runtime_is_not_the_verified_pin(tmp_path, monkeypatch):
    repo, sha, tree = _repo(tmp_path)
    rt = tmp_path / "home" / "plugin-data" / "emporia" / "runtime"
    rt.parent.mkdir(parents=True)
    repo.rename(rt)
    src = {"repo": "https://github.com/wujitsana/emporia", "sha": sha, "tree": tree}
    monkeypatch.setattr(setup_cli, "_origin", lambda r: src["repo"])
    assert setup_cli.runtime_note(rt, src) == "pinned, verified"
    (rt / "code.py").write_text("print('changed here')\n")
    assert "not the pinned" in setup_cli.runtime_note(rt, src) and "hermes emporia update" in setup_cli.runtime_note(rt, src)
    monkeypatch.setattr(setup_cli, "_origin", lambda r: "file:///elsewhere")
    assert "not the pinned source" in setup_cli.runtime_note(rt, src)
    assert "pins no runtime" in setup_cli.runtime_note(rt, {**src, "sha": ""})


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
    release's pinned commit is its branch's tip, so HEAD already matches. Catalog rule 3: the
    checkout is forced (a changed tracked file is put back) and verified before and after the
    packages are installed."""
    sha, tree = "a" * 40, "b" * 40
    ran, verified = [], []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: ran.append(argv) or "")
    monkeypatch.setattr(setup_cli, "_git_head", lambda rt: sha)
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda rt, s, t="": verified.append((s, t)))
    monkeypatch.setattr(setup_cli, "runtime_dir", lambda: tmp_path / "plugin-data" / "emporia" / "runtime")
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://example.test/e", "sha": sha, "tree": tree})
    setup_cli.install_runtime(say=lambda *a: None)
    assert any(a[3:7] == ["checkout", "--quiet", "--force", "--detach"] for a in ran), ran
    assert verified == [(sha, tree), (sha, tree)]
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "file:///local", "sha": sha})
    with pytest.raises(setup_cli.SetupFailed, match="pinned commit"):
        setup_cli.install_runtime(say=lambda *a: None)


def test_hermess_own_python_settings_never_reach_emporias_processes(monkeypatch):
    """P296, found by the end-to-end run: the `hermes` launcher exports PYTHONPATH to its own
    packages; pip then skipped Emporia's dependencies, and Emporia's Python imported Hermes's."""
    monkeypatch.setenv("PYTHONPATH", "/opt/hermes:/opt/hermes/site")
    monkeypatch.setenv("PIP_TARGET", "/somewhere")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = setup_cli._clean_env()
    assert "PYTHONPATH" not in env and "PIP_TARGET" not in env and env["PATH"] == "/usr/bin"


def test_no_child_of_hermes_emporia_inherits_an_approval_bypass(monkeypatch):
    """Catalog rule 12: `hermes -p NAME plugins install`, `hermes profile create` and the
    runtime's CLI (and the `hermes` commands it runs) never inherit YOLO or pre-accepted hooks."""
    for name in ("HERMES_YOLO_MODE", "HERMES_YOLO", "HERMES_ACCEPT_HOOKS", "HERMES_ACP_AUTO_APPROVE"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HERMES_HOME", "/somewhere/home")
    env = setup_cli._clean_env()
    assert not setup_cli.APPROVAL_BYPASS_VARS & set(env)
    assert env["HERMES_HOME"] == "/somewhere/home"


@pytest.mark.parametrize("tty", [True, False])
def test_the_runtimes_cli_never_waits_on_a_stdin_nobody_writes_to(monkeypatch, tmp_path, tty):
    """Catalog rule 12: under cron or the gateway (no terminal) a question from the runtime's
    CLI ends at once on /dev/null instead of hanging; at a terminal it is asked as before."""
    seen = {}

    class Stdin:
        def isatty(self):
            return tty

    def call(argv, env=None, stdin=None):
        seen.update(argv=argv, env=env, stdin=stdin)
        return 0

    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setattr(setup_cli.sys, "stdin", Stdin())
    monkeypatch.setattr(setup_cli.subprocess, "call", call)
    assert setup_cli._emporia(tmp_path, "wallet", "status") == 0
    assert seen["argv"][-3:] == ["emporia.cli", "wallet", "status"]
    assert seen["stdin"] is (None if tty else setup_cli.subprocess.DEVNULL)
    assert "HERMES_YOLO_MODE" not in seen["env"]


def test_a_new_profile_gets_the_plugin_before_its_agent_is_set_up(monkeypatch, tmp_path):
    """P296, found by the end-to-end run: the plugin install came after setup's "restart"
    advice, so that advice was printed before everything it covered."""
    order = []
    rt = tmp_path / "plugin-data" / "emporia" / "runtime"
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "judge")
    monkeypatch.setattr(setup_cli, "runtime", lambda: rt)
    monkeypatch.setattr(setup_cli, "from_catalog", lambda r: True)
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: "a" * 40)
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda *a, **k: None)   # its own tests: above
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://x.test/e", "sha": "a" * 40})
    monkeypatch.setattr(setup_cli, "python_of", lambda r: Path(sys.executable))
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: order.append(("create", n)) or True)
    monkeypatch.setattr(setup_cli, "_install_plugin_into", lambda n, s: order.append(("plugin", n)) or True)
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: order.append(("setup", a[2])) or 0)
    assert setup_cli.cmd_setup(_args(profile="shop")) == 0
    assert order == [("create", "shop"), ("plugin", "shop"), ("setup", "shop")]


@pytest.mark.parametrize("made,flag", [("made", True), ("there", False)])
def test_setup_agent_hears_that_the_profile_was_made_just_now(monkeypatch, tmp_path, made, flag):
    """Found by the end-to-end run: a profile the plugin had just made was set up as an
    existing one (developer role, keys in files). Only a profile it made is new."""
    ran = []
    rt = tmp_path / "plugin-data" / "emporia" / "runtime"
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "judge")
    monkeypatch.setattr(setup_cli, "runtime", lambda: rt)
    monkeypatch.setattr(setup_cli, "from_catalog", lambda r: True)
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: "a" * 40)
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda *a, **k: None)   # its own tests: above
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://x.test/e", "sha": "a" * 40})
    monkeypatch.setattr(setup_cli, "python_of", lambda r: Path(sys.executable))
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: made)
    monkeypatch.setattr(setup_cli, "_install_plugin_into", lambda n, s: True)
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: ran.append(a) or 0)
    assert setup_cli.cmd_setup(_args(profile="shop")) == 0
    assert ("--new-profile" in ran[0]) is flag


def test_doctor_checks_this_profile_and_takes_all(monkeypatch, tmp_path):
    """Found by the end-to-end run: `hermes -p shop emporia doctor` checked every
    profile, and refused the `--all` its last line suggests."""
    ran = []
    monkeypatch.setattr(setup_cli, "runtime", lambda: tmp_path)
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: ran.append(a) or 0)
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "shop")
    parser = argparse.ArgumentParser()
    setup_cli.register_cli(parser)
    assert setup_cli.dispatch(parser.parse_args(["doctor", "--all"])) == 0
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "default")
    assert setup_cli.dispatch(parser.parse_args(["doctor"])) == 0
    assert ran == [("doctor", "--profile", "shop", "--all"), ("doctor",)]


def test_every_emporia_command_passes_through_hermes_emporia():
    """Found by the end-to-end run: a plugin-only install puts no `emporia` on PATH, yet
    doctor and the tools' refusals name its commands. `hermes emporia NAME …` runs them;
    setup, status, doctor and update stay the plugin's own."""
    import argparse as ap

    from emporia import cli
    commands = next(a for a in cli.build_parser()._actions if isinstance(a, ap._SubParsersAction)).choices
    assert set(setup_cli.PASSTHROUGH) == set(commands) - {"setup", "doctor", "update"}
    setup = next(a for a in commands["setup"]._actions if isinstance(a, ap._SubParsersAction)).choices
    assert set(setup_cli.SETUP_PASSTHROUGH) == set(setup) - {"agent"}


def test_hermes_emporia_runs_the_runtimes_command_with_every_word(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(setup_cli, "runtime", lambda: tmp_path)
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: ran.append(a) or 0)
    parser = argparse.ArgumentParser()
    setup_cli.register_cli(parser)
    for argv in (["agent", "playbook", "shop", "--games", "set-plays"], ["wallet", "--help"], ["login"],
                 ["house", "run", "h", "--once", "-h"], ["setup", "dashboard", "shop"],
                 ["setup", "relay", "--preset", "base-sepolia"]):
        assert setup_cli.dispatch(parser.parse_args(argv)) == 0
    assert ran == [("agent", "playbook", "shop", "--games", "set-plays"), ("wallet", "--help"), ("login",),
                   ("house", "run", "h", "--once", "-h"), ("setup", "dashboard", "shop"),
                   ("setup", "relay", "--preset", "base-sepolia")]
    monkeypatch.setattr(setup_cli, "runtime", lambda: None)
    assert setup_cli.dispatch(parser.parse_args(["wallet", "create", "shop"])) == 1
