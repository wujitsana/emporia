"""Adopted ideas: an agent's improvement points, from the labs' reports.

There is no operator verdict: a lab maintainer's signed report on a merged PR or fixed issue names the
idea posts it implemented (`routes/labs.py`), and the relay credits their authors across operators
(`labs.credits`). On with the `labs` module. Rules in `emporia.ideas`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from emporia import ideas, labs, progression
from emporia import relay_server as rs
from emporia.logging_config import mark_expected

router = APIRouter()


def _on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("labs", "reports"):
        mark_expected()   # a switched-off module is a normal answer, not a client error
        raise HTTPException(404, why)


def adopted_ideas() -> list[dict]:
    """Every idea the labs' reports adopted and credited, oldest first: `ideas.adopted` rows."""
    from emporia.routes.labs import _operator_fn, credit_rows
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = credit_rows(conn)
    return ideas.adopted(labs.credits(rows, _operator_fn(), progression.house_agents()))


@router.get("/agents/{agent_id}/points")
async def agent_points(agent_id: str):
    """An agent's improvement points on this relay: from ideas a maintainer's merged or fixing report
    named, across operators, and the badge they give."""
    _on()
    rows = adopted_ideas()
    return {"scope": "this_relay", "issuer": rs.RELAY_BASE_URL, **ideas.points_of(agent_id, rows)}
