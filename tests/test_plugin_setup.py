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


def test_a_new_profile_gets_the_plugin_before_its_agent_is_set_up(monkeypatch, tmp_path):
    """P296, found by the end-to-end run: the plugin install came after setup's "restart"
    advice, so that advice was printed before everything it covered."""
    order = []
    rt = tmp_path / "plugin-data" / "emporia" / "runtime"
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "judge")
    monkeypatch.setattr(setup_cli, "runtime", lambda: rt)
    monkeypatch.setattr(setup_cli, "from_catalog", lambda r: True)
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: "a" * 40)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://x.test/e", "sha": "a" * 40})
    monkeypatch.setattr(setup_cli, "python_of", lambda r: Path(sys.executable))
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: order.append(("create", n)) or True)
    monkeypatch.setattr(setup_cli, "_install_plugin_into", lambda n, s: order.append(("plugin", n)) or True)
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: order.append(("setup", a[2])) or 0)
    assert setup_cli.cmd_setup(_args(profile="shop")) == 0
    assert order == [("create", "shop"), ("plugin", "shop"), ("setup", "shop")]


def test_status_says_where_a_runtime_came_from(monkeypatch, tmp_path):
    """P296: a --runtime-from runtime is not "not the pinned"; it says where it is from."""
    sha, other = "a" * 40, "b" * 40
    monkeypatch.setattr(setup_cli, "from_catalog", lambda r: True)
    state = {"head": sha, "origin": "https://github.com/wujitsana/emporia"}
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: state["head"])
    monkeypatch.setattr(setup_cli, "_origin", lambda r: state["origin"])
    src = {"repo": "https://github.com/wujitsana/emporia", "sha": sha}
    assert setup_cli.runtime_note(tmp_path, src) == "pinned"
    state.update(head=other, origin="file:///home/me/emporia")
    note = setup_cli.runtime_note(tmp_path, src)
    assert note.startswith("from --runtime-from file:///home/me/emporia") and "update" in note
    assert setup_cli.runtime_note(tmp_path, {**src, "sha": ""}).startswith("from --runtime-from")
    state["origin"] = src["repo"]
    assert "not the pinned" in setup_cli.runtime_note(tmp_path, src)
    assert "pins no release" in setup_cli.runtime_note(tmp_path, {**src, "sha": ""})


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


def _catalog_setup(monkeypatch, tmp_path, sha="a" * 40):
    rt = tmp_path / "hh" / "profiles" / "judge" / "plugin-data" / "emporia" / "runtime"
    monkeypatch.setattr(setup_cli, "hermes_home", lambda: tmp_path / "hh" / "profiles" / "judge")
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "judge")
    monkeypatch.setattr(setup_cli, "runtime", lambda: rt)
    monkeypatch.setattr(setup_cli, "from_catalog", lambda r: True)
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: sha)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://x.test/e", "sha": sha})
    monkeypatch.setattr(setup_cli, "python_of", lambda r: Path(sys.executable))
    return rt


def test_a_build_that_pins_no_release_says_so_and_how_to_go_on(monkeypatch, tmp_path, capsys):
    """Found by the end-to-end run: with no pinned commit it said "Needs git, and uv or
    python3 with venv", which sends people after tools they have."""
    monkeypatch.setattr(setup_cli, "runtime", lambda: None)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://github.com/wujitsana/emporia", "sha": ""})
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: pytest.fail("no profile before a runtime"))
    assert setup_cli.cmd_setup(_args(profile="shop")) == 1
    out = capsys.readouterr().out
    assert "pins no release yet; use --runtime-from REPO@SHA" in out and "Needs git" not in out


def test_an_existing_profile_is_not_called_new(monkeypatch, tmp_path, capsys):
    """Found by the end-to-end run: "(new)" was said of every profile but the current one."""
    _catalog_setup(monkeypatch, tmp_path)
    plugin = tmp_path / "hh" / "profiles" / "alpha" / "plugins" / "emporia"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: emporia\n")
    ran = []
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: pytest.fail("it exists"))
    monkeypatch.setattr(setup_cli, "_install_plugin_into", lambda n, s: pytest.fail("it has the plugin"))
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: ran.append(a) or 0)
    assert setup_cli.cmd_setup(_args(profile="alpha")) == 0
    out = capsys.readouterr().out
    assert "Agent profile: alpha  (existing)" in out and "(new)" not in out
    assert "--new-profile" not in ran[0]


