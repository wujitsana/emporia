"""Hints name `emporia …` commands; where the Hermes plugin installed the runtime there is no
`emporia` on PATH, so they say `hermes emporia …` (found by the end-to-end run)."""
import argparse
from pathlib import Path

from emporia import cli, cli_hints, playbook
from emporia.cli_hints import localize


def test_a_runtime_in_plugin_data_is_the_plugins():
    assert cli_hints.via_plugin(Path("/h/profiles/judge/plugin-data/emporia/runtime"))
    assert not cli_hints.via_plugin(Path("/home/me/src/emporia"))
    assert not cli_hints.via_plugin(cli_hints._RUNTIME)          # this checkout


def test_hints_say_hermes_emporia_under_the_plugin():
    cases = {
        "your operator sets it: emporia agent playbook": "your operator sets it: hermes emporia agent playbook",
        "`emporia wallet create shop`": "`hermes emporia wallet create shop`",
        "emporia setup agent shop   (strips them)": "hermes emporia setup --profile shop   (strips them)",
        "Check it any time: emporia doctor --profile shop": "Check it any time: hermes -p shop emporia doctor",
        "emporia doctor --all": "hermes emporia doctor --all",
        "emporia setup dashboard shop": "hermes emporia setup dashboard shop",
    }
    for hint, plugin_form in cases.items():
        assert localize(hint, plugin=True) == plugin_form
        assert localize(hint, plugin=False) == hint
    for untouched in ("hermes emporia update", "hermes -p shop emporia doctor", "the emporia-relay folder",
                      "ln -s .venv/bin/emporia ~/.local/bin/emporia", "the emporia tab"):
        assert localize(untouched, plugin=True) == untouched


def test_the_hints_know_every_command():
    commands = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
    assert set(cli_hints.COMMANDS) == set(commands)


def test_the_operator_hints_in_tool_answers_name_the_command_this_install_has(monkeypatch):
    """Review pass (11-05): two tool answers tell the agent what its operator can run when it
    could not be asked; they said `emporia …` even where only `hermes emporia …` exists."""
    from emporia import mcp_server
    monkeypatch.setattr(cli_hints, "via_plugin", lambda runtime=None: True)
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda *a, **k: None)
    dms = mcp_server.set_cross_relay_dms(True, agent_id="hint_me")
    assert not dms["success"] and "they can run: hermes emporia dms hint_me" in dms["error"], dms
    from types import SimpleNamespace

    from emporia import visits
    monkeypatch.setattr(visits, "visit_needs_yes", lambda url, linked: True)
    monkeypatch.setattr(mcp_server, "fetch", SimpleNamespace(get=lambda *a, **k: SimpleNamespace(json=lambda: {})))
    away = mcp_server.visit_relay("https://relay-b.test", agent_id="hint_me")
    assert not away["success"] and "allow it: hermes emporia visits hint_me --allow" in away["error"], away


def test_playbook_refusals_and_doctor_fixes_name_the_command_this_install_has(monkeypatch, capsys):
    monkeypatch.setattr(cli_hints, "via_plugin", lambda runtime=None: True)
    pb = playbook.Playbook("player", playbook.NONE, 100, "anyone", False, False, "model")
    for refusal in (playbook.may_sell(pb), playbook.may_buy(pb), playbook.may_play(pb, "emporia:chess:v1")):
        assert refusal.endswith("your operator sets it: hermes emporia agent playbook"), refusal
    rep = cli.Report()
    rep.warn("no wallet", "emporia wallet create shop")
    assert "fix: hermes emporia wallet create shop" in capsys.readouterr().out
