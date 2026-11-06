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
    out = capsys.readouterr().out
    assert "--agent-profile shop" in out and "--profile shop" not in out.replace("--agent-profile", "")


def _option_strings(parser):
    for action in parser._actions:
        yield from action.option_strings
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _option_strings(sub)


def test_no_option_of_hermes_emporia_is_one_hermes_takes_for_itself():
    """Release review (11-06): Hermes reads `--profile NAME`, `--profile=NAME` and `-p NAME` anywhere
    on its command line as its own profile switch, and strips them before a plugin's parser runs.
    `hermes emporia setup --profile shop` therefore never reached setup ("Profile 'shop' does not
    exist"). The agent profile is `--agent-profile`."""
    parser = argparse.ArgumentParser()
    setup_cli.register_cli(parser)
    options = set(_option_strings(parser))
    assert not {o for o in options if o in ("-p", "--profile") or o.startswith("--profile=")}, options
    setup = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["setup"]
    args = setup.parse_args(["--agent-profile", "shop", "--relay", "https://r.test", "--", "--role", "marketplace"])
    assert (args.profile, args.relay, args.rest) == ("shop", "https://r.test", ["--", "--role", "marketplace"])


@pytest.mark.parametrize("bad", ["../escape", "a/b", "-dash", "Bad Name", "x" * 65, "root", "default"])
def test_a_profile_name_is_held_to_hermess_rules_before_anything_runs(monkeypatch, capsys, bad):
    """Review follow-up L7: `--profile NAME` came back from `choose_profile` unchecked, and
    `_profile_exists`, `_has_agent` and `_remove_profile` built `profiles/<NAME>` paths from it."""
    ran = []
    for f in ("_emporia", "runtime", "_profile_exists", "_has_agent", "_remove_profile", "_install_plugin_into"):
        monkeypatch.setattr(setup_cli, f, lambda *a, f=f, **k: ran.append(f))
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "alpha")
    assert setup_cli.cmd_setup(_args(profile=bad)) == 2
    assert ran == [], ran
    assert "not a profile name Hermes accepts" in capsys.readouterr().out


def test_a_good_profile_name_is_taken_as_hermes_spells_it():
    assert setup_cli.choose_profile(_args(profile="Shop"), "alpha", False) == ("shop", True)
    assert setup_cli.choose_profile(_args(profile="default"), "default", False) == ("default", False)


def test_the_profile_name_rule_is_hermess_mirrored_not_imported():
    """Catalog rule 9: the plugin imports no private Hermes module where it can do without, so
    setup_cli mirrors hermes_cli.profiles' name rule. Here (a test, not the plugin) the mirror
    is held to Hermes's own, asked through Hermes's interpreter."""
    import inspect
    import json
    import subprocess
    assert "hermes_cli" not in inspect.getsource(setup_cli._valid_profile_name)
    hermes_py = Path("/opt/hermes/.venv/bin/python")
    if not hermes_py.exists():
        pytest.skip("no Hermes install here")
    names = ["shop", "a", "a" * 64, "a" * 65, "x-y_z9", "-a", "_a", "A", "a b", "../x", "a/b", "é",
             "root", "hermes", "tmp", "test", "sudo", "0day"]
    probe = ("import json, sys\nfrom hermes_cli import profiles\nok = {}\n"
             "for n in json.loads(sys.argv[1]):\n"
             "    try:\n        profiles.validate_profile_name(n); ok[n] = True\n"
             "    except ValueError:\n        ok[n] = False\n"
             "print(json.dumps({'ok': ok, 'reserved': sorted(profiles._RESERVED_NAMES)}))")
    out = subprocess.run([str(hermes_py), "-c", probe, json.dumps(names)], cwd="/opt/hermes",
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-1000:]
    hermes = json.loads(out.stdout.strip().splitlines()[-1])
    assert sorted(setup_cli._RESERVED_NAMES) == hermes["reserved"]
    for name in names:
        assert setup_cli._valid_profile_name(name) == hermes["ok"][name], name


