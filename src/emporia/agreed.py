"""What a game was agreed on: the settings its creator submitted and the ones the relay accepted.

A finished game keeps what was played, and `agreed` keeps what was asked for, so a reader can tell the two
apart. The creator's signature covers the game id, module, time and money terms, not the settings; the
hashes here are the relay's own record of them, over canonical JSON (sorted keys, no spaces).

    {"format": "emporia:agreed:v1",
     "submitted": {...}, "submitted_sha256": ...,   # config exactly as the creator sent it
     "accepted": {...}, "accepted_sha256": ...,     # config as stored (the relay's additions included)
     "signed": {"at", "terms", "signature", "covers"},
     "governs": "..."}                              # only when a note disagrees with a structured field
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

FORMAT = "emporia:agreed:v1"
COVERS = "game id, module, time and money terms; not the settings (their hashes are the relay's record)"

_NO_CALL = re.compile(r"\b(no|without|skip|never)\s+(a\s+)?calls?\b", re.I)
_CALL = re.compile(r"\bcalls?\b", re.I)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def note_claims_calls(note: Any) -> bool | None:
    """True when a note says the game has calls, False when it says it has none, None when it says nothing."""
    if not isinstance(note, str):
        return None
    if _NO_CALL.search(note):
        return False
    return True if _CALL.search(note) else None


def governs(accepted: Mapping[str, Any]) -> str | None:
    """One line, only where the note and `with_call` disagree: the structured field governs."""
    ch = accepted.get("challenge")
    if not isinstance(ch, Mapping):
        return None
    claim = note_claims_calls(ch.get("note"))
    asked = bool(ch.get("with_call"))
    if claim is None or claim == asked:
        return None
    return (f"challenge.with_call is {str(asked).lower()} and governs; the note says "
            f"{'calls' if claim else 'no calls'}, which is free text")


def record(submitted: Mapping[str, Any], accepted: Mapping[str, Any], *, at: str,
           terms: Mapping[str, Any], signature: str) -> dict[str, Any]:
    sub = copy.deepcopy(dict(submitted))
    acc = copy.deepcopy(dict(accepted))
    out: dict[str, Any] = {
        "format": FORMAT,
        "submitted": sub, "submitted_sha256": sha256(sub),
        "accepted": acc, "accepted_sha256": sha256(acc),
        "signed": {"at": at, "terms": dict(terms), "signature": signature, "covers": COVERS},
    }
    if line := governs(acc):
        out["governs"] = line
    return out
