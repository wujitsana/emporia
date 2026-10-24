"""Commit-reveal for simultaneous moves (P132). Only imports: stdlib.

A player first sends `commitment(move, nonce)`, a hash, and later the move and
nonce themselves. The relay stores and relays only the hash until the reveal,
so it cannot see a move early or leak one; a reveal that does not match its
commitment is refused, so nobody can change a move after seeing the other's.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

_HEX64 = re.compile(r"[0-9a-f]{64}")
#: A nonce long enough that a commitment cannot be brute-forced over the small
#: space of legal moves.
MIN_NONCE_CHARS = 32


def canonical(move: Any) -> str:
    return json.dumps(move, sort_keys=True, separators=(",", ":"))


def commitment(move: Any, nonce: str) -> str:
    return hashlib.sha256(f"{canonical(move)}:{nonce}".encode()).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(32)


def is_commitment(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def matches(committed: str, move: Any, nonce: Any) -> bool:
    return (isinstance(nonce, str) and len(nonce) >= MIN_NONCE_CHARS
            and secrets.compare_digest(committed, commitment(move, nonce)))
