"""Bounty endpoints (P72-4) — post one, list them, enter one.

`bounties.py` held the rules and nothing called them. This is what calls them.

A bounty is a `listings` row with `listing_type="bounty"`, so discovery,
federation gossip and the dashboard all work with no new plumbing — a bounty
posted here reaches a peer relay on the machinery P71 demonstrated. Entries live
in their own table because they need a UNIQUE constraint that a JSON blob in
`metadata_json` cannot give, and because a read-modify-write on that blob loses
a concurrent entry.

Settlement is deliberately not here. A bounty settles by running the module's
own session and letting the existing `settle()` path pay out, which P72-1 proved
already handles the asymmetric case. That is the next phase.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import bounties as rules
from emporia import relay_server as rs
from emporia.module_sdk import MODULE_REGISTRY

router = APIRouter()


class CreateBountyRequest(BaseModel):
    title: str
    description: str = ""
    module_type: str
    prize_cents: int
    entry_deadline: datetime


def _agent_identity(conn, agent_id: str) -> tuple[str, str | None]:
    """(trust_level, nous_user_id) — read together because entry needs both and
    reading them separately invites them to disagree."""
    row = conn.execute(
        "SELECT trust_level, nous_user_id FROM authorized_agents WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Unknown agent {agent_id!r}")
    return (row["trust_level"] or "key_only"), row["nous_user_id"]


def _spec_from_row(row) -> rules.BountySpec:
    meta = json.loads(row["metadata_json"] or "{}")
    return rules.BountySpec(
        prize_cents=int(meta.get("prize_cents") or 0),
        module_type=row["module_type"] or "",
        entry_deadline=datetime.fromisoformat(meta["entry_deadline"]),
        funder_id=row["agent_id"],
    )


@router.post("/bounties")
async def create_bounty(req: CreateBountyRequest, request: Request):
    """Post a funded contest. Refused now if it could not be settled later."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    try:
        await rs._assert_payload_safe_counted(req.model_dump(mode="json"))
    except PermissionError as e:
        raise HTTPException(403, str(e))

    try:
        spec = rules.validate_spec(
            prize_cents=req.prize_cents,
            module_type=req.module_type,
            entry_deadline=req.entry_deadline,
            funder_id=caller,
            registry=MODULE_REGISTRY,
        )
    except rules.BountyRejected as e:
        raise HTTPException(400, str(e))

    listing_id = f"bty_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        _, funder_nous = _agent_identity(conn, caller)
        conn.execute(
            "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
            "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (listing_id, req.title, req.description, rules.BOUNTY_LISTING_TYPE, caller,
             "bounty", f"{spec.prize_cents / 100:.2f}", spec.module_type, rs.RELAY_BASE_URL,
             spec.entry_deadline.isoformat(),
             json.dumps({
                 "prize_cents": spec.prize_cents,
                 "entry_deadline": spec.entry_deadline.isoformat(),
                 # Pinned at funding. Nothing reads it back from the funder's
                 # current row, so re-registering cannot change who is barred
                 # from entering a bounty already posted.
                 "funder_nous_user_id": funder_nous,
                 "state": "open",
             }), now),
        )
    await rs.broadcast_global({
        "type": "bounty_created", "listing_id": listing_id,
        "title": req.title, "module_type": spec.module_type,
        "prize_cents": spec.prize_cents,
    })
    return {"listing_id": listing_id, "prize_cents": spec.prize_cents,
            "module_type": spec.module_type,
            "entry_deadline": spec.entry_deadline.isoformat(), "state": "open"}


@router.get("/bounties")
async def list_bounties(limit: int = 50, open_only: bool = True):
    """Open contests, with how many have entered. Public discovery."""
    limit = rs._clamp_limit(limit)
    now = datetime.now(UTC).isoformat()
    q = "SELECT * FROM listings WHERE listing_type = ? "
    params: list = [rules.BOUNTY_LISTING_TYPE]
    if open_only:
        q += "AND (expires_at IS NULL OR expires_at > ?) "
        params.append(now)
    q += "ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(q, params).fetchall()
        counts: dict[str, int] = {}
        if rows:
            ids = [r["listing_id"] for r in rows]
            ph = ",".join("?" * len(ids))
            for c in conn.execute(
                f"SELECT listing_id, COUNT(*) AS c FROM bounty_entries "
                f"WHERE listing_id IN ({ph}) GROUP BY listing_id", ids,
            ).fetchall():
                counts[c["listing_id"]] = c["c"]
    out = []
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        out.append({
            "listing_id": r["listing_id"], "title": r["title"],
            "description": r["description"], "module_type": r["module_type"],
            "funder_id": r["agent_id"], "prize_cents": meta.get("prize_cents"),
            "entry_deadline": meta.get("entry_deadline"),
            "state": meta.get("state", "open"),
            "entrant_count": counts.get(r["listing_id"], 0),
            "origin_relay": r["origin_relay"],
        })
    return {"count": len(out), "bounties": out}


@router.post("/bounties/{listing_id}/enter")
async def enter_bounty(listing_id: str, request: Request):
    """Enter a contest. The Sybil rule is enforced twice, on purpose."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)

    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE listing_id = ? AND listing_type = ?",
            (listing_id, rules.BOUNTY_LISTING_TYPE),
        ).fetchone()
        if not row:
            raise HTTPException(404, "No such bounty")
        spec = _spec_from_row(row)
        meta = json.loads(row["metadata_json"] or "{}")
        trust, nous_id = _agent_identity(conn, caller)
        existing = {
            r["nous_user_id"] for r in conn.execute(
                "SELECT nous_user_id FROM bounty_entries WHERE listing_id = ?",
                (listing_id,),
            ).fetchall()
        }
        allowed, why = rules.may_enter(
            agent_id=caller,
            agent_nous_user_id=nous_id,
            is_nous_verified=(trust == "nous_verified"),
            spec=spec,
            funder_nous_user_id=meta.get("funder_nous_user_id"),
            existing_entrant_nous_ids=existing,
        )
        if not allowed:
            raise HTTPException(403, why)
        try:
            conn.execute(
                "INSERT INTO bounty_entries (listing_id, agent_id, nous_user_id, entered_at) "
                "VALUES (?,?,?,?)",
                (listing_id, caller, nous_id, datetime.now(UTC).isoformat()),
            )
        except sqlite3.IntegrityError:
            # The check above read, then this wrote. Two personas of one account
            # entering at once both pass the read and race here; the UNIQUE
            # index decides, and the loser gets the same answer the check would
            # have given it a moment later.
            raise HTTPException(403, (
                "This Nous account has already entered. Entry is one per account, not one "
                "per agent, because one Nous account may operate any number of personas."
            ))
    return {"listing_id": listing_id, "agent_id": caller, "entered": True}
