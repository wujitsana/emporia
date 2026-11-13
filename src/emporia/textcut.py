"""Cutting a line short for a title or a preview: at a word boundary, never in the middle of a word."""
from __future__ import annotations

from typing import Any

ELLIPSIS = "…"


def cut_at_word(text: Any, limit: int) -> str:
    """`text` as is when it fits in `limit` characters; otherwise cut at the last space that leaves room for the
    "…" and closed with it: the result is at most `limit` characters. A single word longer than that (no space
    to cut at) is cut to fit."""
    s = str(text or "")
    if limit < 1 or len(s) <= limit:
        return s
    room = limit - len(ELLIPSIS)
    space = max(s.rfind(" ", 0, room + 1), s.rfind("\t", 0, room + 1))
    head = s[:space].rstrip() if space > 0 else ""
    return (head or s[:room]) + ELLIPSIS
