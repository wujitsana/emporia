"""Arrow-key menus for the `emporia` command (P212).

Every question the CLI asks goes through `cli.choose`, `cli.choose_many` and
`cli.ask`. In a real terminal they show a menu, with the keys Hermes's own
menus use:

    ↑ ↓      move (round the ends)      Enter   choose
    Esc      back                       1-9     jump to that line
    /        filter (long lists)        Space   tick (where several may be chosen)
    y / n    answer a yes-or-no question

Anywhere else — no terminal, `TERM=dumb`, no curses (Windows), a test, or
`EMPORIA_PLAIN_MENUS=1` — the CLI asks by number instead. A menu leaves the
screen as it found it; the CLI then prints the answer as a normal line, so the
scrollback still says what was chosen.
"""

from __future__ import annotations

import os
import sys

BACK = object()          # Esc: the caller decides what "back" means there
_LONG = 8                # lists longer than this can be filtered with /


def available() -> bool:
    if os.getenv("EMPORIA_PLAIN_MENUS", "").strip() == "1" or os.getenv("TERM", "") in ("", "dumb"):
        return False
    try:
        import curses  # noqa: F401
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


def _menu(title, labels, default, *, many, ticked=frozenset(), yes_no=False):
    import curses

    os.environ.setdefault("ESCDELAY", "25")          # Esc answers at once

    def run(scr):
        curses.curs_set(0)
        scr.keypad(True)
        cursor, chosen, query = min(default, len(labels) - 1), set(ticked), None
        while True:
            shown = [i for i, lab in enumerate(labels) if not query or query.lower() in lab.lower()]
            cursor = min(cursor, max(0, len(shown) - 1))
            hint = ("Space tick · Enter done · Esc back" if many else
                    "y/n · Enter choose · Esc back" if yes_no else "↑↓ move · Enter choose · Esc back")
            if query is not None:
                hint = f"filter: {query}▏ (Esc clears)"
            elif len(labels) > _LONG:
                hint += " · / filter"
            _draw(scr, curses, title, hint, [labels[i] for i in shown], cursor,
                  [("[x] " if i in chosen else "[ ] ") for i in shown] if many else None)
            k = _read_key(scr, curses)
            if query is not None:                           # typing a filter
                if k == 27:
                    query = None
                elif k in (curses.KEY_BACKSPACE, 127, 8):
                    query = query[:-1]
                elif k in (10, 13, curses.KEY_ENTER) and shown:
                    return shown[cursor]
                elif k in (curses.KEY_UP, curses.KEY_DOWN):
                    cursor = (cursor + (1 if k == curses.KEY_DOWN else -1)) % max(1, len(shown))
                elif 32 <= k < 127:
                    query += chr(k)
                continue
            ch = chr(k).lower() if 0 <= k < 256 else ""
            if k in (10, 13, curses.KEY_ENTER):
                return sorted(chosen) if many else shown[cursor]
            if k in (27, curses.KEY_LEFT):
                return BACK
            if k in (curses.KEY_UP, curses.KEY_DOWN):
                cursor = (cursor + (1 if k == curses.KEY_DOWN else -1)) % len(shown)
            elif yes_no and ch in ("y", "n"):
                return 0 if ch == "y" else 1
            elif many and k == ord(" "):
                chosen ^= {shown[cursor]}
            elif k == ord("/") and len(labels) > _LONG:
                query = ""
            elif ord("1") <= k <= ord("9") and k - ord("1") < len(shown):
                cursor = k - ord("1")

    return curses.wrapper(run)


def _read_key(scr, curses) -> int:
    """A key, with arrows decoded however the terminal sends them: curses knows
    the form its keypad mode asks for, but some terminals (and tmux setups)
    send the other — which read raw would look like Esc, and cancel."""
    k = scr.getch()
    if k != 27:
        return k
    scr.nodelay(True)
    try:
        nxt = scr.getch()
        if nxt in (ord("["), ord("O")):
            return {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN, ord("C"): curses.KEY_RIGHT,
                    ord("D"): curses.KEY_LEFT}.get(scr.getch(), -1)
        return 27
    finally:
        scr.nodelay(False)


def _draw(scr, curses, title, hint, rows, cursor, marks):
    scr.erase()
    height, width = scr.getmaxyx()

    def put(y, text, attr=0):
        if 0 <= y < height:
            try:
                scr.addnstr(y, 0, text, max(1, width - 1), attr)
            except curses.error:
                pass

    put(0, title, curses.A_BOLD)
    put(1, hint, curses.A_DIM)
    room = max(1, height - 3)
    top = max(0, min(cursor - room + 1, len(rows) - room)) if cursor >= room else 0
    for y, n in enumerate(range(top, min(len(rows), top + room)), start=3):
        line = f" {'›' if n == cursor else ' '} {marks[n] if marks else ''}{rows[n]}"
        put(y, line, curses.A_REVERSE if n == cursor else 0)
    if not rows:
        put(3, "   nothing matches", curses.A_DIM)
    scr.refresh()
