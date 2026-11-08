"""Security review pass 2, its proposals built (branch security-followups-2).

P2: `house stop` stops only its own profile's runner. P6: the key guard matches paths as they
resolve. P7: MetaMask's Guard Mode check is remembered across requests. (P1, the operator's
verdict key, is gone with the verdicts: adopted ideas are credited by lab reports.)
Each test fails without its fix."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from emporia import signer
from emporia import signer_backends as sb
from tests.test_key_guard import guard
from tests.test_signer_backends import AGENT, _hold_like, mm, setup  # noqa: F401  (fixtures)


# ── P2: `house stop` stops only its own profile's runner ─────────────────────

def _fake_runner(home: Path) -> subprocess.Popen:
    """A process whose command line reads like a house runner's, for `home`: it only sleeps."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "emporia.house",
                             "--home", str(home.resolve()), "--interval", "20"])


def test_house_stop_stops_its_own_profiles_runner_and_no_other(tmp_path, monkeypatch, capsys):
    from emporia import cli
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path / n, n, {"EMPORIA_AGENT_ID": n}))
    runner_b = _fake_runner(b)
    try:
        time.sleep(0.3)
        cli._house_files(a)[0].write_text(str(runner_b.pid))     # A's agent points A's PID file at B's runner
        assert cli.main(["house", "stop", "a"]) == 1
        assert "not a's house runner" in capsys.readouterr().out
        assert runner_b.poll() is None, "B's runner still runs"
        assert cli.main(["house", "status", "a"]) == 0 and "not running here" in capsys.readouterr().out
        cli._house_files(b)[0].write_text(str(runner_b.pid))
        assert cli._house_pid(b) == runner_b.pid
        assert cli.main(["house", "stop", "b"]) == 0 and "stopped" in capsys.readouterr().out
        runner_b.wait(timeout=10)
    finally:
        if runner_b.poll() is None:
            runner_b.kill()
            runner_b.wait()


def test_the_runner_names_its_profile_in_its_command_line(tmp_path, monkeypatch):
    from emporia import cli
    started = []

    class P:
        pid = 4242

        def poll(self):
            return None
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path, n, {"EMPORIA_AGENT_ID": n}))
    monkeypatch.setattr(cli, "_mcp_env", lambda p: {})
    monkeypatch.setattr(cli, "installer", lambda: type("I", (), {"harvest_env": staticmethod(lambda p: {
        "EMPORIA_PLAYBOOK": "host"})})())
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: started.append(argv) or P())
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["house", "start", "h"]) == 0
    argv = started[0]
    assert argv[1:3] == ["-m", "emporia.house"] and argv[argv.index("--home") + 1] == str(tmp_path.resolve())


# ── P6: paths as they resolve ────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"command": "cat ~/.hermes//keys/alpha.json"},
    {"command": "ls ~/.hermes/./keys"},
    {"command": "tar czf /x/k.tgz ~/.hermes/x/../keys"},
    {"command": "cat ~/.hermes/a/b/../../keys/z"},
    {"command": "cd ~/.hermes && tar czf /x/k.tgz keys"},
    {"command": "cd ~/.hermes; ls keys"},
    {"command": "cd /opt/data/profiles && cat .signer/admin.token"},
    {"command": "cat /opt/data/profiles//.signer/policy.json"},
    {"command": "cd ~/plugin-data && ls emporia/console"},
    {"command": "ls keys", "workdir": "~/.hermes"},
    {"path": "/home/a/.hermes/./keys/b.json"},
])
def test_the_key_guard_matches_paths_as_they_resolve(args):
    r = guard.pre_tool_call("terminal", args)
    assert r and r["action"] == "block", args


@pytest.mark.parametrize("cmd", ["cd src && ls emporia", "cd docs && cat protocol.md", "echo a//b ./c ../d",
                                 "curl http://localhost:8088//health", "cd tests && ../.venv/bin/python -m pytest -q"])
def test_normalising_adds_no_false_positive(cmd):
    assert guard.pre_tool_call("terminal", {"command": cmd}) is None


# ── P7: MetaMask's Guard Mode check, remembered across requests ──────────────

def _mode_checks(m):
    return [c for c in m["state"]()["calls"] if c[:3] == ["wallet", "trading-mode", "get"]]


def test_guard_mode_is_checked_once_per_interval_across_requests(setup, mm, monkeypatch):  # noqa: F811
    ref = signer.get_backend("mm1").make(AGENT, "evm")
    msg = _hold_like(ref)
    for _ in range(3):                          # a new back end per request, as the signer builds them
        signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    assert len(_mode_checks(mm)) == 1
    clock = [time.monotonic() + sb.MetaMaskBackend.GUARD_TTL_S + 1]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    st = mm["state"]()
    st["wallets"][ref]["mode"] = "beast"        # switched after the last check
    mm["set"](wallets=st["wallets"])
    with pytest.raises(sb.BackendError, match="Beast Mode"):
        signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    with pytest.raises(sb.BackendError, match="Beast Mode"):
        signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)   # a refusal is never remembered
    assert len(_mode_checks(mm)) == 3
    st["wallets"][ref]["mode"] = "guard"
    mm["set"](wallets=st["wallets"])
    signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    b = signer.get_backend("mm1")
    signer.save_backend("mm1", "metamask", {**b.settings, "wait_seconds": "30"}, b.secrets)   # set up again
    signer.get_backend("mm1").sign_typed_data(AGENT, msg, ref)
    assert len(_mode_checks(mm)) == 5, "a back end set up again is asked afresh"


