"""Setup says what each setting does, marks the recommended answer (which is the default), offers a fast path,
and closes with every setting it installed."""
import builtins
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from emporia import cli, rounds, setup_choices as sc

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emporia_setup_menu_t", REPO / "hermes_plugin" / "emporia" / "setup_menu.py")
menu = importlib.util.module_from_spec(_spec)
sys.modules["emporia_setup_menu_t"] = menu
_spec.loader.exec_module(menu)


@pytest.mark.parametrize("key", list(sc.CHOICES))
def test_every_question_has_one_recommended_option_equal_to_its_default(key):
    c = sc.CHOICES[key]
    flagged = [v for v, label in c.labels() if "(recommended)" in label]
    if key == "optional":
        assert set(flagged) == c.recommended()
        return
    assert flagged == [c.default] and c.labels()[c.index()][0] == c.default
    assert c.question.strip() and c.matters.strip() and c.change.strip()
    for _, label in c.labels():
        first, *rest = label.split("\n")
        assert rest and rest[0].strip(), "every option says what it does on the next line"


def test_the_questions_setup_asks_are_all_in_the_registry():
    for key in ("profile", "wake", "rounds", "optional", "playbook", "role", "keys", "wallet", "register",
                "sync_model", "signer"):
        assert key in sc.CHOICES
    assert sc.default("wake") == "direct" and sc.default("rounds") == "4h"
    assert [v for v, _ in sc.CHOICES["role"].labels()] == [k for k, _ in cli.ROLES]
    from emporia import playbook
    assert [v for v, _ in sc.CHOICES["playbook"].labels()] == list(playbook.PRESETS)


def test_the_registry_defaults_are_the_defaults_the_code_uses():
    from emporia import wake
    assert sc.default("wake") == wake.DEFAULT
    assert sc.default("playbook") == "custom" and sc.default("role") == cli.ROLES[0][0]
    from emporia import rounds as R
    assert sc.default("rounds") == R.DEFAULT_EVERY


def test_no_ansi_without_a_terminal_or_with_no_color(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    block = "\n".join(sc.CHOICES["wake"].text() + menu.lines("q", [menu.Item("a", "x", True)]))
    assert "\x1b" not in block and "(recommended)" in block
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in "\n".join(sc.CHOICES["wake"].text() + menu.lines("q", [menu.Item("a", "x", True)]))
    monkeypatch.delenv("NO_COLOR")
    colored = "\n".join(sc.CHOICES["wake"].text())
    assert "\x1b[2m" in colored and "\x1b[32;1m(recommended)" in colored, "dim explanation, highlighted recommendation"


def test_the_layout_puts_each_explanation_on_the_next_line_indented(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    text = sc.CHOICES["wake"].text()
    assert text[0] == "Who can wake your agent?"
    assert text[2].strip() == "1) direct  (recommended)" and text[3].startswith("         Anyone can challenge")
    assert text[4].strip() == "2) open" and text[6].strip() == "3) closed"


def test_the_closing_block_lists_every_setting_with_value_why_and_how_to_change(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    lines = sc.installed_lines({}, role="marketplace", keys="env", wallets=[], rounds="4h", name="shop")
    block = "\n".join(lines)
    for setting in ("Role", "Keys", "Wallets", "Wake mode", "Rounds", "Playbook", "Declare model", "Thread limit",
                    "Daily turns", "Relay turn tools"):
        assert setting in block, setting
    assert "Emporia — rounds" in block and "emporia rounds off" in block
    assert "(recommended default)" in block, "what the person did not answer is marked as the default"
    assert block.count("Change:") == len(lines)
    assert "\x1b" not in block
    answered = "\n".join(sc.installed_lines({}, role="marketplace", keys="env", wallets=[], rounds="",
                                            chosen=set(sc.CHOICES)))
    assert "(recommended default)" not in answered


# -- the menu: pure key handling, fake key streams ---------------------------------

ITEMS = [menu.Item("a", "first", False), menu.Item("b", "second", True), menu.Item("c", "third")]


def test_the_cursor_starts_on_the_recommended_option():
    assert menu.start_index(ITEMS) == 1


@pytest.mark.parametrize("keys,expected", [
    ([menu.ENTER], 1),
    ([menu.DOWN, menu.ENTER], 2),
    ([menu.DOWN, menu.DOWN, menu.ENTER], 0),               # wraps
    ([menu.UP, menu.UP, menu.ENTER], 2),
    (["3", menu.ENTER], 2),                               # a number key jumps
    (["9", "x", menu.ENTER], 1),                          # out of range and unknown keys change nothing
    ([menu.ESC], None), (["q"], None), ([], None),
])
def test_key_handling(keys, expected):
    assert menu.run(ITEMS, iter(keys)) == expected


def test_the_numbered_fallback_takes_the_recommended_on_enter(capsys):
    answers = iter(["", ])
    assert menu.numbered("q", ITEMS, lambda p: next(answers)) == 1
    answers = iter(["x", "3"])
    assert menu.numbered("q", ITEMS, lambda p: next(answers)) == 2


def test_no_raw_terminal_means_the_caller_falls_back(monkeypatch):
    monkeypatch.setattr(menu, "available", lambda: False)
    assert menu.select("q", ITEMS) is False


def test_the_terminal_is_restored_after_an_exception(monkeypatch):
    import termios
    import tty
    calls = []
    monkeypatch.setattr(menu, "available", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0, raising=False)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: "SAVED")
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, saved: calls.append(saved))
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)

    def boom(fd):
        raise KeyboardInterrupt
        yield  # pragma: no cover
    monkeypatch.setattr(menu, "_keys", boom)
    with pytest.raises(KeyboardInterrupt):
        menu.select("q", ITEMS, out=open(os.devnull, "w"))
    assert calls == ["SAVED"], "cbreak mode is undone however the menu ends"


