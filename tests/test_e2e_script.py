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


# ── the cleanup, against a fake service directory (the real run is never started here) ──────────────

def _e2e():
    import importlib.util
    spec = importlib.util.spec_from_file_location("emporia_e2e_script", REPO / "scripts" / "e2e.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_hermes(tmp_path, *names, other=("gateway-e2e-player-ffff", "gateway-alpha", "gateway-e2e-judge-ab12x")):
    scan = tmp_path / "run" / "hermes-service"
    logs = tmp_path / "home" / "logs" / "gateways"
    for n in names:
        (scan / f"gateway-{n}").mkdir(parents=True)
        (logs / n).mkdir(parents=True)
        (logs / n / "current").write_text("log")
    for n in other:                      # another run's, a person's, and a near miss: none is touched
        (scan / n).mkdir(parents=True, exist_ok=True)
        (logs / n.removeprefix("gateway-")).mkdir(parents=True, exist_ok=True)
    return scan, logs


def test_cleanup_removes_only_this_runs_gateway_services_and_log_folders(tmp_path):
    e2e = _e2e()
    mine = ["e2e-judge-ab12", "e2e-player-ab12", "e2e-sdk-ab12"]
    scan, logs = _fake_hermes(tmp_path, *mine)
    deleted = []

    def delete(name):                    # what `hermes profile delete` does: the service goes
        deleted.append(name)
        import shutil
        shutil.rmtree(scan / f"gateway-{name}")

    res = e2e.cleanup_gateways(mine, "ab12", scandirs=(scan,), log_root=logs, delete=delete)
    assert sorted(deleted) == sorted(mine) and res["services_left"] == [] and res["logs_left"] == []
    assert sorted(Path(p).name for p in res["logs_removed"]) == sorted(mine)
    assert sorted(p.name for p in scan.iterdir()) == ["gateway-alpha", "gateway-e2e-judge-ab12x",
                                                     "gateway-e2e-player-ffff"]
    assert sorted(p.name for p in logs.iterdir()) == ["alpha", "e2e-judge-ab12x", "e2e-player-ffff"]


def test_cleanup_keeps_the_log_folder_of_a_service_it_could_not_remove_and_never_follows_links(tmp_path):
    e2e = _e2e()
    scan, logs = _fake_hermes(tmp_path, "e2e-judge-ab12", "e2e-beta-ab12")
    outside = tmp_path / "precious"
    outside.mkdir()
    import shutil
    shutil.rmtree(logs / "e2e-beta-ab12")
    (logs / "e2e-beta-ab12").symlink_to(outside)
    (scan / "gateway-e2e-beta-ab12").rmdir()           # beta's service is already gone; its log entry is a link
    res = e2e.cleanup_gateways(["e2e-judge-ab12", "e2e-beta-ab12"], "ab12", scandirs=(scan,), log_root=logs,
                               delete=lambda n: None)            # delete fails to remove judge's service
    assert res["services_left"] == ["gateway-e2e-judge-ab12"]
    assert (logs / "e2e-judge-ab12" / "current").exists(), "its service still runs: its log stays"
    assert outside.is_dir() and (logs / "e2e-beta-ab12").is_symlink(), "a link is not followed"


def test_cleanup_leaves_services_that_existed_before_the_run_and_foreign_names_alone(tmp_path):
    e2e = _e2e()
    scan, logs = _fake_hermes(tmp_path, "e2e-judge-ab12")
    called = []
    res = e2e.cleanup_gateways(["e2e-judge-ab12", "alpha", "e2e-judge-zzzz"], "ab12", scandirs=(scan,), log_root=logs,
                               delete=called.append, services_before={"gateway-e2e-judge-ab12"})
    assert called == [] and res["logs_removed"] == [] and (scan / "gateway-e2e-judge-ab12").is_dir()


def test_the_log_root_comes_from_hermes_home_not_a_hard_coded_path():
    e2e = _e2e()
    assert e2e.host_log_root("/srv/h") == Path("/srv/h/logs/gateways") and e2e.host_log_root("") is None
    assert "/opt/data" not in (REPO / "scripts" / "e2e.py").read_text()
    # the service root is read from the installed Hermes (fake install here), never from a path in this script
    install = Path(__file__).parent / "_fake_hermes_root"
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "hermes_cli").mkdir()
        (tmp / "hermes_cli" / "service_manager.py").write_text('S6_DYNAMIC_SCANDIR = Path("/srv/fake-services")\n')
        exe = tmp / "hermes"
        exe.write_text(f"#!{sys.executable}\nprint('Hermes Agent v9\\nInstall directory: {tmp}')\n")
        exe.chmod(0o755)
        assert e2e.service_roots(str(exe)) == (Path("/srv/fake-services"),)
    assert subprocess and not install.exists()
    assert e2e.service_roots("/nonexistent/hermes") == (Path("/run/hermes-service"),)   # unreadable: Hermes 0.23's