def test_it_runs_setup_agent_for_the_chosen_profile_only(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "default")
    monkeypatch.setattr(setup_cli, "_emporia", lambda rt, *a: ran.append(a) or 0)
    assert setup_cli.checkout() == REPO                          # in this repo the checkout is the runtime
    assert setup_cli.cmd_setup(_args(profile="shop", relay="https://r.test", rest=["--", "--role", "marketplace"])) == 0
    assert ran == [("setup", "agent", "shop", "--relay-url", "https://r.test", "--role", "marketplace"),
                   ("welcome", "shop")]                           # then the read-only "you're in" block
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
    # the security follow-up's check that the hash-locked export is in step with uv.lock
    monkeypatch.setattr(pin, "committed_requirements", lambda s: "locked\n")
    monkeypatch.setattr(pin, "export_requirements", lambda s=None: "locked\n")
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


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_a_changed_file_the_index_was_told_to_ignore_is_still_refused(tmp_path, flag):
    """Found integrating the catalog work: `git status` reads the index, and an index told to
    look away from a file (assume-unchanged, skip-worktree) passes it changed. The files'
    own bytes are compared with the pinned tree."""
    import subprocess
    repo, sha, tree = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "update-index", flag, "code.py"], check=True)
    (repo / "code.py").write_text("print('changed, and the index looks away')\n")
    assert not subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                              text=True).stdout.strip(), "git status alone sees nothing"
    with pytest.raises(setup_cli.SetupFailed, match="differ from the pinned commit:\ncode.py"):
        setup_cli.verify_runtime(repo, sha, tree)
    (repo / "code.py").unlink()
    with pytest.raises(setup_cli.SetupFailed, match="differ from the pinned commit"):
        setup_cli.verify_runtime(repo, sha, tree)


@pytest.mark.parametrize("extra", ["src/emporia/modules/extra.py", "src/sitecustomize.py", "src/x.pth",
                                   "src/emporia/stale.pyc", "installer/helper.py"])
def test_code_no_commit_holds_is_refused_beside_the_runtime(tmp_path, extra):
    """emporia.modules imports every module in its folder, and an editable install puts `src`
    on sys.path (a sitecustomize there runs at every start): code beside the pinned files would
    run as the reviewed runtime. A cache in __pycache__, or another untracked file, is fine."""
    repo, sha, tree = _repo(tmp_path)
    cache = repo / "src" / "emporia" / "__pycache__" / "cli.cpython-313.pyc"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"cache")
    (repo / "src" / "emporia.egg-info").mkdir()
    (repo / "src" / "emporia.egg-info" / "PKG-INFO").write_text("Name: emporia\n")
    setup_cli.verify_runtime(repo, sha, tree)
    (repo / extra).parent.mkdir(parents=True, exist_ok=True)
    (repo / extra).write_text("print('not reviewed')\n")
    with pytest.raises(setup_cli.SetupFailed, match="code the pinned commit does not hold"):
        setup_cli.verify_runtime(repo, sha, tree)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_installing_puts_back_a_file_the_index_was_told_to_ignore(tmp_path, monkeypatch, flag):
    """The index is rebuilt from the pinned tree before the forced checkout, so a file hidden
    from it is restored rather than kept (and then refused) — update repairs the runtime.
    (`checkout --force` alone keeps a skip-worktree file as it is.)"""
    import subprocess
    repo, sha, tree = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "update-index", flag, "code.py"], check=True)
    (repo / "code.py").write_text("print('changed')\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / setup_cli._INSTALLED).write_text(sha + "\n")     # its packages: this commit's
    monkeypatch.setattr(setup_cli, "python_of", lambda r: Path(sys.executable))
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.test/e"], check=True)
    assert setup_cli._sync_runtime(lambda *a: None, repo, "https://example.test/e", sha, tree) == repo
    assert (repo / "code.py").read_text() == "print('reviewed')\n"


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


def test_git_uv_and_pip_get_no_secret_from_hermess_environment(monkeypatch, tmp_path):
    """Security audit (11-05): installing the runtime ran git, uv and pip with everything
    the `hermes` process holds — its .env's API keys among it — and pip and uv run
    packages' build code from PyPI. They get the locale, certificates, proxies and caches."""
    for k, v in (("OPENROUTER_API_KEY", "sk-or-secret"), ("TELEGRAM_BOT_TOKEN", "123:secret"),
                 ("EMPORIA_IDENTITY_PRIVATE_KEY", "ab" * 32), ("HTTPS_PROXY", "http://proxy:3128"),
                 ("LC_ALL", "C.UTF-8"), ("PATH", "/usr/bin")):
        monkeypatch.setenv(k, v)
    seen = []

    class Done:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda argv, **kw: seen.append(kw["env"]) or Done())
    setup_cli._run(["git", "--version"])
    [env] = seen
    assert not {"OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "EMPORIA_IDENTITY_PRIVATE_KEY"} & env.keys()
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and env["LC_ALL"] == "C.UTF-8" and env["PATH"] == "/usr/bin"


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
    monkeypatch.setattr(setup_cli, "_emporia", lambda r, *a: order.append(("setup", a[2]) if a[0] == "setup" else (a[0],)) or 0)
    assert setup_cli.cmd_setup(_args(profile="shop")) == 0
    assert order == [("create", "shop"), ("plugin", "shop"), ("setup", "shop"), ("welcome",)]


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


def _catalog_setup(monkeypatch, tmp_path, sha="a" * 40):
    rt = tmp_path / "hh" / "profiles" / "judge" / "plugin-data" / "emporia" / "runtime"
    monkeypatch.setattr(setup_cli, "hermes_home", lambda: tmp_path / "hh" / "profiles" / "judge")
    monkeypatch.setattr(setup_cli, "profile_name", lambda home=None: "judge")
    monkeypatch.setattr(setup_cli, "runtime", lambda: rt)
    monkeypatch.setattr(setup_cli, "from_catalog", lambda r: True)
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: sha)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://x.test/e", "sha": sha})
    monkeypatch.setattr(setup_cli, "python_of", lambda r: Path(sys.executable))
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda *a, **k: None)   # its own tests: above
    return rt


