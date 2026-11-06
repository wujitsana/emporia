"""The randomness service `emporia:rand:v1` (architecture-v2 section 5.1). Stdlib only.

The relay never generates randomness a party could gain from. Draws come from a public beacon
(drand's `quicknet` chain, see `emporia.beacon`) named by each player's commitment *before* the
round exists, mixed with the player's own secret for private draws:

    rand    = sha256(F("emporia:rand:v1", scope, purpose, beacon_sig [, sorted reveals]))
    priv_i  = sha256(F("emporia:rand-private:v1", scope, seat, purpose, beacon_sig, s_i))

`F` joins its parts with a 4-byte big-endian length before each, so no two part lists give the
same bytes. `beacon_sig` is the raw 48-byte signature of the round. `s_i` is a 32-byte secret the
player committed to as sha256(s_i || nonce_i) before the round. Only player i can compute
`priv_i`; everyone can check it once `s_i` is revealed.

With no beacon (unreachable), a public draw falls back to the players' commit-reveal
contributions alone (`beacon_sig=b""` and `reveals`). A *private* draw never falls back: without
the beacon it would be chosen by the player who holds `s_i`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

LABEL_PUBLIC = "emporia:rand:v1"
LABEL_PRIVATE = "emporia:rand-private:v1"

#: Pinned from the live chain (api.drand.sh/<hash>/info, read 2026-11-06). Verified in
#: `emporia.beacon` against `PUBLIC_KEY`; the genesis and period are what `round_time` uses.
CHAIN_HASH = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
SCHEME = "bls-unchained-g1-rfc9380"
PERIOD_S = 3
GENESIS = 1692803367
PUBLIC_KEY = ("83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c8c4b450b6a0a6c3ac6a577"
              "6a2d1064510d1fec758c921cc22b0e17e63aaf4bcb5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09"
              "e76eae8991ef5ece45a")

#: A commitment names a round at least this many rounds (30 s) after its own signing time.
MIN_LEAD_ROUNDS = 10
#: What the agent clients choose: a margin for the other side's client to see the commitment.
CLIENT_LEAD_ROUNDS = 30


def _f(*parts: bytes | str | int) -> bytes:
    out = b""
    for p in parts:
        b = p if isinstance(p, bytes) else str(p).encode()
        out += len(b).to_bytes(4, "big") + b
    return out


def uint(digest: bytes) -> int:
    return int.from_bytes(digest, "big")


def rand(scope: str, purpose: str, beacon_sig: bytes = b"", reveals: list[str] | tuple[str, ...] = ()) -> bytes:
    """A public draw: from the beacon, or (no beacon) from the players' revealed secrets alone."""
    return hashlib.sha256(_f(LABEL_PUBLIC, scope, purpose, beacon_sig, *sorted(reveals))).digest()


def priv(scope: str, seat: int, purpose: str, beacon_sig: bytes, s: bytes) -> bytes:
    """Player `seat`'s private draw. `beacon_sig` is required: see the module doc."""
    if not beacon_sig:
        raise ValueError("a private draw needs the beacon")
    return hashlib.sha256(_f(LABEL_PRIVATE, scope, seat, purpose, beacon_sig, s)).digest()


def commit_hash(s: bytes, nonce: bytes) -> str:
    """sha256(s || nonce), hex: what a player signs before the round."""
    return hashlib.sha256(s + nonce).hexdigest()


def new_secret() -> tuple[str, str]:
    """(s, nonce) as hex, 32 random bytes each."""
    return secrets.token_hex(32), secrets.token_hex(32)


def round_time(round_no: int) -> float:
    """Unix time at which `round_no` is published."""
    return GENESIS + (round_no - 1) * PERIOD_S


def round_at(ts: float) -> int:
    """The round current at unix time `ts` (the last one published)."""
    return int((ts - GENESIS) // PERIOD_S) + 1


def to_ts(when: str | datetime) -> float:
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def commit_round_problem(signed_at: str, beacon_round: object) -> str:
    """Why a commitment signed at `signed_at` may not name `beacon_round`, or ""."""
    if isinstance(beacon_round, bool) or not isinstance(beacon_round, int) or beacon_round < 1:
        return "beacon_round must be a whole number of a drand round"
    need = round_at(to_ts(signed_at)) + MIN_LEAD_ROUNDS
    if beacon_round < need:
        return (f"beacon_round {beacon_round} is too early: it must be at least {MIN_LEAD_ROUNDS} rounds "
                f"({MIN_LEAD_ROUNDS * PERIOD_S} s) after the commitment is signed (round {need} or later)")
    return ""


def is_hex(value: object, nbytes: int) -> bool:
    if not isinstance(value, str) or len(value) != 2 * nbytes:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return value == value.lower()
