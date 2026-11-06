"""The agent's side of `emporia:rand:v1` and Bargain v2: keep the secret, name the round, compute
the hand. Nothing here is the relay's: it never receives a secret, a hand or a walk-away.

A player's tools call `make_commit` once (with the create or join), keep the returned record in a
0600 file next to the agent's keys, and call `private_view` whenever they read the game. The relay
only ever sees the hash.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from emporia import beacon, rand


def make_commit(sign: Callable[[str, int, str, int, str], str], session_id: str, agent_id: str, seat: int,
                now: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the commitment to send, the secret record to keep). `sign(session_id, seat, hash, round, at)`
    returns the signature over `wire.signed_rand_commit_payload`."""
    now = time.time() if now is None else now
    s, nonce = rand.new_secret()
    h = rand.commit_hash(bytes.fromhex(s), bytes.fromhex(nonce))
    rnd = rand.round_at(now) + rand.CLIENT_LEAD_ROUNDS
    at = datetime.fromtimestamp(now, UTC).isoformat()
    commit = {"hash": h, "beacon_round": rnd, "at": at, "signature": sign(session_id, seat, h, rnd, at)}
    return commit, {"s": s, "nonce": nonce, "seat": seat, "hash": h, "beacon_round": rnd}


def get_beacon(rnd: int, relay_base: str | None = None, get: Callable[[str], dict | None] = beacon.http_get) -> str | None:
    """The verified signature of a round: the relay's cache first, then drand itself. A wrong value is
    simply refused, wherever it came from."""
    if relay_base:
        try:
            d = get(f"{relay_base.rstrip('/')}/beacon/{rnd}")
            if isinstance(d, dict) and d.get("round") == rnd and beacon.verify(rnd, str(d.get("signature") or "")):
                return str(d["signature"])
        except Exception:  # noqa: BLE001
            pass
    return beacon.fetch(rnd, get)


def _commits(sess: dict[str, Any]) -> dict[str, Any]:
    return (((sess.get("state") or {}).get("data") or {}).get("rand") or {}).get("commits") \
        or ((sess.get("config") or {}).get("rand") or {}).get("commits") or {}


def note_other_seen(rec: dict[str, Any], sess: dict[str, Any], agent_id: str, now: float | None = None) -> None:
    """Remember when this client first saw every other commitment (the late-commit rule)."""
    commits = _commits(sess)
    if len(commits) >= 2 and agent_id in commits and "seen_all_at" not in rec:
        rec["seen_all_at"] = time.time() if now is None else now


#: A client checks that it has seen every commitment this long before the round is published, and
#: voids then if not: the relay refuses a void once the round is out (a hand could be seen).
VOID_MARGIN_S = 6


def late_commit(rec: dict[str, Any], rnd: int) -> bool:
    """True when this client first saw every commitment only after the check time (round time minus a margin)."""
    return rec.get("seen_all_at") is not None and rec["seen_all_at"] >= rand.round_time(rnd) - VOID_MARGIN_S


def private_view(sess: dict[str, Any], agent_id: str, rec: dict[str, Any] | None,
                 relay_base: str | None = None, now: float | None = None,
                 fetch: Callable[..., str | None] = get_beacon) -> dict[str, Any]:
    """What `agent_id` may read of its own Bargain v2 hand, computed here from its secret and the
    beacon. `rec` (the secret record) is updated in place (first-seen time, the verified beacon)."""
    from emporia.modules import bargain as BG
    now = time.time() if now is None else now
    if rec is None:
        return {"error": "no secret for this game on this machine: it was made by another install, "
                         "or this agent never committed"}
    commits = _commits(sess)
    if agent_id not in commits or len(commits) < 2:
        return {"waiting": "for the other player's commitment"}
    note_other_seen(rec, sess, agent_id, now)
    data = (sess.get("state") or {}).get("data") or {}
    rnd = BG.game_round(commits)
    ready_at = rand.round_time(rnd)
    base = {"round": rnd, "ready_at": datetime.fromtimestamp(ready_at, UTC).isoformat()}
    if late_commit(rec, rnd) and now < ready_at:
        return {**base, "void": "late_commit",
                "do": "submit_action(session_id, 'void', {'reason': 'late_commit'}) NOW, before the round is "
                      "published: you saw the other commitment too late to trust the draw. After the round the "
                      "relay refuses a void"}
    if now < ready_at:
        return {**base, "wait_s": round(ready_at - now, 1),
                "note": "the hands are dealt when this drand round is published; offers wait for it"}
    sig = rec.get("beacon_sig") or fetch(rnd, relay_base)
    if not sig:
        return {**base, "error": "the beacon round could not be read from the relay or drand; try again"}
    rec["beacon_sig"] = sig
    hand, walk = BG.draw(r_scope(sess), int(rec["seat"]), bytes.fromhex(sig), bytes.fromhex(rec["s"]))
    pool = BG._as_vec(data.get("pool") or BG.POOL)
    return {**base, "values": BG._as_dict(hand), "total": BG.TOTAL, "walk_away": walk,
            "all_goods_worth": BG.worth(hand, pool),
            "offers": BG.worth_of_offers(data.get("offers") or [], agent_id, hand),
            "reveal": "when the game ends: submit_action(session_id, 'reveal', {}) (your tools fill it in)"}


def r_scope(sess: dict[str, Any]) -> str:
    return ((((sess.get("state") or {}).get("data") or {}).get("rand") or {}).get("scope")
            or ((sess.get("config") or {}).get("rand") or {}).get("scope") or sess.get("session_id", ""))


def reveal_payload(rec: dict[str, Any] | None) -> dict[str, str] | None:
    return {"s": rec["s"], "nonce": rec["nonce"]} if rec and rec.get("s") and rec.get("nonce") else None
