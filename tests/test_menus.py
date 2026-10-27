"""P212: the arrow-key menus, driven through a real pseudo-terminal."""
import os
import select
import sys
import time

import pytest

pty = pytest.importorskip("pty")
pytest.importorskip("curses")

SCRIPT = r'''
from emporia import cli
cli._TTY = True
opts = [(f"o{i}", f"Option {i}") for i in range(1, 11)] + [("done", "Done")]
print("PICK=" + cli.choose("Which one?", opts, 0))
print("BACK=" + cli.choose("Again?", opts, 0))
print("YES=" + str(cli.ask("Sure?", False)))
print("MANY=" + ",".join(cli.choose_many("Which ones?", opts[:3], {"o1"})))
print("FILTER=" + cli.choose("Find it", opts, 0))
'''


def _drive(down: str) -> dict:
    steps = [down + down + "\r", "\x1b", "y", down + " \r", "/10\r"]
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm", EMPORIA_NO_DOTENV="1")
        os.environ.pop("EMPORIA_PLAIN_MENUS", None)
        os.execv(sys.executable, [sys.executable, "-c", SCRIPT])
    out = b""

    def drain(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return
    for keys in steps:
        drain(0.6)
        os.write(fd, keys.encode())
    drain(1.5)
    os.waitpid(pid, 0)
    lines = out.decode(errors="replace").replace("\r", "\n").split("\n")
    return dict(ln.strip().split("=", 1) for ln in lines if ln.strip().split("=", 1)[0] in
                ("PICK", "BACK", "YES", "MANY", "FILTER"))


@pytest.mark.parametrize("down", ["\x1bOB", "\x1b[B"], ids=["keypad-mode", "plain"])
def test_arrows_enter_esc_yes_space_and_filter(down):
    """Both ways terminals send ↓ move; Esc picks the menu's own Done; y answers;
    Space ticks; / filters. (A terminal sending ↓ the plain way once read as Esc.)"""
    assert _drive(down) == {"PICK": "o3", "BACK": "done", "YES": "True", "MANY": "o1,o2", "FILTER": "o10"}


def test_esc_without_a_back_option_cancels_the_command(monkeypatch, capsys):
    from emporia import cli, menus
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(menus, "available", lambda: True)
    monkeypatch.setattr(menus, "select", lambda *a, **k: menus.BACK)
    with pytest.raises(cli.Cancelled):
        cli.choose("Pick", [("a", "A"), ("b", "B")])
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: (_ for _ in ()).throw(cli.Cancelled()))
    assert cli.main(["doctor"]) == 130 and "Cancelled." in capsys.readouterr().out


WIZARD = r'''
import argparse
from emporia import cli
cli._TTY = True
seen = []

@cli.wizard
def setup(args):
    print("HEADER-LINE")
    colour = cli.choose("Colour?", [("red", "Red — warm"), ("green", "Green"), ("blue", "Blue")])
    sure = cli.ask("Sure?", True)
    seen.append((colour, sure))
    return 0

setup(argparse.Namespace())
print("SEEN=" + repr(seen))
'''


def test_esc_in_a_wizard_goes_back_a_question_with_the_answer_kept():
    """P213: Esc on "Sure?" returns to "Colour?" with Green preselected; ↓ makes it Blue."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm", EMPORIA_NO_DOTENV="1")
        os.environ.pop("EMPORIA_PLAIN_MENUS", None)
        os.execv(sys.executable, [sys.executable, "-c", WIZARD])
    out = b""
    for keys in ["\x1bOB\r", "\x1b", "\x1bOB\r", "y"]:      # Green · Esc · (Green)↓Blue · yes
        end = time.time() + 0.7
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    break
        os.write(fd, keys.encode())
    end = time.time() + 1.5
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            try:
                out += os.read(fd, 65536)
            except OSError:
                break
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert "SEEN=[('blue', True)]" in text, text[-400:]
    assert "← back" in text
    assert text.count("HEADER-LINE") == 1                    # P216: not printed again on going back


def test_numbered_wizard_back_replay_and_commit(monkeypatch, capsys):
    """Without arrows: b goes back; earlier answers replay; after a commit Esc/b cancels."""
    import argparse

    from emporia import cli
    from tests.test_wallet_setup import _typing
    calls = []

    @cli.wizard
    def setup(args):
        a = cli.choose("First?", [("x", "X"), ("y", "Y")])
        b = cli.prompt("Name")
        calls.append((a, b))
        if b == "commit":
            cli.wizard_commit()
            cli.choose("After?", [("p", "P"), ("q", "Q")])
        return 0

    monkeypatch.setattr(cli, "_menus", lambda: None)
    # Y · Esc+Enter at the name (back) · Enter keeps Y · bob
    _typing(monkeypatch, "2", "\x1b", "", "bob")
    assert setup(argparse.Namespace()) == 0 and calls[-1] == ("y", "bob")   # Enter kept Y: preselected
    assert "← back" in capsys.readouterr().out
    _typing(monkeypatch, "b")                              # back on the first question: cancelled
    with pytest.raises(cli.Cancelled):
        setup(argparse.Namespace())
    _typing(monkeypatch, "1", "commit", "b", "1")          # after a commit, b just asks again
    assert setup(argparse.Namespace()) == 0 and calls[-1] == ("x", "commit")


def test_the_menu_is_inline_and_leaves_the_screen_as_it_was():
    """P215: no alternate screen, no clear — what was printed before stays; the
    answer replaces the menu as one line."""
    script = r'''
from emporia import cli
cli._TTY = True
print("EARLIER OUTPUT")
cli.choose("Pick one", [("a", "Alpha"), ("b", "Beta")])
'''
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm", EMPORIA_NO_DOTENV="1")
        os.environ.pop("EMPORIA_PLAIN_MENUS", None)
        os.execv(sys.executable, [sys.executable, "-c", script])
    out = b""
    for keys in ["\x1b[B\r"]:
        end = time.time() + 0.7
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                out += os.read(fd, 65536)
        os.write(fd, keys.encode())
    end = time.time() + 1.0
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            try:
                out += os.read(fd, 65536)
            except OSError:
                break
    os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert "\x1b[?1049h" not in text and "\x1b[2J" not in text          # no full-screen takeover
    assert "EARLIER OUTPUT" in text and "Pick one → Beta" in text
