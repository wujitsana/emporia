"""`GET /games/export`: finished public games as JSON lines, a dataset for multi-agent research.

Each line is one game with everything needed to re-verify it offline (`emporia.game_export`,
`scripts/verify_export.py`). Private games, a game linked to a private room, and sealed games are never
included: only what `GET /sessions/{id}` already shows to anyone. Table talk is data the players
wrote and is marked untrusted. Nothing here is the relay's opinion except the signed referee result.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import Response

from emporia import game_export
from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.session_audit import get_public_log
from emporia.standing import game_module_types

router = APIRouter()

NDJSON = "application/x-ndjson"


def _seats(s: dict[str, Any]) -> list[dict[str, Any]]:
    from emporia.routes.standing import _who
    who = _who()
    players = s["participants"]
    ops = {p: (who[p].operator if p in who else p) for p in players}
    out = []
    with rs.DB_LOCK, rs.get_db() as conn:
        for i, p in enumerate(players):
            row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
                               (p,)).fetchone()
            stable = (row["stable_id"] if row else "") or ""
            out.append({"seat": i, "agent_id": p, "stable_id": stable,
                        "public_key_hex": (row["public_key_hex"] if row else "") or "",
                        "key_log": rs.agent_key_log(conn, stable),
                        "operator_distinct": all(ops[p] != ops[q] for q in players if q != p)})
    return out


def _talk(session_id: str) -> dict[str, Any]:
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT message_id, agent_id, content, created_at, kind, reply_to, root, step "
                            "FROM game_chat WHERE session_id = ? AND deleted_at IS NULL ORDER BY created_at",
                            (session_id,)).fetchall()
    return {"untrusted": True, "note": game_export.UNTRUSTED,
            "messages": [{**dict(r), "untrusted": True} for r in rows]}


def line_of(s: dict[str, Any]) -> dict[str, Any] | None:
    """One exported game, or None when it may not be exported."""
    if s["status"] != "completed" or rs._session_visibility(s) != "public" or rs.session_sealed(s):
        return None
    try:
        module = get_interaction_module(s["module_type"])
        state = SessionState.from_dict(s["state"])
        over, result = module.is_terminal(state)
    except Exception:  # noqa: BLE001 — a module this relay no longer loads, or an unreadable row
        return None
    if not over:
        return None
    data = s["state"].get("data") or {}
    rand_part = data.get("rand") if module.USES_RAND else None
    body = game_export.referee_body(s["session_id"], s["module_type"], result)
    from emporia import identity
    return {
        "format": game_export.FORMAT,
        "session_id": s["session_id"],
        "module": s["module_type"],
        "config": s.get("config") or {},
        "status": s["status"],
        "created_at": s["created_at"],
        "ended_at": s["updated_at"],
        "seats": _seats(s),
        "moves": get_public_log(s["session_id"]),
        "table_talk": _talk(s["session_id"]),
        "rand": rand_part,
        "final_state": s["state"],
        "result": result,
        "referee": {"body": body, "relay_public_key": identity.get_relay_public_key_hex(),
                    "signature": identity.sign_raw_relay(game_export.referee_bytes(body))},
    }


@router.get("/games/export")
async def export_games(module: str = "", since: str = "", limit: int = Query(50)):
    """Finished public games, oldest first, one JSON line each (`application/x-ndjson`).
    `module`: one module type; `since`: only games that ended after this ISO time (pass the
    `X-Next-Since` of the last page to continue); `limit`: at most this many (capped)."""
    limit = rs._clamp_limit(limit)
    marks = ",".join("?" * len(game_module_types())) or "NULL"
    q, params = f"SELECT * FROM sessions WHERE status = 'completed' AND module_type IN ({marks})", list(game_module_types())
    if module:
        q += " AND module_type = ?"
        params.append(module)
    if since:
        q += " AND updated_at > ?"
        params.append(since)
    q += " ORDER BY updated_at ASC LIMIT ?"
    params.append(limit * 4)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    lines, last = [], since
    for r in rows:
        if len(lines) >= limit:
            break
        s = rs._session_row_to_dict(r)
        last = s["updated_at"]
        if (line := line_of(s)) is not None:
            lines.append(json.dumps(line, sort_keys=True, default=str))
    return Response("".join(f"{x}\n" for x in lines), media_type=NDJSON,
                    headers={"X-Next-Since": last or "", "X-Export-Format": game_export.FORMAT,
                             "X-Export-Count": str(len(lines))})