def test_a_build_that_pins_no_release_says_so_and_how_to_go_on(monkeypatch, tmp_path, capsys):
    """Found by the end-to-end run: with no pinned commit it said "Needs git, and uv or
    python3 with venv", which sends people after tools they have."""
    monkeypatch.setattr(setup_cli, "runtime", lambda: None)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://github.com/wujitsana/emporia", "sha": ""})
    monkeypatch.setattr(setup_cli, "_create_profile", lambda n: pytest.fail("no profile before a runtime"))
    assert setup_cli.cmd_setup(_args(profile="shop")) == 1
    out = capsys.readouterr().out
    assert "pins no release of the runtime yet" in out and "Needs git" not in out
    assert "runtime-from" not in out           # catalog rule 3: no other source to point at


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
    (rt / setup_cli.LOCKED_REQUIREMENTS).parent.mkdir(parents=True)       # a runtime's own export, for
    (rt / setup_cli.LOCKED_REQUIREMENTS).write_text("")                   # a machine with no uv
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


def test_without_uv_pip_installs_the_hash_locked_requirements(monkeypatch, tmp_path):
    """Security follow-up (11-05 audit): with no uv, the runtime was installed by
    `pip install -e <runtime>[x402]`, which resolves every dependency afresh from PyPI —
    whatever versions are newest that day, unchecked. pip now installs the requirements
    exported from uv.lock, every file by its hash (`--require-hashes`, `--no-deps`: nothing
    resolved), then Emporia itself without dependencies. A runtime without the file is
    refused rather than installed unpinned."""
    sha = "b" * 40
    rt = tmp_path / "plugin-data" / "emporia" / "runtime"
    ran = []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: ran.append(argv) or "")
    monkeypatch.setattr(setup_cli, "_git_head", lambda r: sha)
    monkeypatch.setattr(setup_cli, "_is_emporia", lambda r: True)
    monkeypatch.setattr(setup_cli, "runtime_dir", lambda: rt)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    monkeypatch.setattr(setup_cli, "source", lambda: {"repo": "https://example.test/e", "sha": sha})
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda *a, **k: None)   # its own tests: above
    (rt / ".git").mkdir(parents=True)
    with pytest.raises(setup_cli.SetupFailed, match="hash-locked"):
        setup_cli.install_runtime(say=lambda *a: None)
    assert not any("pip" in a for a in ran), "nothing installed unpinned"
    locked = rt / setup_cli.LOCKED_REQUIREMENTS
    locked.parent.mkdir(parents=True)
    locked.write_text("idna==3.10 \\\n    --hash=sha256:" + "0" * 64 + "\n")
    setup_cli.install_runtime(say=lambda *a: None)
    pips = [a for a in ran if a[1:4] == ["-m", "pip", "install"]]
    assert len(pips) == 2, ran
    deps, own = pips
    assert "--require-hashes" in deps and "--no-deps" in deps and deps[-2:] == ["-r", str(locked)]
    assert "--no-deps" in own and own[-2:] == ["-e", str(rt)], "Emporia itself: no dependency resolved"
    assert not any("[x402]" in x for a in pips for x in a)


