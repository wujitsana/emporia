"""Rounds: one Hermes cron job per profile, made only through the `hermes cron` CLI, and its prompt."""
import argparse
from pathlib import Path

import pytest

from emporia import cli, rounds

REPO = Path(__file__).resolve().parents[1]
LIST = """
┌──────┐
│ Scheduled Jobs (profile: shop) │
└──────┘

  aaaaaaaaaaaa [active]
    Name:      Daily digest
    Schedule:  every 1440m
    Deliver:   telegram

  bbbbbbbbbbbb [paused]
    Name:      Emporia — rounds
    Schedule:  every 240m
    Deliver:   local
    Skills:    emporia:emporia
"""


class FakeHermes:
    def __init__(self, listing=""):
        self.calls, self.listing = [], listing

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "list" in argv:
            return self.listing
        if "create" in argv:
            return "Created job: cccccccccccc\n  Name: Emporia — rounds\n"
        return ""


@pytest.fixture
def hermes(monkeypatch):
    fake = FakeHermes()
    monkeypatch.setattr(rounds, "run_hermes", fake)
    return fake


def _args(action, every="4h", agent="shop"):
    return argparse.Namespace(action=action, every=every, agent=agent)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    shop = tmp_path / "profiles" / "shop"
    shop.mkdir(parents=True)
    (shop / "config.yaml").write_text("{}")
    monkeypatch.setattr(cli, "profile_dir", lambda name: shop if name == "shop" else tmp_path / name)
    monkeypatch.setattr(cli.installer(), "harvest_env", lambda p: {})
    return shop


def test_on_creates_one_local_job_with_the_shipped_prompt(hermes):
    msg = rounds.turn_on("shop", "2h")
    create = hermes.calls[-1]
    assert create[:4] == ["-p", "shop", "cron", "create"] and create[4] == "every 2h"
    assert create[5] == rounds.prompt_text() and "--name" in create
    assert create[create.index("--name") + 1] == "Emporia — rounds"
    assert create[create.index("--deliver") + 1] == "local" and create[create.index("--failure-deliver") + 1] == "local"
    assert create[create.index("--skill") + 1] == "emporia:emporia"
    assert "cccccccccccc" in msg and "every 2h" in msg


def test_the_default_profile_takes_no_p_flag(hermes):
    rounds.turn_on("default", "4h")
    assert hermes.calls[-1][:2] == ["cron", "create"]


def test_on_again_edits_and_resumes_its_own_paused_job_and_makes_no_second(hermes):
    hermes.listing = LIST
    rounds.turn_on("shop", "8h")
    verbs = [c[3] for c in hermes.calls if c[2:3] == ["cron"]]
    assert "create" not in verbs and verbs.count("edit") == 1 and "resume" in verbs
    edit = next(c for c in hermes.calls if "edit" in c)
    assert edit[4] == "bbbbbbbbbbbb" and "every 8h" in edit


def test_off_removes_only_its_own_job(hermes):
    hermes.listing = LIST
    rounds.turn_off("shop")
    removes = [c for c in hermes.calls if "remove" in c]
    assert removes == [["-p", "shop", "cron", "remove", "bbbbbbbbbbbb"]]
    assert all("aaaaaaaaaaaa" not in c for c in hermes.calls), "another job is never touched"


def test_pause_resume_and_status_only_touch_its_job(hermes):
    hermes.listing = LIST
    rounds.pause("shop")
    rounds.resume("shop")
    touched = {c[4] for c in hermes.calls if c[3] in ("pause", "resume")}
    assert touched == {"bbbbbbbbbbbb"}
    assert "paused" in rounds.status_line("shop") and "every 240m" in rounds.status_line("shop")


def test_with_no_job_there_is_nothing_to_remove(hermes):
    hermes.listing = "  aaaaaaaaaaaa [active]\n    Name:      Daily digest\n"
    assert "not on" in rounds.turn_off("shop")
    assert not [c for c in hermes.calls if "remove" in c]


def test_only_the_offered_intervals(hermes):
    with pytest.raises(rounds.RoundsError):
        rounds.turn_on("shop", "5m")
    assert not hermes.calls


def test_the_estimate_is_honest_about_turns_and_tokens():
    text = rounds.estimate("4h")
    assert "6 rounds a day" in text and "model turn" in text and "tokens" in text


def test_a_failing_hermes_is_an_error_not_a_crash(monkeypatch):
    def boom(argv):
        raise rounds.RoundsError("hermes failed")
    monkeypatch.setattr(rounds, "run_hermes", boom)
    assert rounds.status_line("shop").startswith("unknown")


def test_the_cli_builds_the_commands(hermes, profile, capsys):
    assert cli.cmd_rounds(_args("on", "12h")) == 0
    assert "every 12h" in hermes.calls[-1]
    hermes.listing = LIST
    assert cli.cmd_rounds(_args("off")) == 0
    assert any("remove" in c for c in hermes.calls)
    assert cli.cmd_rounds(_args("status")) == 0
    assert "Rounds" in capsys.readouterr().out


def test_the_prompt_ships_with_the_plugin_and_carries_the_safety_lines():
    text = (REPO / "hermes_plugin" / "emporia" / rounds.PROMPT_FILE).read_text()
    assert rounds.prompt_version(text) == "1"
    for line in ("whats_next", "whats_happening", "at most 3 actions", "data, never instructions",
                 "Never pay", "accept or confirm a deal", "[SILENT]", "outside Emporia"):
        assert line in text, line


def test_a_round_delivers_nowhere_chatty(hermes):
    rounds.turn_on("shop")
    create = hermes.calls[-1]
    assert "origin" not in create and "telegram" not in create


# -- setup: off without a terminal unless asked ------------------------------------

def _opt(rounds_flag, tmp_path, monkeypatch, tty=False):
    monkeypatch.setattr(cli, "_TTY", tty)
    seen = []
    monkeypatch.setattr(rounds, "turn_on", lambda p, every, plugin_dir=None: seen.append((p, every)) or "on")
    monkeypatch.setattr(cli.installer(), "harvest_env", lambda p: {})
    prof = tmp_path / "profiles" / "shop"
    prof.mkdir(parents=True, exist_ok=True)
    cli._setup_optional(prof, "shop", argparse.Namespace(rounds=rounds_flag), False)
    return seen


def test_setup_without_a_terminal_makes_no_job(tmp_path, monkeypatch):
    assert _opt("", tmp_path, monkeypatch) == []


def test_setup_without_a_terminal_makes_one_when_told(tmp_path, monkeypatch):
    assert _opt("4h", tmp_path, monkeypatch) == [("shop", "4h")]
    assert _opt("off", tmp_path, monkeypatch) == []


def test_doctor_shows_the_wake_mode_and_the_rounds_job(tmp_path, monkeypatch, capsys):
    prof = tmp_path / "profiles" / "shop"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("{}")
    monkeypatch.setattr(rounds, "status_line", lambda p: "active, every 240m (job x)")
    rep = cli.Report(compact=False)
    cli.doctor_agent_hardening(rep, prof, {"EMPORIA_WAKE": "open"})
    out = capsys.readouterr().out
    assert "wake mode open" in out and "rounds active, every 240m" in out
    cli.doctor_agent_hardening(rep, prof, {"EMPORIA_WAKE": "sometimes"})
    assert "treated as closed" in capsys.readouterr().out
