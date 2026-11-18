"""2.1: a new relay starts with its transparency log on; a relay that already set it, or is already in use,
keeps what it has (and is told how to turn it on)."""
import argparse

from emporia import cli


def ask(env, **kw):
    return cli._ask_modules(argparse.Namespace(), env, ask_money=False, **kw)


def test_a_new_relay_starts_with_the_log_on():
    updates, want = ask({}, new_relay=True)
    assert updates["EMPORIA_TRANSPARENCY_LOG"] == "on" and "log" in want


def test_a_new_relay_with_a_value_keeps_it():
    updates, want = ask({"EMPORIA_TRANSPARENCY_LOG": "off"}, new_relay=True)
    assert updates.get("EMPORIA_TRANSPARENCY_LOG", "off") == "off" and "log" not in want


def test_an_existing_relay_with_no_value_stays_off():
    updates, want = ask({"EMPORIA_GAMIFY": "sheets"})
    assert updates.get("EMPORIA_TRANSPARENCY_LOG", "off") == "off" and "log" not in want


def test_setup_log_recommends_on_unless_the_operator_set_it_off(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "choose", lambda q, opts, default=0: seen.append((opts[default][0], opts[0][1])) or opts[default][0])
    assert cli._choose_log(now=False) is True and seen[-1][0] == "on" and "recommended" in seen[-1][1]
    assert cli._choose_log(now=False, set_off=True) is False
    assert cli._choose_log(now=True) is True


def test_a_relay_in_use_is_told_how_to_turn_it_on(tmp_path):
    (tmp_path / ".env").write_text("EMPORIA_GAMIFY=sheets\n")
    hint = cli._log_left_off_hint(tmp_path / ".env")
    assert "emporia setup log on" in hint and "does not change" in hint
    assert "--dir /srv/r" in cli._log_left_off_hint(tmp_path / ".env", "/srv/r")
    (tmp_path / ".env").write_text("EMPORIA_TRANSPARENCY_LOG=off\n")
    assert cli._log_left_off_hint(tmp_path / ".env") == ""
    assert cli._log_left_off_hint(tmp_path / "missing" / ".env") == ""
