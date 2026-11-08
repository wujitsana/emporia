"""A small arrow-key menu for the plugin's own setup question (stdlib only, imports nothing from Emporia).

`hermes emporia setup` asks one question before Emporia's runtime exists, so it cannot use the
runtime's menus. This is the same idea, kept small: ↑/↓ move, Enter chooses, number keys jump,
Esc or q cancels. Each option is a line (its name, with "(recommended)" highlighted) and an
indented dim line saying what it does. The cursor starts on the recommended option.

The key handling is a pure function (`step`), tested with fake key streams. The terminal is put
in cbreak mode only while the menu is open and always restored (Ctrl-C and errors included).
Without a terminal, with TERM=dumb, NO_COLOR is honoured for colors, or when raw mode fails, the
caller falls back to a numbered prompt (`numbered`). Colors only on a terminal, never with NO_COLOR.
"""

from __future__ import annotations

import os
import select as _select
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

UP, DOWN, ENTER, ESC = "up", "down", "enter", "esc"


@dataclass(frozen=True)
class Item:
    value: str
    explanation: str
    recommended: bool = False


def color_on() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def paint(text: str, *codes: str) -> str:
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes and color_on() else text


def lines(question: str, items: Sequence[Item], cursor: int | None = None, numbers: bool = True) -> list[str]:
    """The question, then each option and its indented dim explanation (plain text without a terminal)."""
    out = [paint(question, "1")]
    for i, it in enumerate(items):
        name = paint(it.value, "1") + ("  " + paint("(recommended)", "32", "1") if it.recommended else "")
        marker = ("›" if i == cursor else " ") if cursor is not None else " "
        out.append(f"  {marker} {f'{i + 1}) ' if numbers else ''}{name}")
        out.append(paint(f"        {it.explanation}", "2"))
    return out


def start_index(items: Sequence[Item]) -> int:
    return next((i for i, it in enumerate(items) if it.recommended), 0)


def step(cursor: int, count: int, key: str) -> tuple[int, str | None]:
    """One key: (the new cursor, "chosen" | "cancelled" | None). Wraps at the ends; a digit jumps to
    that option; anything else changes nothing."""
    if key == UP:
        return (cursor - 1) % count, None
    if key == DOWN:
        return (cursor + 1) % count, None
    if key == ENTER:
        return cursor, "chosen"
    if key in (ESC, "q"):
        return cursor, "cancelled"
    if key.isdigit() and 1 <= int(key) <= count:
        return int(key) - 1, None
    return cursor, None


def run(items: Sequence[Item], keys, cursor: int | None = None) -> int | None:
    """Drive `step` from any iterable of keys: the index chosen, or None if cancelled or the keys end."""
    cursor = start_index(items) if cursor is None else cursor
    for key in keys:
        cursor, done = step(cursor, len(items), key)
        if done == "chosen":
            return cursor
        if done == "cancelled":
            return None
    return None


def available() -> bool:
    if os.getenv("TERM", "") in ("", "dumb") or os.getenv("EMPORIA_PLAIN_MENUS", "") == "1":
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ImportError, ValueError):
        return False


def _keys(fd: int):
    while True:
        ch = os.read(fd, 1)
        if not ch:
            return
        if ch == b"\x1b":
            if not _select.select([fd], [], [], 0.03)[0]:
                yield ESC
                continue
            seq = os.read(fd, 1)
            if seq in (b"[", b"O"):
                yield {b"A": UP, b"B": DOWN}.get(os.read(fd, 1), "")
            else:
                yield ESC
        elif ch in (b"\r", b"\n"):
            yield ENTER
        elif ch == b"\x03":
            raise KeyboardInterrupt
        else:
            yield ch.decode(errors="ignore")


def select(question: str, items: Sequence[Item], out=None) -> int | None | bool:
    """The index chosen, None if cancelled, or False when no raw terminal is available (use `numbered`)."""
    if not available():
        return False
    out = out or sys.stdout
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        return False
    cursor = start_index(items)
    try:
        try:
            tty.setcbreak(fd)
        except Exception:
            return False
        out.write("\x1b[?25l")
        for key in _redraw_keys(fd, question, items, out, lambda: cursor, lambda c: None):
            cursor, done = step(cursor, len(items), key)
            if done == "chosen":
                return cursor
            if done == "cancelled":
                return None
        return None
    finally:
        out.write("\x1b[?25h")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _redraw_keys(fd, question, items, out, get_cursor: Callable[[], int], _set):
    drawn = 0
    gen = _keys(fd)
    while True:
        text = "\r\n".join(lines(question, items, get_cursor()))
        out.write((f"\x1b[{drawn}F" if drawn else "\r") + "\x1b[J" + text)
        out.flush()
        drawn = 2 * len(items)
        try:
            yield next(gen)
        except StopIteration:
            return


def numbered(question: str, items: Sequence[Item], ask: Callable[[str], str] = input) -> int:
    """The fallback: the same layout, answered by number; Enter takes the recommended option."""
    print("\n".join(lines(question, items)))
    default = start_index(items)
    while True:
        raw = (ask(f"  Choose 1-{len(items)} [{default + 1}]: ") or "").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return int(raw) - 1
