"""`emporia quickstart`: the onboarding smoke test. It runs the whole loop in a throwaway folder against a
relay it starts itself, with no model; then nothing of it may be left. `pytest -m smoke` runs just this."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from emporia import quickstart

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.smoke
def test_quickstart_runs_the_loop_and_cleans_up(tmp_path):
    base = tmp_path / "tmp"
    base.mkdir()
    env = {**os.environ, "TMPDIR": str(base), "PYTHONPATH": str(REPO / "src"), "EMPORIA_NO_DOTENV": "1",
           "HOME": str(tmp_path / "home")}
    # a real setting in the caller's environment must not reach the throwaway relay or agents
    env["EMPORIA_RELAY_URL"] = "https://relay.invalid"
    env["EMPORIA_AGENT_ID"] = "someone_real"
    r = subprocess.run([sys.executable, "-m", "emporia.cli", "quickstart"], env=env, capture_output=True,
                       text=True, timeout=240)
    out = r.stdout
    assert r.returncode == 0, out[-3000:] + r.stderr[-2000:]
    assert "It works on this machine" in out
    for step in ("start a relay of its own", "create your agent", "post a hello", "practice game",
                 "read it back", "cleaned up"):
        assert step in out, out
    assert "✗" not in out
    assert not list(base.iterdir()), "the throwaway folder is deleted"
    assert not (tmp_path / "home").exists(), "nothing was written to the caller's HOME"


def test_stop_refuses_a_folder_that_is_not_a_kept_quickstart(tmp_path):
    other = tmp_path / "my-real-folder"
    other.mkdir()
    (other / "keep.txt").write_text("mine")
    lines: list[str] = []
    assert quickstart.stop_kept(str(other), lines.append) == 1
    assert (other / "keep.txt").exists()


def test_children_get_a_clean_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("EMPORIA_RELAY_URL", "https://relay.invalid")
    monkeypatch.setenv("HERMES_HOME", "/somewhere/real")
    env = quickstart.clean_env(tmp_path)
    assert env["HOME"] == str(tmp_path) and env["EMPORIA_NO_DOTENV"] == "1"
    assert "EMPORIA_RELAY_URL" not in env and "HERMES_HOME" not in env


def test_the_house_runner_can_mark_inbox_read():
    """The tool module has get_inbox but no mark-read: the runner's helper adds it (it failed every pass
    that held a challenge)."""
    from types import SimpleNamespace

    from emporia import house
    tools = SimpleNamespace(AGENT_ID="h", _base=lambda: "http://x", _seg=lambda s: s)
    house.attach_tools(tools)
    assert callable(tools.mark_inbox_read) and callable(tools.relay_activity)
