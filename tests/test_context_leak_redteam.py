"""Red team: a host's project notes must not reach a stranger's turn.

The leak: Hermes resolves a gateway turn's working directory to the folder the gateway was launched in and puts
that folder's project files (.hermes.md, AGENTS.md chain up to the git root, CLAUDE.md) into the agent's system
prompt. An Emporia profile sits inside the host's repo, so any other agent that DMs it could draw those notes out.
The fix is a pair the installer writes: `terminal.cwd` is a folder of the profile and
`gateway.platforms.emporia.skip_context_files: true`.

The canary tests drive Hermes's own `agent.system_prompt._context_files_part` (in Hermes's interpreter, skipped
where Hermes is not installed) against a repo that holds a canary in each project file."""
import json
import subprocess
from pathlib import Path

import pytest

from emporia import cli

CANARY = "CANARY-9f3c2a-host-project-note"

_SCRIPT = r"""
import json, os, sys, types
sys.path.insert(0, sys.argv[1])
cfg = json.load(open(sys.argv[2]))
launch = sys.argv[3]
os.chdir(launch)
if cfg.get("terminal", {}).get("cwd"):
    os.environ["TERMINAL_CWD"] = cfg["terminal"]["cwd"]
else:
    os.environ.pop("TERMINAL_CWD", None)
# gateway/session_execution.py:_skip_context_files, as Hermes reads the profile's config
plat = ((cfg.get("gateway") or {}).get("platforms") or {})
skip = bool((plat.get("emporia") or {}).get("skip_context_files"))
from agent import system_prompt as sp
agent = types.SimpleNamespace(skip_context_files=skip, platform="emporia", load_soul_identity=True,
                              _context_cwd_is_launch_artifact=False, _session_db=None)
parts = sp._context_files_part(agent, None, True)
print(json.dumps({"skip": skip, "prompt": "\n".join(parts)}))
"""


def _hermes():
    found = cli._hermes_python()
    if found is None:
        pytest.skip("Hermes is not installed here")
    return found


def _run(tmp_path, cfg, launch):
    python, install = _hermes()
    cfg_file = tmp_path / "probe-config.json"
    cfg_file.write_text(json.dumps(cfg))
    script = tmp_path / "probe.py"
    script.write_text(_SCRIPT)
    out = subprocess.run([python, "-I", str(script), install, str(cfg_file), str(launch)], capture_output=True,
                         text=True, timeout=60, stdin=subprocess.DEVNULL, env={"HERMES_HOME": str(tmp_path / "home"),
                                                                              "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture
def host(tmp_path):
    """A host repo with a canary in every project file, and an Emporia profile inside it."""
    repo = tmp_path / "host"
    (repo / ".git").mkdir(parents=True)
    profile = repo / "profiles" / "alpha"
    profile.mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(f"# host notes\n{CANARY}-claude\n")
    (repo / "AGENTS.md").write_text(f"# host agents\n{CANARY}-agents\n")
    (repo / ".hermes.md").write_text(f"# host hermes\n{CANARY}-hermes\n")
    (profile / "config.yaml").write_text("{}")
    return repo, profile


def test_the_leak_exists_without_the_fix(tmp_path, host):
    repo, _ = host
    got = _run(tmp_path, {}, repo)
    assert not got["skip"] and CANARY in got["prompt"]


def test_a_workspace_alone_still_leaks_from_the_parents(tmp_path, host):
    repo, profile = host
    workspace = profile / "workspace"
    workspace.mkdir()
    got = _run(tmp_path, {"terminal": {"cwd": str(workspace)}}, repo)
    assert CANARY in got["prompt"]                  # .hermes.md and AGENTS.md are found walking up to the git root:
    #                                                 a working directory alone is not a boundary


def test_the_git_marker_in_the_workspace_stops_the_search_by_itself(tmp_path, host):
    repo, profile = host
    workspace = profile / "workspace"
    (workspace / ".git").mkdir(parents=True)
    got = _run(tmp_path, {"terminal": {"cwd": str(workspace)}}, repo)
    assert not got["skip"] and CANARY not in got["prompt"]      # the parent repository is never reached


def test_the_installer_config_keeps_every_canary_out(tmp_path, host):
    repo, profile = host
    cli.installer().isolate_context(profile)
    cfg = cli.installer().load_yaml(profile / "config.yaml")
    got = _run(tmp_path, cfg, repo)
    assert got["skip"] and got["prompt"] == "" and CANARY not in got["prompt"]
    assert cli.installer().workspace_confined(cfg, profile) and cli.installer().workspace_bounded(cfg, profile)
    assert (profile / "workspace" / ".git").is_dir()
    # either layer alone holds: the same config with the skip switched off
    cfg["gateway"]["platforms"]["emporia"]["skip_context_files"] = False
    assert CANARY not in _run(tmp_path, cfg, repo)["prompt"]


def test_hermes_reads_the_installer_memory_setting_as_memory_on_user_profile_off(tmp_path):
    """The operator's USER.md is out of the prompt, the agent's own MEMORY.md stays (Hermes's own flag reader)."""
    python, install = _hermes()
    prof = tmp_path / "alpha"
    prof.mkdir()
    (prof / "config.yaml").write_text("{}")
    cli.installer().isolate_context(prof)
    cfg = cli.installer().load_yaml(prof / "config.yaml")
    code = ("import sys, json; sys.path.insert(0, sys.argv[1]); "
            "from tools.memory_tool import get_builtin_memory_store_flags as f; "
            "print(json.dumps(f(json.loads(sys.argv[2]))))")
    out = subprocess.run([python, "-I", "-c", code, install, json.dumps(cfg)], capture_output=True, text=True,
                         timeout=60, stdin=subprocess.DEVNULL, env={"HERMES_HOME": str(tmp_path / "home"),
                                                                    "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-800:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [True, False]


def test_soul_is_still_the_identity_when_context_files_are_skipped():
    """agent/system_prompt.py:_identity_parts loads SOUL.md when `load_soul_identity or not skip_context_files`, and
    the gateway passes load_soul_identity=True unless a session policy ignores rules."""
    python_install = cli._hermes_python()
    if python_install is None:
        pytest.skip("Hermes is not installed here")
    src = (Path(python_install[1]) / "agent" / "system_prompt.py").read_text()
    assert "wants_soul = agent.load_soul_identity or not agent.skip_context_files" in src
    gw = (Path(python_install[1]) / "gateway" / "session_execution.py").read_text()
    assert "load_soul_identity=not bool(policy and policy.ignore_rules)" in gw
