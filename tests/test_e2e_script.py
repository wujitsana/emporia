"""The scripted end-to-end run (scripts/e2e.py): the Hermes plugin, a throwaway relay and three
agents, no model calls. Minutes long and it needs `hermes` on PATH, so it is off by default:

    EMPORIA_E2E=1 .venv/bin/python -m pytest -q tests/test_e2e_script.py

It installs the plugin and runtime from this repo's HEAD commit, so commit before running it."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.environ.get("EMPORIA_E2E") != "1",
                    reason="the end-to-end run takes minutes and a real Hermes: set EMPORIA_E2E=1")
def test_the_end_to_end_run_passes(tmp_path):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "e2e.py"), "--repo", str(REPO),
                        "--workdir", str(tmp_path)], capture_output=True, text=True, timeout=3600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "✗" not in r.stdout
