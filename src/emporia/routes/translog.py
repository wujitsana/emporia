"""P256: the transparency log, read only and public (translog.py).

Anyone may check a relay's log, so nothing here needs a credential: the leaves
are hashes and commitments, never content.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from emporia import relay_server as rs
from emporia import translog

router = APIRouter()


@router.get("/log/state")
async def log_state(size: int | None = None):
    """The latest signed log state, or the one signed at `size`."""
    with rs.DB_LOCK, rs.get_db() as conn:
        state = translog.state_at(conn, size) if size is not None else translog.latest_state(conn)
    if state is None:
        raise HTTPException(404, "no log state signed at that size" if size is not None else "no log state signed yet")
    return state


@router.get("/log/entries")
async def log_entries(start: int = 0, end: int | None = None, ref: str = "", limit: int = 100):
    """Leaves from `start` (to `end`), oldest first, at most `limit` (1000). With
    `ref`, only the leaves filed under it — an agent's key step, or a commitment
    recomputed from a record its holder has."""
    with rs.DB_LOCK, rs.get_db() as conn:
        return {"entries": translog.entries(conn, max(0, start), end, ref, limit)}


def _sized(conn, size: int | None) -> int:
    held = translog.latest_state(conn)
    n = size if size is not None else (held["size"] if held else 0)
    if not n or translog.state_at(conn, n) is None:
        raise HTTPException(404, "proofs are given against a signed log state: no state at that size")
    return n


@router.get("/log/proof/inclusion")
async def log_inclusion_proof(index: int, size: int | None = None):
    """Proof that leaf `index` is in the log of `size` (default: the latest signed state)."""
    with rs.DB_LOCK, rs.get_db() as conn:
        n = _sized(conn, size)
        if not 0 <= index < n:
            raise HTTPException(400, f"index must be between 0 and {n - 1}")
        return {"index": index, "size": n, "leaf_hash": translog.leaf_at(conn, index).hex(),
                "proof": [h.hex() for h in translog.stored_inclusion_proof(conn, index, n)]}


@router.get("/log/proof/consistency")
async def log_consistency_proof(first: int, second: int | None = None):
    """Proof that the log of size `second` (default: latest) only added to the log of size `first`."""
    with rs.DB_LOCK, rs.get_db() as conn:
        n = _sized(conn, second)
        if translog.state_at(conn, first) is None:
            raise HTTPException(404, "no log state signed at the first size")
        if not 0 < first <= n:
            raise HTTPException(400, "first must be between 1 and second")
        return {"first": first, "second": n,
                "proof": [h.hex() for h in translog.stored_consistency_proof(conn, first, n)]}
