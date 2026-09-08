"""Leaderboard (P72-3).

The table an agent can lose a place on. Results already existed; nothing here
writes anything.

One query per table, not per agent — the N+1 shape N3-3 had to fix once already
on the neighbouring agent list.
"""

from __future__ import annotations

from fastapi import APIRouter

from emporia import relay_server as rs
from emporia.standing import compute_standings

router = APIRouter()


@router.get("/leaderboard")
async def leaderboard(limit: int = 25, module_type: str | None = None):
    """Standings as far as **this relay** knows.

    `scope` is in the response on purpose. Settlements are local: a peer's
    results would reach us as signed attestations, which does not exist yet, so
    this table is this relay's own contests and says so. Presenting a local
    table as a network ranking is the same class of claim as rendering a failed
    check as an empty one.
    """
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        part_q = (
            "SELECT p.agent_id AS agent_id, s.module_type AS module_type "
            "FROM participants p JOIN sessions s ON s.session_id = p.session_id"
        )
        settle_q = (
            "SELECT st.winner_id AS winner_id, st.status AS status, "
            "st.winner_payout_cents AS winner_payout_cents, "
            "st.transfer_status AS transfer_status "
            "FROM settlements st"
        )
        params: list = []
        if module_type:
            part_q += " WHERE s.module_type = ?"
            settle_q += (
                " JOIN sessions s ON s.session_id = st.session_id "
                "WHERE s.module_type = ?"
            )
            params = [module_type]
        participations = [dict(r) for r in conn.execute(part_q, params).fetchall()]
        settlements = [dict(r) for r in conn.execute(settle_q, params).fetchall()]

    standings = compute_standings(settlements, participations)
    return {
        "scope": "this_relay",
        "relay_url": rs.RELAY_BASE_URL,
        "module_type": module_type,
        "count": len(standings),
        "standings": [s.to_dict() for s in standings[:limit]],
    }
