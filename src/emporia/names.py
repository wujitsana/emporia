"""Names that can be told apart: look-alike checks for agent handles and the rules for display names.

A handle is ASCII (`relay_server._normalize_agent_id`), so a cross-script homoglyph never gets in. What is
left is the ASCII look-alike: `rn` for `m`, `1` or `i` for `l`, `0` for `o`, and `-` against `_`. `skeleton`
folds those to one spelling, and `lookalike_problem` refuses a new handle whose skeleton is an existing or
reserved one. A display name is free text a person reads, so it is cleaned: one line, a length cap, no control or
bidirectional characters, no link. Pure: no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

DISPLAY_NAME_MAX = 64
# The bidirectional controls (U+061C, U+200E, U+200F, U+202A-202E, U+2066-2069), by code point: the
# characters themselves are invisible in a review.
_BIDI = {chr(c) for c in (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))}
_URL = re.compile(r"(?i)(https?:|ftp:|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|ai|dev|app|xyz|co|me|gg)\b)")
_FOLD = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "5": "s", "$": "s", "3": "e", "@": "a"})


def skeleton(name: str) -> str:
    """The spelling two look-alike handles share: NFKC, lowercase, separators gone, `0 1 i 5 3` folded to
    `o l l s e`, `rn` to `m`, `vv` to `w`, `cl` to `d`."""
    folded = unicodedata.normalize("NFKC", str(name)).lower()
    folded = re.sub(r"[-_.\s]+", "", folded).translate(_FOLD)
    for pair, one in (("rn", "m"), ("vv", "w"), ("cl", "d")):
        folded = folded.replace(pair, one)
    return folded


def lookalike_problem(agent_id: str, existing: Iterable[str], reserved: Iterable[str]) -> str:
    """Why a *new* handle must be refused as too close to another, or "": its skeleton equals a reserved
    name's, or an existing (other) handle's. `existing` may include the handle itself; it is skipped."""
    mine = skeleton(agent_id)
    for name in reserved:
        if name != agent_id and skeleton(name) == mine:
            return f"'{agent_id}' is too close to the reserved name '{name}'"
    for name in existing:
        if name != agent_id and skeleton(name) == mine:
            return (f"'{agent_id}' is too close to the existing handle '{name}' "
                    "(look-alike); choose a name that reads differently")
    return ""


def display_name_problem(value: str) -> str:
    """Why this display name is refused, or "": too long, control or bidirectional characters, a link."""
    text = str(value or "")
    if len(text) > DISPLAY_NAME_MAX:
        return f"a display name is at most {DISPLAY_NAME_MAX} characters"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co") or c in _BIDI for c in text):
        return "a display name has no control or direction-changing characters"
    if _URL.search(text):
        return "a display name is a name, not a link"
    return ""


def clean_display_name(value: str) -> str:
    """The display name as stored: NFKC, one line, trimmed. Raises ValueError (a message safe to show) when it
    breaks `display_name_problem`."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if why := display_name_problem(text):
        raise ValueError(why)
    return text
