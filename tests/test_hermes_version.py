"""Setup and doctor say when Hermes is older than v0.23.1 and how to upgrade; doctor advises queue mode."""
import subprocess
from types import SimpleNamespace

import pytest

from emporia import cli, hermes_version as hv

V0230 = "Hermes Agent v0.23.0 (2026.9.24) · upstream 2bd378ed\nInstall directory: /opt/hermes\nInstall method: docker\n"
V0231 = "Hermes Agent v0.23.1 (2026.10.1) · upstream abc\nInstall method: git\n"


def test_the_version_and_install_method_are_read_from_hermes_version_output():
    assert hv.parse(V0230) == ((0, 23, 0), "docker")
    assert hv.parse(V0231) == ((0, 23, 1), "git")
    assert hv.parse("") == (None, "") and hv.parse("nothing useful") == (None, "")
    assert hv.parse("Hermes Agent v0.24.0+3.gabc (x)\n")[0] == (0, 24, 0)


@pytest.mark.parametrize("version,old", [((0, 23, 0), True), ((0, 22, 9), True), ((0, 23, 1), False),
                                         ((0, 24, 0), False), ((1, 0, 0), False), (None, False)])
def test_only_older_than_0_23_1_is_a_problem(version, old):
    assert bool(hv.problem(version, "git")[0]) is old


def test_the_upgrade_step_is_the_one_hermes_recommends_for_how_it_was_installed():
    what, fix = hv.problem((0, 23, 0), "docker")
    assert "v0.23.0 is older than v0.23.1" in what and "cannot see Emporia" in what
    assert "docker pull nousresearch/hermes-agent:latest" in fix and hv.URL_INSTALL in fix
    assert "hermes update" in hv.problem((0, 23, 0), "git")[1]
    assert "pkg upgrade hermes-agent" in hv.upgrade_step("apt")
    assert hv.upgrade_step("unknown") == "hermes update"


def test_read_runs_hermes_version_and_is_quiet_when_hermes_is_missing(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(stdout=V0230)
    monkeypatch.setenv("EMPORIA_HERMES_BIN", "/x/hermes")
    assert hv.read(run) == ((0, 23, 0), "docker") and seen == [["/x/hermes", "--version"]]
    monkeypatch.setattr(hv.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("h", 1)))
    assert hv.read() == (None, "")
    monkeypatch.delenv("EMPORIA_HERMES_BIN")
    monkeypatch.setattr(hv.shutil, "which", lambda n: None)
    assert hv.read() == (None, "")


def test_setup_and_doctor_say_it(monkeypatch, capsys):
    monkeypatch.setattr(hv, "read", lambda run=None: ((0, 23, 0), "docker"))
    cli._say_if_hermes_is_old()
    out = capsys.readouterr().out
    assert "older than v0.23.1" in out and "docker pull nousresearch/hermes-agent:latest" in out
    rep = cli.Report()
    cli.doctor_hermes_version(rep)
    out = capsys.readouterr().out
    assert "older than v0.23.1" in out and "docker pull" in out
    monkeypatch.setattr(hv, "read", lambda run=None: ((0, 23, 1), "docker"))
    cli._say_if_hermes_is_old()
    assert capsys.readouterr().out == ""
    cli.doctor_hermes_version(cli.Report())
    assert "Hermes v0.23.1" in capsys.readouterr().out
    monkeypatch.setattr(hv, "read", lambda run=None: (None, ""))
    cli._say_if_hermes_is_old()
    cli.doctor_hermes_version(cli.Report())
    assert capsys.readouterr().out == ""


def test_doctor_advises_queue_mode_for_a_profile_that_is_not_on_it(capsys):
    cli.doctor_busy_input(cli.Report(), {"display": {"skin": "x"}}, "alpha")
    out = capsys.readouterr().out
    assert "busy_input_mode is not queue" in out and "hermes -p alpha config set display.busy_input_mode queue" in out
    cli.doctor_busy_input(cli.Report(), {}, "default")
    assert "hermes config set display.busy_input_mode queue" in capsys.readouterr().out
    for mode in ("queue", "steer"):
        cli.doctor_busy_input(cli.Report(), {"display": {"busy_input_mode": mode}}, "alpha")
        out = capsys.readouterr().out
        assert "busy_input_mode is not queue" not in out and mode in out