@pytest.mark.parametrize("fails", ["plugin", "setup agent"])
def test_a_profile_made_for_a_setup_that_failed_is_not_left_behind(monkeypatch, tmp_path, capsys, fails):
    _catalog_setup(monkeypatch, tmp_path)
    removed = []
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: "made")
    monkeypatch.setattr(setup_cli, "_install_plugin_into", lambda n, s: fails != "plugin")
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: 1)
    monkeypatch.setattr(setup_cli, "_remove_profile", lambda n: removed.append(n))
    assert setup_cli.cmd_setup(_args(profile="shop")) == 1
    assert removed == ["shop"]


def test_removing_a_failed_profile_never_removes_an_agent(monkeypatch, tmp_path):
    _catalog_setup(monkeypatch, tmp_path)
    prof = tmp_path / "hh" / "profiles" / "shop"
    (prof / "home" / ".hermes" / "keys").mkdir(parents=True)
    (prof / "config.yaml").write_text("model: {}\n")
    ran = []
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/x/hermes")
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda argv, **kw: ran.append(argv) or
                        argparse.Namespace(returncode=0))
    setup_cli._remove_profile("shop")
    assert ran == [["/x/hermes", "profile", "delete", "shop", "--yes"]]
    (prof / "home" / ".hermes" / "keys" / "shop.priv").write_bytes(b"k" * 32)
    setup_cli._remove_profile("shop")
    (prof / "home" / ".hermes" / "keys" / "shop.priv").unlink()
    (prof / "config.yaml").write_text("mcp_servers:\n  emporia:\n    args: [-m, emporia.mcp_server]\n")
    setup_cli._remove_profile("shop")
    assert len(ran) == 1, "a profile holding an agent's keys or MCP entry stays"


def test_update_with_nothing_to_do_says_so_and_names_each_profile_to_restart(monkeypatch, tmp_path, capsys):
    """Found by the end-to-end run: update said "first time: a minute or two" and "restart the
    gateway" when nothing had changed, and its restart hint named no profile."""
    sha = "a" * 40
    rt = _catalog_setup(monkeypatch, tmp_path, sha)
    py = rt / ".venv" / "bin" / "python"
    monkeypatch.setattr(setup_cli, "python_of", lambda r: py)
    for name in ("judge", "shop", "other"):
        (tmp_path / "hh" / "profiles" / name).mkdir(parents=True, exist_ok=True)
    for name in ("shop", "judge"):
        (tmp_path / "hh" / "profiles" / name / "config.yaml").write_text(f"mcp_servers:\n  emporia:\n    command: {py}\n")
    (tmp_path / "hh" / "profiles" / "other" / "config.yaml").write_text("model: {}\n")
    ran = []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: ran.append(argv) or "")
    (rt / ".git").mkdir(parents=True)
    monkeypatch.setattr(setup_cli, "_is_emporia", lambda r: True)
    py.parent.mkdir(parents=True)
    py.write_text("")
    parser = argparse.ArgumentParser()
    setup_cli.register_cli(parser)
    assert setup_cli.dispatch(parser.parse_args(["update"])) == 0           # first: installs, then restarts
    out = capsys.readouterr().out
    assert "hermes -p judge gateway restart" in out and "hermes -p shop gateway restart" in out
    assert "other" not in out and "first time" not in out
    installs = len(ran)
    assert setup_cli.dispatch(parser.parse_args(["update"])) == 0           # again: nothing to do
    out = capsys.readouterr().out
    assert "nothing to do" in out and "restart" not in out and "Installing" not in out
    assert all("install" not in " ".join(a) and "sync" not in a for a in ran[installs:]), ran[installs:]