def test_the_hash_locked_requirements_are_in_step_with_uv_lock():
    """The committed export names exactly uv.lock's packages for the runtime (no dev, the
    x402 extra), at uv.lock's versions, with only uv.lock's hashes."""
    import re
    import tomllib
    lock = {(p["name"], p["version"]): p for p in tomllib.loads((REPO / "uv.lock").read_text())["package"]}
    text = (REPO / setup_cli.LOCKED_REQUIREMENTS).read_text()
    pinned = re.findall(r"^([a-z0-9][a-z0-9._-]*)==([^ ;\\\n]+)", text, re.M)
    assert pinned and "eth-account" in dict(pinned) and "emporia" not in dict(pinned)
    for name, version in pinned:
        assert (name, version) in lock, (name, version)   # some carry two versions, by Python version
        pkg = lock[(name, version)]
        files = [*(pkg.get("wheels") or []), *([pkg["sdist"]] if "sdist" in pkg else [])]
        known = {f["hash"] for f in files}
        block = ("\n" + text).split(f"\n{name}=={version}", 1)[1].split("\n    # via", 1)[0]
        hashes = set(re.findall(r"--hash=(sha256:[0-9a-f]{64})", block))
        assert hashes and hashes <= known, name
    for dev_only in ("pytest", "ruff"):
        assert dev_only not in dict(pinned), dev_only


def test_the_release_pin_refuses_an_export_out_of_step_with_uv_lock(monkeypatch, tmp_path, capsys):
    """scripts/pin_plugin_runtime.py exports uv.lock (`--export`) and refuses to pin a commit
    whose committed export is not what uv.lock at that commit exports."""
    spec = importlib.util.spec_from_file_location("pin_plugin_runtime", REPO / "scripts" / "pin_plugin_runtime.py")
    pin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin)
    head = "c" * 40
    committed = {"x": "idna==3.10 \\\n    --hash=sha256:" + "1" * 64 + "\n"}
    monkeypatch.setattr(pin, "git", lambda *a: {("status", "--porcelain", "--untracked-files=no"): "",
                                                ("rev-parse", "HEAD"): head}.get(a, ""))
    monkeypatch.setattr(pin, "_on_origin", lambda sha: True)
    monkeypatch.setattr(pin, "committed_requirements", lambda sha: committed["x"])
    monkeypatch.setattr(pin, "export_requirements", lambda sha=None: "idna==3.11 \\\n    --hash=sha256:" + "2" * 64 + "\n")
    monkeypatch.setattr(pin, "SOURCE", tmp_path / "emporia_source.json")
    (tmp_path / "emporia_source.json").write_text('{"repo": "https://x.test/e", "sha": ""}')
    assert pin.main([]) == 1
    assert "out of step with uv.lock" in capsys.readouterr().out
    assert '"sha": ""' in (tmp_path / "emporia_source.json").read_text(), "not pinned"
    committed["x"] = pin.export_requirements()
    assert pin.main([]) == 0 and head in (tmp_path / "emporia_source.json").read_text()
    out = tmp_path / "req.txt"
    monkeypatch.setattr(pin, "REQUIREMENTS", out)
    assert pin.main(["--export"]) == 0 and out.read_text() == committed["x"]
