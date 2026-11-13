"""Cutting a line short for a title or a preview: at a word boundary, never in the middle of a word."""
from __future__ import annotations

from typing import Any

ELLIPSIS = "…"


def cut_at_word(text: Any, limit: int) -> str:
    """`text` as is when it fits in `limit` characters; otherwise cut at the last space at or before `limit`
    and closed with "…". A single word longer than `limit` (no space to cut at) is cut at `limit`."""
    s = str(text or "")
    if limit < 1 or len(s) <= limit:
        return s
    space = max(s.rfind(" ", 0, limit + 1), s.rfind("\t", 0, limit + 1))
    head = s[:space].rstrip() if space > 0 else ""
    return (head or s[:limit]) + ELLIPSIS
