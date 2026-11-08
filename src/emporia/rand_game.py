"""What the games that draw from `emporia:rand:v1` share (Liar's dice, the common-value auction;
Bargain v2 predates it). Pure: stdlib and `emporia.rand` only.

A seated player commits `sha256(s || nonce)` with its create or join, naming a drand round. The game's
round is the latest any commitment names, fixed once every seat has committed; a player's private draw
is `rand.priv(scope, seat, purpose, beacon_sig, s)`, which only it can compute until it reveals `s`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia import rand


def now() -> datetime:
    return datetime.now(UTC)


def game_round(commits: dict[str, Any], need: int) -> int | None:
    """The round the game uses: the latest any commitment names (None until `need` seats have committed)."""
    rounds = [int(c["beacon_round"]) for c in commits.values()]
    return max(rounds) if len(rounds) >= max(need, 2) else None


def reveal_problem(commit: dict[str, Any], payload: Any) -> str:
    """Why `payload` ({"s", "nonce"}) is not what `commit` committed to, or ""."""
    if not isinstance(payload, dict) or set(payload) != {"s", "nonce"} \
            or not rand.is_hex(payload["s"], 32) or not rand.is_hex(payload["nonce"], 32):
        return 'a reveal is {"s": <64 hex>, "nonce": <64 hex>}, the secret and nonce you committed'
    if rand.commit_hash(bytes.fromhex(payload["s"]), bytes.fromhex(payload["nonce"])) != commit["hash"]:
        return "that secret and nonce are not what your commitment hashes"
    return ""


def void_problem(data: dict[str, Any], payload: Any, over: bool) -> str:
    """Why a `void` is refused now, or "": it is for a commitment missing before the round is published
    (after it a draw can be seen, and a void would be a reroll)."""
    if over:
        return "The game is over: reveal your secret, or let the reveal window pass"
    if (payload or {}).get("reason") != "late_commit":
        return 'void takes {"reason": "late_commit"}: a commitment you did not see in time'
    rnd = (data.get("rand") or {}).get("round")
    if not rnd:
        return "Nothing to void: not every player's commitment is in yet"
    if now().timestamp() >= rand.round_time(rnd):
        return (f"Too late to void: drand round {rnd} is published, so a draw can be seen. "
                "A void is for a commitment missing before the round, and is checked then")
    return ""


def not_dealt_problem(data: dict[str, Any]) -> str:
    r = data.get("rand") or {}
    if r.get("round") and now().timestamp() < rand.round_time(r["round"]):
        when = datetime.fromtimestamp(rand.round_time(r["round"]), UTC).isoformat()
        return f"Nothing is dealt yet: drand round {r['round']} is published at {when}"
    return ""


def start_time(rnd: int | None) -> datetime:
    t = now()
    return max(t, datetime.fromtimestamp(rand.round_time(rnd), UTC)) if rnd is not None else t


def reveal_deadline(rnd: int | None, window_s: int) -> str:
    floor = max(now().timestamp(), rand.round_time(rnd) if rnd else 0)
    return datetime.fromtimestamp(floor + window_s, UTC).isoformat()


def sig_bytes(data: dict[str, Any]) -> bytes:
    sig = (data.get("rand") or {}).get("beacon_sig")
    return bytes.fromhex(sig) if sig else b""


def secrets_of(data: dict[str, Any], participants: list[str]) -> dict[str, tuple[int, bytes]]:
    """(seat, secret) of every player who revealed."""
    revs = (data.get("rand") or {}).get("reveals") or {}
    return {p: (seat, bytes.fromhex(revs[p]["s"])) for seat, p in enumerate(participants) if p in revs}


def int_config_problem(config: dict[str, Any], defaults: dict[str, int], bounds: dict[str, tuple[int, int]]) -> str:
    for key, (lo, hi) in bounds.items():
        v = config.get(key, defaults[key])
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return f"{key} must be a whole number, {lo}–{hi}"
    return ""
