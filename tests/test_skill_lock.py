"""Skill locks through Hermes's `curator lock`: only what is in the profile, never faked for plugin skills."""
import argparse

import pytest

from emporia import cli, rounds, setup_choices as sc, skill_lock


class FakeHermes:
    def __init__(self, refuse=()):
        self.calls, self.refuse = [], set(refuse)

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[-1] in self.refuse:
            raise rounds.RoundsError("hermes curator lock failed: no skill named")
        return "curator: locked\n"


@pytest.fixture
def hermes(monkeypatch):
    fake = FakeHermes()
    monkeypatch.setattr(rounds, "run_hermes", fake)
    return fake


def _skill(profile, rel):
    d = profile / "skills" / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")


@pytest.fixture
def profile(tmp_path):
    p = tmp_path / "shop"
    p.mkdir()
    (p / "config.yaml").write_text("{}")
    _skill(p, "payments/stripe-projects")
    _skill(p, "payments/stripe-link-cli")
    _skill(p, "chess-notes")
    return p


def test_only_payment_skills_are_lockable(profile):
    assert skill_lock.present(profile) == ["stripe-link-cli", "stripe-projects"]


def test_lock_asks_hermes_for_each_one_in_this_profile(hermes, profile):
    locked, notes = skill_lock.lock("shop", profile)
    assert locked == ["stripe-link-cli", "stripe-projects"]
    assert hermes.calls == [["-p", "shop", "curator", "lock", n] for n in locked]
    assert any("read-only" in n and "emporia:emporia" in n for n in notes)


def test_the_default_profile_takes_no_p_flag(hermes, profile):
    skill_lock.lock("default", profile)
    assert hermes.calls[0][:2] == ["curator", "lock"]


def test_a_refused_lock_is_reported_not_counted(monkeypatch, profile):
    monkeypatch.setattr(rounds, "run_hermes", FakeHermes(refuse={"stripe-projects"}))
    locked, notes = skill_lock.lock("shop", profile)
    assert "stripe-projects" not in locked and any(n.startswith("stripe-projects: not locked") for n in notes)


def test_a_profile_with_no_such_skill_calls_hermes_never(hermes, tmp_path):
    (tmp_path / "skills").mkdir()
    locked, notes = skill_lock.lock("shop", tmp_path)
    assert locked == [] and hermes.calls == [] and "nothing to lock" in notes[0]


def test_plugin_skills_are_never_sent_to_hermes(hermes, profile):
    skill_lock.lock("shop", profile)
    assert not any(c[-1].startswith("emporia") for c in hermes.calls)


def test_unlock(hermes, profile):
    assert skill_lock.unlock("shop", profile) == ["stripe-link-cli", "stripe-projects"]
    assert all(c[2:4] == ["curator", "unlock"] or c[2] == "curator" for c in hermes.calls)


def test_the_cli_locks_and_records_the_choice(hermes, profile, monkeypatch, capsys):
    saved = {}
    monkeypatch.setattr(cli, "profile_dir", lambda name: profile)
    monkeypatch.setattr(cli, "merge_env_file", lambda path, updates: saved.update(updates) or list(updates))
    assert cli.main(["skill-lock", "on", "--agent", "shop"]) == 0
    out = capsys.readouterr().out
    assert "stripe-projects" in out and "Undo" in out and saved == {"EMPORIA_SKILL_LOCK": "on"}
    assert cli.main(["skill-lock", "off", "--agent", "shop"]) == 0
    assert saved == {"EMPORIA_SKILL_LOCK": "off"}


def test_the_question_recommends_yes_and_the_closing_block_lists_it():
    assert sc.CHOICES["skill_lock"].default == "yes"
    text = "\n".join(sc.hermes_lines("on", ["stripe-projects"]))
    assert "Skill lock" in text and "stripe-projects" in text and "emporia skill-lock on|off|status" in text
    assert "off" in "\n".join(sc.hermes_lines("off", [])).split("\n")[0]


def _guard():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "key_guard.py"
    spec = importlib.util.spec_from_file_location("emporia_key_guard_sl", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command", ["emporia skill-lock off", "hermes -p shop emporia skill-lock on",
                                     "echo EMPORIA_SKILL_LOCK=off >> /x/profiles/shop/.env",
                                     "hermes config set EMPORIA_SKILL_LOCK off"])
def test_an_agent_cannot_change_the_lock(command):
    out = _guard().pre_tool_call(tool_name="terminal", args={"command": command})
    assert out and out["action"] == "block", command


@pytest.mark.parametrize("command", ["emporia skill-lock status"])
def test_reading_them_passes(command):
    assert _guard().pre_tool_call(tool_name="terminal", args={"command": command}) is None


def test_setup_locks_by_default_without_a_terminal_and_records_only_a_departure_from_the_default(
        hermes, profile, monkeypatch):
    saved = {}
    monkeypatch.setattr(cli, "_TTY", False)
    monkeypatch.setattr(cli.installer(), "harvest_env", lambda p: {})
    monkeypatch.setattr(cli, "merge_env_file", lambda path, updates: saved.update(updates) or list(updates))
    lines = cli._setup_hermes_extras(profile, "shop", argparse.Namespace(skill_lock=""))
    assert [c[-1] for c in hermes.calls] == ["stripe-link-cli", "stripe-projects"]
    assert saved == {} and "stripe-projects" in "\n".join(lines)
    lines = cli._setup_hermes_extras(profile, "shop", argparse.Namespace(skill_lock="off"))
    assert saved == {"EMPORIA_SKILL_LOCK": "off"}
    assert any("unlock" in c for c in hermes.calls)
