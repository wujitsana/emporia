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

**The prize is escrowed before the bounty exists (P72-5).** The first version of
this file recorded `prize_cents` in a JSON blob and held nothing, which made a
bounty a signup sheet with a number on it: an entrant could do the work and
discover the funder never had the money. `validate_spec` already refused a prize
below the payment floor on the grounds that finding out at payout is too late —
and "the funder has no funds" is the same discovery, one step later. So creation
now takes a manual-capture hold first and writes the row only if that hold
exists. A relay with no payment rail configured cannot post a bounty at all,
which is the honest failure: better than advertising a prize it could never pay.
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
from emporia.module_sdk import get_interaction_module
from emporia.payments import cancel_payment_hold, create_stake_intent

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

    # Escrow first. A bounty row that exists without a hold behind it is a
    # promise this relay cannot keep, so the hold is the precondition for the
    # row rather than a step after it.
    rs._assert_agent_budget(caller, spec.prize_cents)
    try:
        escrow = await create_stake_intent(
            session_id=listing_id,
            amount_cents=spec.prize_cents,
            buyer_id=caller,
            seller_id="",           # unknown until the contest is decided
            service_type=spec.module_type,
            capture_method="manual",
            resource_type="bounty",
        )
    except Exception as e:
        # Deliberately broad. `create_stake_intent` raises RuntimeError for "no
        # rail configured", but an unreachable Stripe surfaces as
        # httpx.ConnectError and a malformed response as something else again —
        # and catching only RuntimeError turned an unreachable payment provider
        # into a 500 with a full traceback instead of the refusal written right
        # here. Every one of them means the same thing to the caller: there is
        # no hold, so there is no bounty.
        raise HTTPException(402, (
            f"The prize could not be escrowed, so this bounty was not created: "
            f"{type(e).__name__}: {e}"
        ))
    payment_intent_id = escrow.get("payment_intent_id") or escrow.get("id")

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
                 # The hold. Settlement captures this and pays the winner;
                 # the no-entrants path cancels it.
                 "payment_intent_id": payment_intent_id,
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
            "entry_deadline": spec.entry_deadline.isoformat(), "state": "open",
            "prize_escrowed": True}


async def _start_contest(listing_id: str) -> dict:
    """Seat the entrants and hand the funder's hold to the session (P72-6).

    The trick that makes this small: the contest is an ordinary session, and
    the funder's escrowed PaymentIntent is recorded as that session's stake
    row. So when the module reaches a terminal state, the *existing* settlement
    path in routes/sessions.py reads it, captures it, and transfers to the
    winner — which P72-1 proved already works when the payer is not the winner.
    Nothing about settlement is bounty-aware, and nothing needs to be.
    """
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE listing_id = ? AND listing_type = ?",
            (listing_id, rules.BOUNTY_LISTING_TYPE),
        ).fetchone()
        if not row:
            raise HTTPException(404, "No such bounty")
        meta = json.loads(row["metadata_json"] or "{}")
        if meta.get("state") != "open":
            raise HTTPException(409, f"This bounty is {meta.get('state')}, not open.")
        spec = _spec_from_row(row)
        entrants = [
            r["agent_id"] for r in conn.execute(
                "SELECT agent_id FROM bounty_entries WHERE listing_id = ? ORDER BY entered_at",
                (listing_id,),
            ).fetchall()
        ]

    if not rules.is_ready_to_run(spec, len(entrants)):
        raise HTTPException(409, (
            f"Not ready: {len(entrants)} entrant(s), needs {spec.min_entrants} "
            f"(or {spec.max_entrants} to start early). Deadline "
            f"{spec.entry_deadline.isoformat()}."
        ))

    module = get_interaction_module(spec.module_type)
    seated = entrants[:spec.max_entrants]
    valid, err = module.validate_participants(seated)
    if not valid:
        raise HTTPException(409, f"The module refused these participants: {err}")

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC).isoformat()
    state = module.initial_state(seated, {"bounty_id": listing_id})
    rs.save_session(
        session_id=session_id, module_type=spec.module_type,
        config={"bounty_id": listing_id, "prize_cents": spec.prize_cents},
        payment_rules={}, state=state, current_agent=seated[0],
        step_number=0, status="active", participants=seated,
        created_at=now, updated_at=now,
    )
    for a in seated:
        # stake_paid False: entrants pay nothing. The prize is the funder's.
        rs.add_participant(session_id, a, stake_paid=False)

    pi = meta.get("payment_intent_id")
    if pi:
        rs.record_payment(
            payment_intent_id=pi, agent_id=spec.funder_id,
            amount_cents=spec.prize_cents, payment_type="session_stake",
            session_id=session_id, rail="stripe",
        )

    with rs.DB_LOCK, rs.get_db() as conn:
        meta |= {"state": "running", "session_id": session_id, "seated": seated}
        conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                     (json.dumps(meta), listing_id))
    await rs.broadcast_global({
        "type": "bounty_started", "listing_id": listing_id,
        "session_id": session_id, "participants": seated,
    })
    return {"listing_id": listing_id, "session_id": session_id,
            "participants": seated, "state": "running"}


@router.post("/bounties/{listing_id}/start")
async def start_bounty(listing_id: str, request: Request):
    """Run the contest. Any registered agent may trigger it — readiness is
    decided by the rule, not the caller, so there is nothing to gain by being
    the one who asks. That keeps it callable from a cron without inventing an
    operator credential for a decision the relay already owns."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    return await _start_contest(listing_id)


@router.post("/bounties/{listing_id}/cancel")
async def cancel_bounty(listing_id: str, request: Request):
    """Release the hold on a bounty nobody entered.

    Only the funder, and only while the entrant list is empty — once an agent
    has committed time to a contest, the funder withdrawing the prize is the
    defection the escrow exists to prevent. After the deadline with entrants
    present the bounty settles or refunds through the module, not through here.

    The hold expires by itself in 5-7 days, so this is the fast path rather
    than the only one; a relay that dies still returns the funder's money.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE listing_id = ? AND listing_type = ?",
            (listing_id, rules.BOUNTY_LISTING_TYPE),
        ).fetchone()
        if not row:
            raise HTTPException(404, "No such bounty")
        if row["agent_id"] != caller:
            raise HTTPException(403, "Only the funder may cancel a bounty.")
        entrants = conn.execute(
            "SELECT COUNT(*) FROM bounty_entries WHERE listing_id = ?", (listing_id,)
        ).fetchone()[0]
        if entrants:
            raise HTTPException(409, (
                f"{entrants} agent(s) have entered. A funded contest cannot be withdrawn "
                "once agents have committed to it — that is what the escrow is for."
            ))
        meta = json.loads(row["metadata_json"] or "{}")
        pi = meta.get("payment_intent_id")

    released = False
    if pi:
        try:
            await cancel_payment_hold(pi)
            released = True
        except Exception as e:
            # The hold's own 5-7 day expiry is the backstop, so a failure here
            # is reported rather than retried into a loop.
            raise HTTPException(502, (
                f"Could not release the hold ({e}). It expires on its own in 5-7 days; "
                "the bounty has been left open rather than closed with money still held."
            ))
    with rs.DB_LOCK, rs.get_db() as conn:
        meta["state"] = "cancelled"
        conn.execute(
            "UPDATE listings SET metadata_json = ?, expires_at = ? WHERE listing_id = ?",
            (json.dumps(meta), datetime.now(UTC).isoformat(), listing_id),
        )
    return {"listing_id": listing_id, "state": "cancelled", "hold_released": released}


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
