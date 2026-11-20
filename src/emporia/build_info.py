"""Which build a relay runs: the short commit, known without network or a subprocess.

In order: `EMPORIA_BUILD_COMMIT` (set at build or install time), a `BUILD_COMMIT` file next to this module, then the
checkout's git data read as plain files (read-only). Anything else gives None and the field is left out.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_HEX = re.compile(r"[0-9a-f]{7,40}")


def _short(text: str) -> str | None:
    text = text.strip().lower()
    return text[:8] if _HEX.fullmatch(text) else None


def _git_dir(start: Path) -> Path | None:
    for d in (start, *start.parents):
        g = d / ".git"
        if g.is_dir():
            return g
        if g.is_file():                         # a worktree: `gitdir: <path>`
            line = g.read_text().strip()
            if line.startswith("gitdir:"):
                p = Path(line[7:].strip())
                return p if p.is_absolute() else (d / p).resolve()
    return None


def _from_git(start: Path) -> str | None:
    gd = _git_dir(start)
    if gd is None:
        return None
    head = (gd / "HEAD").read_text().strip()
    if not head.startswith("ref:"):
        return _short(head)
    ref = head[4:].strip()
    common = gd
    if (gd / "commondir").is_file():
        common = (gd / (gd / "commondir").read_text().strip()).resolve()
    for base in (gd, common):
        if (base / ref).is_file():
            return _short((base / ref).read_text())
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            if line.endswith(" " + ref):
                return _short(line.split()[0])
    return None


def commit() -> str | None:
    """The running build's short commit, or None when it cannot be known."""
    here = Path(__file__).resolve().parent
    for get in (lambda: _short(os.environ.get("EMPORIA_BUILD_COMMIT", "")),
                lambda: _short((here / "BUILD_COMMIT").read_text()),
                lambda: _from_git(here)):
        try:
            if value := get():
                return value
        except Exception:  # noqa: BLE001 — a missing source is skipped; the field is then left out
            continue
    return None
