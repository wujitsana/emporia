"""Arrow-key menus for the `emporia` command (P212–P215).

Every question the CLI asks goes through `cli.choose`, `cli.choose_many` and
`cli.ask`. In a real terminal they show a menu **inline**, right below the
last line — the screen is not cleared, so earlier answers stay in view — with
the keys Hermes's own menus use:

    ↑ ↓      move (round the ends)      Enter   choose
    Esc      back                       1-9     jump to that line
    /        filter (long lists)        Space   tick (where several may be chosen)
    y / n    answer a yes-or-no question

Colours are the terminal's own: its foreground and background, its theme's
cyan for the question, inverse for the highlighted line; NO_COLOR drops the
cyan. A line too wide ends in "…" and the highlighted one is shown whole
beneath. When it is answered the menu is erased and the CLI prints the answer
as one line.

Anywhere else — no terminal, `TERM=dumb`, no termios (Windows), a test, or
`EMPORIA_PLAIN_MENUS=1` — the CLI asks by number instead.
"""

from __future__ import annotations

import os
import select as _io
import shutil
import sys
import textwrap

BACK = object()          # Esc: the caller decides what "back" means there
_LONG = 8                # lists longer than this can be filtered with /

# Keys, as read: arrows arrive as ESC [ A or ESC O A, however the terminal sends them.
_UP, _DOWN, _LEFT, _ENTER, _ESC, _SPACE, _DEL = "up", "down", "left", "enter", "esc", " ", "del"


def available() -> bool:
    if os.getenv("EMPORIA_PLAIN_MENUS", "").strip() == "1" or os.getenv("TERM", "") in ("", "dumb"):
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ImportError, ValueError):
        return False


def select(title: str, labels: list[str], default: int = 0):
    """The index chosen, or BACK."""
    return _menu(title, labels, default, many=False)


def checklist(title: str, labels: list[str], ticked: set[int]):
    """The indices ticked, or BACK (nothing changed)."""
    return _menu(title, labels, 0, many=True, ticked=ticked)


def confirm(question: str, default: bool):
    """True, False, or BACK."""
    picked = _menu(question, ["Yes", "No"], 0 if default else 1, many=False, yes_no=True)
    return picked if picked is BACK else picked == 0


# ─── drawing ──────────────────────────────────────────────────────────────────

def _style(text: str, *codes: str) -> str:
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if codes else text


def _render(title, hint, labels, shown, cursor, marks) -> list[str]:
    cols, rows = shutil.get_terminal_size((80, 24))
    width = max(20, cols - 1)
    accent = () if os.getenv("NO_COLOR") else ("36",)
    out = [_style(line, "1", *accent) for line in textwrap.wrap(title, width)[:3]] or [""]
    out.append(_style(hint, "2"))
    room = max(3, min(12, rows - len(out) - 4))
    top = max(0, min(cursor - room + 1, len(shown) - room)) if cursor >= room else 0
    for n in range(top, min(len(shown), top + room)):
        i = shown[n]
        text = f"{'›' if n == cursor else ' '} {marks[i] if marks else ''}{labels[i]}"
        text = text if len(text) <= width else text[:width - 1] + "…"
        out.append(_style(text, "7", "1") if n == cursor else text)
    if len(shown) > room:
        out.append(_style(f"  {len(shown) - room} more — ↑↓ to scroll", "2"))
    if not shown:
        out.append(_style("  nothing matches", "2"))
    elif len(f"› {labels[shown[cursor]]}") > width:
        out += [_style("  " + line, "2") for line in textwrap.wrap(labels[shown[cursor]], width - 2)]
    return out


class _Inline:
    """Lines drawn below the cursor, redrawn in place, erased when done."""

    def __init__(self):
        self.drawn = 0

    def draw(self, lines: list[str]) -> None:
        up = f"\x1b[{self.drawn}F" if self.drawn else "\r"
        sys.stdout.write(up + "\x1b[J" + "\r\n".join(lines))
        sys.stdout.flush()
        self.drawn = len(lines) - 1

    def erase(self) -> None:
        sys.stdout.write((f"\x1b[{self.drawn}F" if self.drawn else "\r") + "\x1b[J")
        sys.stdout.flush()


# ─── keys ─────────────────────────────────────────────────────────────────────

def _read_key(fd: int) -> str:
    ch = os.read(fd, 1)
    if ch == b"\x1b":
        if not _io.select([fd], [], [], 0.03)[0]:
            return _ESC                                   # Esc alone: answered at once
        seq = os.read(fd, 1)
        if seq in (b"[", b"O"):
            code = os.read(fd, 1)
            return {b"A": _UP, b"B": _DOWN, b"D": _LEFT}.get(code, "")
        return _ESC
    if ch in (b"\r", b"\n"):
        return _ENTER
    if ch in (b"\x7f", b"\x08"):
        return _DEL
    if ch == b"\x03":
        raise KeyboardInterrupt
    return ch.decode(errors="ignore")


# ─── the menu ─────────────────────────────────────────────────────────────────

def _menu(title, labels, default, *, many, ticked=frozenset(), yes_no=False):
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    screen = _Inline()
    cursor, chosen, query = min(default, len(labels) - 1), set(ticked), None
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")                      # hide the cursor while choosing
        while True:
            shown = [i for i, lab in enumerate(labels) if not query or query.lower() in lab.lower()]
            cursor = min(cursor, max(0, len(shown) - 1))
            hint = ("Space tick · Enter done · Esc back" if many else
                    "y/n · Enter choose · Esc back" if yes_no else "↑↓ move · Enter choose · Esc back")
            if query is not None:
                hint = f"filter: {query}▏ (Esc clears)"
            elif len(labels) > _LONG:
                hint += " · / filter"
            marks = {i: ("[x] " if i in chosen else "[ ] ") for i in range(len(labels))} if many else None
            screen.draw(_render(title, hint, labels, shown, cursor, marks))
            k = _read_key(fd)
            if query is not None:                          # typing a filter
                if k == _ESC:
                    query = None
                elif k == _DEL:
                    query = query[:-1]
                elif k == _ENTER and shown:
                    return shown[cursor]
                elif k in (_UP, _DOWN):
                    cursor = (cursor + (1 if k == _DOWN else -1)) % max(1, len(shown))
                elif len(k) == 1 and k.isprintable():
                    query += k
                continue
            if k == _ENTER:
                return sorted(chosen) if many else shown[cursor]
            if k in (_ESC, _LEFT):
                return BACK
            if k in (_UP, _DOWN):
                cursor = (cursor + (1 if k == _DOWN else -1)) % len(shown)
            elif yes_no and k.lower() in ("y", "n"):
                return 0 if k.lower() == "y" else 1
            elif many and k == _SPACE:
                chosen ^= {shown[cursor]}
            elif k == "/" and len(labels) > _LONG:
                query = ""
            elif k.isdigit() and 1 <= int(k) <= len(shown):
                cursor = int(k) - 1
    finally:
        screen.erase()
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
