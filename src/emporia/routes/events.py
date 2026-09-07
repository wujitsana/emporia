"""Event and tournament routes (P56).

Moved verbatim out of `relay_server`; the only edits are `@app` becoming
`@router` and shared relay state resolving through `rs.` at call time rather
than being bound at import. See `emporia.routes` for why that distinction is
load-bearing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from emporia import relay_server as rs

router = APIRouter()


@router.post("/events")
async def create_event(req: rs.CreateEventRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P21-5 (B5): title/description bounded automatically by
    # rs._assert_payload_safe_counted now — see rs._require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))

    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO events (event_id, title, description, module_type, organizer_id, "
            "payment_mode, entry_fee_usd, status, bracket_json, participants_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '{}', '[]', ?, ?)",
            (event_id, req.title, req.description, req.module_type, caller,
             req.payment_mode, req.entry_fee_usd, now, now),
        )
    # Also create a listing for the event
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
            "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, created_at) "
            "VALUES (?, ?, ?, 'event', ?, ?, ?, ?, ?, NULL, ?, ?)",
            (f"lst_{event_id}", req.title, req.description or "", caller,
             req.payment_mode, req.entry_fee_usd, req.module_type, rs.RELAY_BASE_URL,
             json.dumps({"event_id": event_id}), now),
        )
    return {"event_id": event_id, "status": "open"}


@router.get("/events")
async def list_events(status: str | None = None, limit: int = 50):
    """N4-5 (N4 review R7): previously had no `limit` param at all — every event, with
    full bracket + participant lists, in one unbounded query. Public discovery stays
    public (same shape as GET /listings); this just caps the row scan, matching every
    other list endpoint after N3-6.

    N6 review R2: `count` (added N5-5) is the returned page size, not a total — it
    can never exceed `limit`, so it could never actually signal truncation despite
    that being N5-5's stated goal. `has_more` (via rs._fetch_page) is the real signal.
    """
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        if status:
            rows, has_more = rs._fetch_page(
                conn,
                "SELECT * FROM events WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status,), limit,
            )
        else:
            rows, has_more = rs._fetch_page(
                conn, "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (), limit,
            )
    events = [
        {
            **dict(r),
            "bracket": json.loads(r["bracket_json"]),
            "participants": json.loads(r["participants_json"]),
        }
        for r in rows
    ]
    return {"events": events, "count": len(events), "has_more": has_more}


@router.get("/events/{event_id}")
async def get_event(event_id: str):
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Event not found")
    return {
        **dict(row),
        "bracket": json.loads(row["bracket_json"]),
        "participants": json.loads(row["participants_json"]),
    }

