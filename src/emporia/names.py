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


# Cyrillic and Greek letters that draw as a Latin one, folded to it (a small table, not a dependency).
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ѡ": "w",
    "ӏ": "l", "ь": "b", "ο": "o", "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "γ": "y", "η": "n", "ω": "w", "ϲ": "c", "ѵ": "v", "ɡ": "g", "ɑ": "a", "ⅼ": "l",
})


def skeleton(name: str) -> str:
    """The spelling two look-alike handles share: NFKC, lowercase, Cyrillic/Greek look-alikes folded to Latin,
    separators gone, `0 1 i 5 3` folded to `o l l s e`, `rn` to `m`, `vv` to `w`, `cl` to `d`."""
    folded = unicodedata.normalize("NFKC", str(name)).lower().translate(_CONFUSABLES)
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


# Characters that draw nothing though they are not control characters: Hangul fillers, the braille blank,
# combining grapheme joiner, variation selectors, Mongolian free variation selectors, Khmer inherent vowels.
_BLANK = {chr(c) for c in (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x034F, 0x17B4, 0x17B5, 0x180B, 0x180C,
                           0x180D, 0x180E, *range(0xFE00, 0xFE10), 0x2063, 0x2062, 0x2061, 0x2064)}
_MAX_MARKS = 2


def _visible(text: str) -> str:
    """What is left of `text` once whitespace, combining marks and blank-drawing characters are gone."""
    return "".join(c for c in text if c not in _BLANK and not c.isspace()
                   and unicodedata.category(c) not in ("Mn", "Me", "Zs", "Zl", "Zp", "Cf", "Cc"))


def display_name_problem(value: str, handles: Iterable[str] = (), reserved: Iterable[str] = (),
                         own: str = "") -> str:
    """Why this display name is refused, or "": too long, control, line-breaking, unassigned or direction-changing
    characters, nothing visible, a stack of combining marks, a link, or (when `handles` / `reserved` are given)
    a spelling that reads as another handle or a reserved word. `own` is the agent's own handle, which it may
    use as its display name."""
    text = str(value or "")
    if len(text) > DISPLAY_NAME_MAX:
        return f"a display name is at most {DISPLAY_NAME_MAX} characters"
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") or c in _BIDI for c in text):
        return "a display name has no control, line-breaking or direction-changing characters"
    if text and not _visible(text):
        return "a display name must have something visible in it"
    run = 0
    for c in text:
        run = run + 1 if unicodedata.category(c) in ("Mn", "Me", "Mc") else 0
        if run > _MAX_MARKS:
            return f"a display name has at most {_MAX_MARKS} combining marks in a row"
    if _URL.search(text):
        return "a display name is a name, not a link"
    if text:
        return _display_name_impersonation(text, handles, reserved, own)
    return ""


# Reserved words that are ordinary words in a longer name ("Alpha Bot", "Team Rocket"): refused only when the
# whole display name is one, not when one word of it is.
_ORDINARY_WORDS = frozenset({"official", "api", "www", "null", "none", "undefined", "me", "self", "everyone",
                             "anonymous", "house", "bot", "mod", "team", "help", "owner", "founder", "bank",
                             "wallet", "escrow", "treasury", "signer"})


def _display_name_impersonation(text: str, handles: Iterable[str], reserved: Iterable[str], own: str) -> str:
    """The whole display name (leading `@` and end punctuation dropped), and each word of it, against every
    handle and reserved word by skeleton, so `wujitsаna`, `@wujitsana`, `Wujitsana!` and `wujitsana official`
    all read as the handle they wear. The agent's own handle is exempt."""
    whole = re.sub(r"^[\W_]+|[\W_]+$", "", text)
    words = [w for w in re.split(r"[\W_]+", text) if w]
    handles, reserved = [h for h in handles if h], [r for r in reserved if r]
    for name in (*handles, *reserved):
        if name == own:
            continue
        target = skeleton(name)
        parts = [text, whole]
        # a handle as one word of a short name ("wujitsana official") reads as that agent; in a longer
        # sentence ("Alice the Bold") it is just a word. An authority word counts wherever it stands.
        if (name in handles and len(words) <= 2) or (name in reserved and name not in _ORDINARY_WORDS):
            parts += words
        if any(p and skeleton(p) == target for p in parts):
            return f"a display name may not read as the handle or reserved name '{name}'"
    return ""


def clean_display_name(value: str, handles: Iterable[str] = (), reserved: Iterable[str] = (),
                       own: str = "") -> str:
    """The display name as stored: NFKC, one line, trimmed. Raises ValueError (a message safe to show) when it
    breaks `display_name_problem`."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if why := display_name_problem(text, handles, reserved, own):
        raise ValueError(why)
    return text
