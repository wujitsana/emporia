"""Marketplace listing routes (P56).

Moved verbatim out of `relay_server`; the only edits are `@app` becoming
`@router` and shared relay state resolving through `rs.` at call time rather
than being bound at import. See `emporia.routes` for why that distinction is
load-bearing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia.wire import (
    ERR_DUPLICATE_NONCE,
    MAX_LISTING_HOURS,
    listing_id_for,
    signed_listing_payload,
)

router = APIRouter()


def _verify_agent_signed(req: rs.CreateListingRequest, caller: str) -> tuple[str, str, str]:
    """P240: a listing is its agent's signed word, checked here against the
    caller's registered key before anything is stored — so the relays that pull
    it can check it too, without taking this relay's word for it. Returns the
    listing's id (from the agent's nonce), the key and the stable id.

    No unsigned path: a listing a relay writes itself (a bounty, an event's)
    has no signature and stays on that relay."""
    if not rs._NONCE_RE.fullmatch(req.listing_nonce):
        raise HTTPException(400, "listing_nonce must be 32-64 hex characters")
    if set(req.metadata) & rs._PRIVATE_LISTING_METADATA:
        raise HTTPException(400, "metadata keys "
                            f"{sorted(set(req.metadata) & rs._PRIVATE_LISTING_METADATA)} are the relay's own")
    try:
        expires = datetime.fromisoformat(req.expires_at)
    except ValueError as e:
        raise HTTPException(400, "expires_at must be an ISO 8601 time") from e
    if expires.tzinfo is None or expires.astimezone(UTC).isoformat() != req.expires_at:
        raise HTTPException(400, "expires_at must be in UTC, as wire.listing_expires_at() writes it")
    now = datetime.now(UTC)
    if expires <= now:
        raise HTTPException(400, "expires_at is in the past")
    if expires > now + timedelta(hours=MAX_LISTING_HOURS, minutes=5):
        raise HTTPException(400, f"a listing can be up for at most {MAX_LISTING_HOURS} hours")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
                           (caller,)).fetchone()
    key = (row["public_key_hex"] or "").lower() if row else ""
    stable_id = row["stable_id"] if row else ""     # P245: its key may since have changed; its identity has not
    signed = signed_listing_payload(
        stable_id, rs.RELAY_BASE_URL, req.listing_nonce, req.title, req.description,
        req.listing_type, req.payment_mode, req.price_usd, req.module_type, req.metadata,
        req.expires_at)
    if not key or not rs.verify(signed, req.signature, key):
        raise HTTPException(403, "listing signature does not match this agent's key "
                                 "(signed for this relay's relay_url, from /health?)")
    return listing_id_for(stable_id, req.listing_nonce), key, stable_id


@router.post("/listings")
async def create_listing(req: rs.CreateListingRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P240: the signature first, before the (possibly paid) guardrails scan, as P21-2 (B3) ordered the message routes.
    listing_id, key, stable_id = _verify_agent_signed(req, caller)
    # P19-7 (M4)/P20-5 (A3): title/description/metadata used to be bounded by
    # hand here. P21-5 (B5): rs._assert_payload_safe_counted now bounds every
    # top-level field of whatever it's handed, so this is redundant — see
    # rs._require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))

    now = datetime.now(UTC).isoformat()

    import sqlite3
    try:
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute(
                "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
                "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, created_at, "
                "agent_public_key_hex, agent_stable_id, listing_nonce, signature) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (listing_id, req.title, req.description, req.listing_type, caller,
                 req.payment_mode, req.price_usd, req.module_type, rs.RELAY_BASE_URL,
                 req.expires_at, json.dumps(req.metadata), now,
                 key, stable_id, req.listing_nonce, req.signature),
            )
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, {"code": ERR_DUPLICATE_NONCE,
                                  "message": "a listing with this nonce is already up"}) from e
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

    listings = rs.annotate_federated(rs.public_listings(rows))

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
                # the old lobby's PaymentMode) — "stripe_link" here is
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