def test_raw_mode_failing_falls_back(monkeypatch):
    import termios
    import tty
    monkeypatch.setattr(menu, "available", lambda: True)
    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0, raising=False)
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: "SAVED")
    monkeypatch.setattr(termios, "tcsetattr", lambda *a: None)

    def refuse(fd):
        raise OSError("no raw mode")
    monkeypatch.setattr(tty, "setcbreak", refuse)
    assert menu.select("q", ITEMS, out=open(os.devnull, "w")) is False


# -- both setup paths ---------------------------------------------------------------

@pytest.fixture
def profiles(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    op = root / "op"
    op.mkdir(parents=True)
    (op / "config.yaml").write_text(yaml.dump({"model": {"provider": "openrouter", "default": "x/y"}}))
    (op / ".env").write_text("OPENROUTER_API_KEY=sk-or-x\n")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0, url="": None)
    inst = cli.installer()
    monkeypatch.setattr(inst, "_hermes_profile_create", lambda name: (root / name).mkdir())
    monkeypatch.setattr(inst, "install_platform_plugin", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_install_skill_link", lambda *a, **k: None)
    monkeypatch.setattr(inst, "_TOUCHED_PROFILES", [])
    jobs = []
    monkeypatch.setattr(rounds, "turn_on", lambda p, every, plugin_dir=None: jobs.append((p, every)) or f"rounds on: {every}")
    monkeypatch.setattr(rounds, "status_line", lambda p: "off")
    return root, jobs


SETTINGS = ("Role", "Keys", "Wallets", "Wake mode", "Rounds", "Playbook", "Declare model", "Thread limit",
            "Daily turns", "Relay turn tools")


def _run(argv, capsys):
    with patch.dict(os.environ):
        rc = cli.main(argv)
    return rc, capsys.readouterr().out


def test_non_interactive_setup_uses_the_defaults_and_lists_each_choice(profiles, capsys, monkeypatch):
    root, jobs = profiles
    monkeypatch.setattr(cli, "_TTY", False)
    rc, out = _run(["setup", "agent", "nia", "--relay-url", "http://relay.test", "--agent-id", "nia", "--keys", "files"],
                   capsys)
    assert rc == 0
    for setting in SETTINGS:
        assert setting in out, setting
    assert "Settings installed for nia" in out and "(recommended default)" in out
    assert "EMPORIA_WAKE=direct" in (root / "nia" / ".env").read_text()
    assert jobs == [], "no terminal: no cron job unless --rounds is given"
    assert "emporia wake" in out and "emporia rounds on" in out


def test_non_interactive_rounds_flag_makes_the_job(profiles, capsys, monkeypatch):
    root, jobs = profiles
    monkeypatch.setattr(cli, "_TTY", False)
    rc, out = _run(["setup", "agent", "nia", "--relay-url", "http://relay.test", "--agent-id", "nia", "--keys", "files",
                    "--rounds", "2h", "--wake", "open"], capsys)
    assert rc == 0 and jobs == [("nia", "2h")]
    assert "EMPORIA_WAKE=open" in (root / "nia" / ".env").read_text()
    assert "(recommended default)" in out  # the playbook and the rest were not answered


def _scripted_input(monkeypatch, first):
    asked = []
    answers = iter([first])

    def fake(prompt=""):
        asked.append(prompt)
        return next(answers, "")
    monkeypatch.setattr(builtins, "input", fake)
    return asked


def test_the_fast_path_takes_every_recommendation_without_asking(profiles, capsys, monkeypatch):
    root, jobs = profiles
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(cli, "_menus", lambda: None)
    asked = _scripted_input(monkeypatch, "")          # Enter on "use the recommended settings?" = yes
    rc, out = _run(["setup", "agent", "fay", "--relay-url", "http://relay.test", "--agent-id", "fay"], capsys)
    assert rc == 0, out
    assert "Use the recommended settings for everything?" in out
    assert len(asked) <= 3, f"only the first question (and nothing else) was asked: {asked}"
    env = (root / "fay" / ".env").read_text()
    assert "EMPORIA_WAKE=direct" in env and "EMPORIA_KEY_STORAGE=env" in env
    assert jobs == [("fay", "4h")]
    for setting in SETTINGS:
        assert setting in out
    assert cli._TTY is True, "the fast path restores the terminal flag when setup ends"


def test_declining_the_fast_path_asks_each_question_with_the_recommended_first(profiles, capsys, monkeypatch):
    root, jobs = profiles
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(cli, "_menus", lambda: None)
    asked = _scripted_input(monkeypatch, "n")         # "no", then Enter on everything: the recommended answers
    rc, out = _run(["setup", "agent", "dan", "--relay-url", "http://relay.test", "--agent-id", "dan"], capsys)
    assert rc == 0, out
    assert len(asked) > 5, "each question was asked"
    for question in ("What will this agent do?", "Where should its keys be kept?", "Who can wake your agent?",
                     "Which optional features should it have?"):
        assert question in out, question
    assert out.count("(recommended)") >= 6
    # Enter everywhere = the recommended answers, the same as the fast path
    env = (root / "dan" / ".env").read_text()
    assert "EMPORIA_WAKE=direct" in env and "EMPORIA_KEY_STORAGE=env" in env
    assert jobs == [("dan", "4h")]
    assert "(recommended default)" not in out, "the person answered them"
