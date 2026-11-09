"""scripts/verify.py: the one command that runs every check, in order, with a pass or fail line each (its steps are
faked here; running them for real is what the script is for)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _verify():
    spec = importlib.util.spec_from_file_location("emporia_verify_script", ROOT / "scripts" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_steps_run_in_order_and_each_prints_a_line(monkeypatch, capsys):
    v = _verify()
    monkeypatch.setattr(v, "RUNNERS", {n: (lambda n=n: (v.PASS, f"{n} ok", "")) for n in v.STEPS})
    assert v.main([]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("PASS")]
    assert [ln.split()[1] for ln in lines] == ["pytest", "portable", "dashboard", "plugin"]    # e2e only on request
    assert "4 passed, 0 failed, 0 skipped" in out
    assert v.main(["--e2e"]) == 0
    assert capsys.readouterr().out.count("PASS  e2e") == 1


def test_a_failure_is_shown_with_its_detail_and_sets_the_exit_code(monkeypatch, capsys):
    v = _verify()
    runners = {n: (lambda n=n: (v.PASS, "ok", "")) for n in v.STEPS}
    runners["portable"] = lambda: (v.FAIL, "out of date", "      the diff")
    monkeypatch.setattr(v, "RUNNERS", runners)
    assert v.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL  portable" in out and "the diff" in out and "failed: portable" in out
    assert "PASS  plugin" in out, "later steps still run"


def test_a_skipped_step_is_not_a_pass(monkeypatch, capsys):
    v = _verify()
    runners = {n: (lambda n=n: (v.PASS, "ok", "")) for n in v.STEPS}
    runners["plugin"] = lambda: (v.SKIP, "hermes is not on PATH", "")
    monkeypatch.setattr(v, "RUNNERS", runners)
    assert v.main(["--skip", "pytest"]) == 0
    out = capsys.readouterr().out
    assert "SKIP  plugin" in out and "pytest" not in out.split("… ")[1]
    assert "2 passed, 0 failed, 1 skipped" in out and "skipped (not a pass): plugin" in out


def test_the_plugin_step_skips_without_hermes_and_fails_on_a_warning(monkeypatch):
    v = _verify()
    monkeypatch.setattr(v.shutil, "which", lambda name: None)
    assert v.step_plugin()[0] == v.SKIP
    monkeypatch.setattr(v.shutil, "which", lambda name: "/bin/hermes")
    outputs = iter([(0, ""), (0, ""), (0, "✓ manifest\n⚠ security scan caution: dump_all_env (SKILL.md:32)\nValidation passed.\n")])
    monkeypatch.setattr(v, "run", lambda *a, **k: next(outputs))
    status, note, detail = v.step_plugin()
    assert status == v.FAIL and "1 warning" in note and "dump_all_env" in detail
    outputs = iter([(0, ""), (0, ""), (0, "✓ manifest\nValidation passed.\n")])
    assert v.step_plugin()[0] == v.PASS


def test_the_dashboard_step_skips_without_node_modules(monkeypatch, tmp_path):
    v = _verify()
    monkeypatch.setattr(v, "DASHBOARD", tmp_path)
    monkeypatch.setattr(v.shutil, "which", lambda name: "/usr/bin/npm")
    status, note, _ = v.step_dashboard()
    assert status == v.SKIP and "node_modules" in note
    (tmp_path / "node_modules").mkdir()
    calls = []
    monkeypatch.setattr(v, "run", lambda argv, **k: calls.append(argv[-1]) or (0, ""))
    assert v.step_dashboard()[0] == v.PASS and calls == ["typecheck", "lint", "test"]
    monkeypatch.setattr(v, "run", lambda argv, **k: (1, "boom") if argv[-1] == "lint" else (0, ""))
    status, note, detail = v.step_dashboard()
    assert status == v.FAIL and "lint" in note and "boom" in detail


def test_children_never_read_a_profile_env():
    assert _verify().child_env()["EMPORIA_NO_DOTENV"] == "1"


@pytest.mark.parametrize("name", ["pytest", "portable", "dashboard", "plugin", "e2e"])
def test_every_step_has_a_runner_and_the_readme_names_the_script(name):
    v = _verify()
    assert name in v.RUNNERS and name in v.STEPS
    assert "scripts/verify.py" in (ROOT / "README.md").read_text()
