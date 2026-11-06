"""Marketplace listing routes (P56).

Moved verbatim out of `relay_server`; the only edits are `@app` becoming
`@router` and shared relay state resolving through `rs.` at call time rather
than being bound at import. See `emporia.routes` for why that distinction is
load-bearing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from emporia import relay_server as rs
from emporia import rooms as rooms_db
router = APIRouter()


@router.post("/listings")
async def create_listing(request: Request):
    """Gone: `emporia:listing:v1` is replaced by `emporia:post:v2`. What a listing did is a post of
    kind offer, challenge or pointer, with `ref` or `tool` tags saying what it is about."""
    raise HTTPException(410, "listings v1 are gone: post kind='offer' | 'challenge' | 'pointer' "
                             "(emporia:post:v2, POST /posts) instead")


@router.get("/listings")
async def get_listings(
    listing_type: str | None = None,
    module_type: str | None = None,
    creator_id: str | None = None,
    include_rooms: bool = True,
    limit: int = 50,
):
    """Browse all discoverable listings.

    Includes standard listings (sessions, services, events) plus public rooms and
    the paid rooms their creators advertised, when include_rooms=true (default).
    Rooms appear as listing_type='room' with gate_type, entry_fee_cents, and
    member_count visible. A private invite-only room is never shown.
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

    listings = rs.annotate_federated(rs.public_listings(rows))

    # Surface rooms as browsable listings so agents can discover them
    if include_rooms and listing_type in (None, "room"):
        room_list = rooms_db.list_rooms(room_type="public", limit=limit, db_path=rs.DATABASE_PATH)
        # P249: a paid room is shown only when its creator advertised it — and then as
        # terms and a head count. An invite-only room is never shown.
        from emporia import adverts
        own_adverts = adverts.own_room_adverts()
        advertised = {r.room_id for r, _ in own_adverts}
        paid_private = [r for r, _ in own_adverts if r.room_type == "private"]
        from emporia.addresses import address
        here_host = rs._own_host()
        for room in room_list + paid_private:
            if not adverts.room_may_be_advertised(room.room_type, room.gate_type):
                continue
            listings.append({
                "listing_id": room.room_id,
                "listing_type": "room",
                "title": room.name,
                "description": room.description,
                "agent_id": room.creator_id,
                "agent_address": address(room.creator_id, here_host),
                "host": here_host,
                "advertised": room.room_id in advertised,
                "room_type": room.room_type,
                "gate_type": room.gate_type,
                "entry_fee_cents": room.entry_fee_cents if room.gate_type == "paid" else 0,
                "currency": room.currency,
                "member_count": len(room.members),
                "max_members": room.max_members,
                "encrypted": room.encrypted,
                # payment_mode is a *different* vocabulary (listings/events axis,
                # the old lobby's PaymentMode) — "stripe_link" here is
                # that axis's own value, deliberately not renamed by the P3 gate_type
                # rename. Reconciling the two vocabularies is out of scope (see
                # docs/archive/roadmap-hackathon-era.md's P3 section).
                "payment_mode": "stripe_link" if room.gate_type == "paid" else "free",
                "price_usd": f"{room.entry_fee_cents / 100:.2f}" if room.gate_type == "paid" else "0",
                "created_at": room.created_at,
                "metadata": {"linked_session_id": room.linked_session_id if room.room_type == "public" else None},
            })

    if module_type:
        listings = [l for l in listings if l.get("module_type") == module_type]

    # Newest first across both kinds: rooms were appended after a full page of
    # listings, so on a busy relay the cut below dropped every room.
    listings.sort(key=lambda l: l.get("created_at") or "", reverse=True)
    return {"listings": listings[:limit], "count": len(listings[:limit])}

