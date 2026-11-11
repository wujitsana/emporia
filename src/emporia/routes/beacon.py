"""`GET /beacon/{round}`: a cache of drand's public rounds for the randomness service
(`emporia:rand:v1`, architecture-v2 section 5.1).

The relay is a courtesy here, not a trust point: it only returns a round whose BLS signature it
has verified against the pinned chain key, and every client verifies the signature again itself.
A relay that served a wrong value, or none, is caught at once, or simply bypassed (a client
falls back to drand's own servers).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from emporia import beacon, rand
from emporia import relay_server as rs

router = APIRouter()

#: Beacon reads the relay makes for clients (not for its own game checks) per round are cached.
_FETCH = beacon.http_get      # a seam: tests replace it with recorded beacons


def cached(round_no: int) -> str | None:
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT signature FROM beacon_rounds WHERE round = ?", (round_no,)).fetchone()
    return row["signature"] if row else None


def signature(round_no: int) -> str | None:
    """The verified signature of a round that has been published, from the cache or drand; else None."""
    if not beacon.available(round_no, time.time()):
        return None
    sig = cached(round_no)
    if sig:
        return sig
    sig = beacon.fetch(round_no, _FETCH)
    if sig:
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO beacon_rounds (round, signature, fetched_at) VALUES (?, ?, ?)",
                         (round_no, sig, datetime.now(UTC).isoformat()))
    return sig


#: A round the relay will look up for an anonymous reader, if it has not stored it: one this recent (rounds
#: it holds for its own games are stored when it checks them, and are served whatever their age).
RECENT_ROUNDS = 200          # 10 minutes of 3 s rounds
#: Uncached lookups (drand fetches plus a BLS check) the route makes for readers, relay-wide, per minute.
LOOKUPS_PER_MINUTE = 20
_lookups: list[float] = []


def _lookup_budget(now: float) -> bool:
    _lookups[:] = [t for t in _lookups if now - t < 60]
    if len(_lookups) >= LOOKUPS_PER_MINUTE:
        return False
    _lookups.append(now)
    return True


@router.get("/beacon/{round_no}")
async def get_beacon(round_no: int) -> dict[str, Any]:
    if round_no < 1:
        raise HTTPException(400, "a drand round is a whole number from 1")
    if not beacon.available(round_no):
        raise HTTPException(404, {"error": "round not published yet",
                                  "round": round_no, "available_at": rand.round_time(round_no)})
    sig = await run_in_threadpool(cached, round_no)
    if not sig:
        now = time.time()
        if rand.round_at(now) - round_no > RECENT_ROUNDS:
            raise HTTPException(404, {"error": "this relay holds only recent rounds and the ones its games used; "
                                               "ask drand directly", "round": round_no})
        if not _lookup_budget(now):
            raise HTTPException(429, "this relay is looking up rounds as fast as it allows; ask drand directly "
                                     "or retry shortly")
        sig = await run_in_threadpool(lambda: signature(round_no))     # looked up at call time
    if not sig:
        raise HTTPException(502, "this relay could not read that round from drand; ask drand directly")
    return {"round": round_no, "signature": sig, "chain": rand.CHAIN_HASH, "scheme": rand.SCHEME}
