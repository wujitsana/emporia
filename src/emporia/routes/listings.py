"""Marketplace listing routes (P56).

Moved verbatim out of `relay_server`; the only edits are `@app` becoming
`@router` and shared relay state resolving through `rs.` at call time rather
than being bound at import. See `emporia.routes` for why that distinction is
load-bearing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from emporia import relay_server as rs
from emporia import rooms as rooms_db

router = APIRouter()


@router.post("/listings")
async def create_listing(req: rs.CreateListingRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P19-7 (M4)/P20-5 (A3): title/description/metadata used to be bounded by
    # hand here. P21-5 (B5): rs._assert_payload_safe_counted now bounds every
    # top-level field of whatever it's handed, so this is redundant — see
    # rs._require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))

    listing_id = f"lst_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC).isoformat()
    from datetime import timedelta
    expires_at = (datetime.now(UTC) + timedelta(hours=req.expires_in_hours)).isoformat()

    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
            "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (listing_id, req.title, req.description, req.listing_type, caller,
             req.payment_mode, req.price_usd, req.module_type, rs.RELAY_BASE_URL,
             expires_at, json.dumps(req.metadata), now),
        )
    await rs.broadcast_global({
        "type": "listing_created",
        "listing_id": listing_id,
        "title": req.title,
        "module_type": req.module_type,
        "agent_id": caller,
    })
    return {"listing_id": listing_id, "status": "created"}


@router.get("/listings")
async def get_listings(
    listing_type: str | None = None,
    module_type: str | None = None,
    creator_id: str | None = None,
    include_rooms: bool = True,
    limit: int = 50,
):
    """Browse all discoverable listings.

    Includes standard listings (sessions, services, events) plus public and
    paid-private rooms when include_rooms=true (default). Rooms appear as
    listing_type='room' with gate_type, entry_fee_cents, and member_count visible.
    Private invite-only rooms are never shown to non-members.
    """
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        q = "SELECT * FROM listings WHERE (expires_at IS NULL OR expires_at > ?) "
        params: list[Any] = [datetime.now(UTC).isoformat()]
        if listing_type and listing_type != "room":
            q += "AND listing_type = ? "
            params.append(listing_type)
        if module_type:
            q += "AND module_type = ? "
            params.append(module_type)
        if creator_id:
            q += "AND agent_id = ? "
            params.append(creator_id)
        q += "ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()

    listings = [
        {**dict(r), "metadata": json.loads(r["metadata_json"])}
        for r in rows
    ]

    # Surface rooms as browsable listings so agents can discover them
    if include_rooms and listing_type in (None, "room"):
        room_list = rooms_db.list_rooms(room_type="public", limit=limit, db_path=rs.DATABASE_PATH)
        # Also include paid private rooms (paid gate) — agents need to know they exist
        paid_private = rooms_db.list_rooms(room_type="private", limit=limit, db_path=rs.DATABASE_PATH)
        for room in room_list + paid_private:
            if room.gate_type == "invite":
                continue  # never surfaced — invite-only means truly private
            listings.append({
                "listing_id": room.room_id,
                "listing_type": "room",
                "title": room.name,
                "description": room.description,
                "agent_id": room.creator_id,
                "room_type": room.room_type,
                "gate_type": room.gate_type,
                "entry_fee_cents": room.entry_fee_cents if room.gate_type == "paid" else 0,
                "currency": room.currency,
                "member_count": len(room.members),
                "max_members": room.max_members,
                "encrypted": room.encrypted,
                # payment_mode is a *different* vocabulary (listings/events axis,
                # PaymentMode in engine/game_registry.py) — "stripe_link" here is
                # that axis's own value, deliberately not renamed by the P3 gate_type
                # rename. Reconciling the two vocabularies is out of scope (see
                # docs/archive/roadmap-hackathon-era.md's P3 section).
                "payment_mode": "stripe_link" if room.gate_type == "paid" else "free",
                "price_usd": f"{room.entry_fee_cents / 100:.2f}" if room.gate_type == "paid" else "0",
                "created_at": room.created_at,
                "metadata": {"linked_session_id": room.linked_session_id},
            })

    if module_type:
        listings = [l for l in listings if l.get("module_type") == module_type]

    return {"listings": listings[:limit], "count": len(listings[:limit])}

