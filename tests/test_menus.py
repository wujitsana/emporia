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
