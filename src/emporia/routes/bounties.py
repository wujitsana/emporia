"""Bounty endpoints (P72-4) — post one, list them, enter one.

`bounties.py` held the rules and nothing called them. This is what calls them.

A bounty is a `listings` row with `listing_type="bounty"`, so discovery,
federation gossip and the dashboard all work with no new plumbing — a bounty
posted here reaches a peer relay on the machinery P71 demonstrated. Entries live
in their own table because they need a UNIQUE constraint that a JSON blob in
`metadata_json` cannot give, and because a read-modify-write on that blob loses
a concurrent entry.

Settlement is deliberately not here. A held bounty settles by running the
module's own session and letting the existing `settle()` path pay out (P72-6); a
promised one is paid by the funder through `settle-promise`. `reconcile_bounties`
moves both to their terminal states.

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
from typing import Any
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from emporia import bounties as rules
from emporia import relay_server as rs
from emporia.module_sdk import MODULE_REGISTRY
from emporia.module_sdk import get_interaction_module
from emporia.payments import (
    RAIL_STRIPE, RAIL_X402, _x402_enabled, cancel_payment_hold, extract_mpp_token,
)

router = APIRouter()


class CreateBountyRequest(BaseModel):
    title: str
    description: str = ""
    module_type: str
    prize_cents: int
    entry_deadline: datetime
    # P79: which rail backs the prize. Defaults to the one that actually holds
    # money — a funder gets the strong guarantee unless they ask for the weak
    # one, rather than the other way round.
    rail: str = RAIL_STRIPE


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


def _require_hosted_here(row) -> None:
    """Refuse to act on a bounty another relay hosts (P83).

    Federation gossip copies a peer's bounties onto this board so they can be
    discovered — and every action route used to treat that copy as its own:
    entries recorded here for a contest run there, a peer's Stripe hold recorded
    as a local session's stake, a peer-reported state driving this relay's
    reconciler. The hold, the entries and the contest all live where the bounty
    was posted, so that is where it is entered.

    "Hosted here" is the same test `federated_listings_out` already uses to
    decide what this relay publishes as its own: `origin_relay` is this relay.
    """
    if row["origin_relay"] != rs.RELAY_BASE_URL:
        raise HTTPException(409, (
            f"This bounty is hosted on {row['origin_relay'] or 'another relay'}. "
            "Its prize, entries and contest live there; act on it there."
        ))


def _spec_from_row(row) -> rules.BountySpec:
    """Rebuild the spec a stored bounty row describes.

    Seats come from `rules.seats_for()`, not from BountySpec's defaults — this
    function used to take the defaults, which meant capacity was validated at
    the module's real size and then *enforced* at two. Invisible while both
    judging modules seat two; wrong for the first one that does not.
    """
    meta = json.loads(row["metadata_json"] or "{}")
    module_type = row["module_type"] or ""
    lo, hi = rules.seats_for(module_type, MODULE_REGISTRY)
    return rules.BountySpec(
        prize_cents=int(meta.get("prize_cents") or 0),
        module_type=module_type,
        entry_deadline=datetime.fromisoformat(meta["entry_deadline"]),
        funder_id=row["agent_id"],
        min_entrants=lo, max_entrants=hi,
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
            rail=req.rail,
        )
    except (rules.BountyRejected, ValueError) as e:
        raise HTTPException(400, str(e))

    listing_id = f"bty_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC).isoformat()

    # P77: the funder pays the way every other payer on this relay pays.
    #
    # P72-5 called `create_stake_intent()` directly, which POSTs a PaymentIntent
    # with no `payment_method` and no `confirm` — so it came back in
    # `requires_payment_method` and **held nothing**. The bounty recorded its id
    # and called that escrow. Two consequences, both bad: an entrant could work
    # for a prize that was never held, and at settlement `settle()` catches a
    # failed capture and pays the winner anyway, so the relay would have paid
    # out of its own platform balance while the funder was never charged.
    # (P82 closed that second half: `settle()` no longer transfers when a
    # capture fails.)
    #
    # It was also the wrong shape for agents: that flow returns a
    # `client_secret` for a human to confirm in a browser.
    #
    # Routing through `_verify_and_confirm_payment` fixes all of it at once. The
    # funder presents `Authorization: Payment <spt_token>` like anyone joining a
    # paid room; `confirm_spt()` creates *and confirms* in one call with
    # `capture_method="manual"`, so the hold is real and no human is involved.
    # No credential means a 402 challenge, which is the same answer this relay
    # gives every other unpaid request.
    rs._assert_agent_budget(caller, spec.prize_cents)

    # P79: a funder may back a prize with money or with their word, and the two
    # take different routes from here.
    #
    # `rail=x402` is a PROMISE: nothing is committed now, because nothing can
    # be. An EIP-3009 authorization signs `to`, so it cannot name a winner who
    # does not exist yet (P78), and holding the funds ourselves was declined —
    # this relay does not custody anyone's money. So the funder pays after the
    # result, and what backs the prize until then is the consequence of not
    # paying: a default is recorded and they cannot promise again until it is
    # settled. That rule is checked here, before anyone can enter.
    if req.rail == RAIL_X402:
        # P84: a promise is paid after the result, over x402, straight to the
        # winner. A relay that cannot take an x402 payment cannot honour one,
        # and the funder would default through no fault of their own.
        if not _x402_enabled():
            raise HTTPException(400, (
                "This relay does not have x402 enabled, so a promised prize could never "
                "be paid. Fund it on the held rail (rail='stripe') instead."
            ))
        with rs.DB_LOCK, rs.get_db() as conn:
            trust, _ = _agent_identity(conn, caller)
            unpaid, outstanding = _funder_promise_record(conn, caller)
        # P84: the consequence that backs a promise has to bind the operator,
        # not the key. Counted per key, a funder who defaulted registered a
        # fresh keypair (free, unlimited) and promised again. Entry is already
        # counted per Nous account for the same reason.
        if trust != "nous_verified":
            raise HTTPException(403, (
                "Posting a bounty backed by a promise requires nous_verified. Its only "
                "backing is the record of whether you paid, and that record has to follow "
                "your account rather than a key you can replace. A held prize needs no "
                "such record — fund it on rail='stripe'."
            ))
        allowed, why = rules.funder_may_promise(
            unpaid_defaults=unpaid, outstanding_promises=outstanding,
        )
        if not allowed:
            raise HTTPException(403, why)
        return await _write_bounty(
            listing_id=listing_id, req=req, spec=spec, caller=caller, now=now,
            rail=RAIL_X402, payment_intent_id=None,
        )

    if not rs.card_game_stakes_allowed():
        # P159: a bounty is a game with a money prize — not on cards.
        raise HTTPException(400, (
            "This relay does not take card money on games: Stripe restricts games of skill with "
            "money prizes. Back the prize with a promise (rail='x402'), paid straight to the "
            "winner after the result."
        ))
    if not rs.custodial_cards_allowed():
        # P157: a held prize waits on the operator's balance for a winner who
        # does not exist yet — custody, which this relay does not take.
        raise HTTPException(400, (
            "This relay does not hold card prizes: the money would sit on the operator's "
            "balance until a winner exists. Back the prize with a promise (rail='x402'), paid "
            "straight to the winner after the result."
        ))

    paid = await rs._verify_and_confirm_payment(
        request,
        agent_id=caller,
        identity_verified=True,
        amount_cents=spec.prize_cents,
        currency="usd",
        resource_type="bounty",
        resource_id=listing_id,
        service_type=spec.module_type,
        # The whole point: authorised now, captured when a winner is named.
        capture_method="manual",
        allowed_statuses=("requires_capture",),
        req_payment_intent_id=None,
        challenge_message=(
            f"Bounty prize: {spec.prize_cents} usd cents, held until the contest is "
            f"decided. Retry with Authorization: Payment <spt_token>."
        ),
        challenge_resource=f"emporia:bounty:{listing_id}",
        creator_id=None,   # no x402 analogue yet — see BOUNTY_RAILS
    )
    if isinstance(paid, JSONResponse):
        return paid          # 402 with the challenge; no bounty was created

    return await _write_bounty(
        listing_id=listing_id, req=req, spec=spec, caller=caller, now=now,
        rail=paid.rail, payment_intent_id=paid.reference,
    )


def _funder_promise_record(conn, agent_id: str) -> tuple[int, int]:
    """(unpaid defaults, outstanding promises) for this funder.

    Read together in one pass because `funder_may_promise()` weighs them
    together, and two queries a moment apart could disagree about the same
    funder mid-contest.
    """
    # P84: counted across every persona of the funder's Nous account, via the
    # `funder_nous_user_id` pinned on each bounty at funding — otherwise a
    # default follows only the key that incurred it.
    _, nous_id = _agent_identity(conn, agent_id)
    rows = conn.execute(
        "SELECT metadata_json FROM listings WHERE listing_type = ? AND origin_relay = ? "
        "AND (agent_id = ? OR (? IS NOT NULL AND "
        "json_extract(metadata_json, '$.funder_nous_user_id') = ?))",
        # P83: hosted here only. A peer's bounty rows carry whatever agent_id
        # and state the peer wrote, and an agent_id is only unique per relay —
        # counting them let a peer plant a default against a local agent.
        (rules.BOUNTY_LISTING_TYPE, rs.RELAY_BASE_URL, agent_id, nous_id, nous_id),
    ).fetchall()
    unpaid = outstanding = 0
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("rail") != "x402":
            continue
        state = meta.get("state")
        if state == "defaulted":
            unpaid += 1
        elif state in ("open", "running", "awaiting_payment"):
            # "settled", "refunded" and "cancelled" are terminal and free the
            # slot; "defaulted" is counted above as a default, not here.
            outstanding += 1
    return unpaid, outstanding


async def _write_bounty(*, listing_id, req, spec, caller, now, rail, payment_intent_id):
    """One row-writer for both rails (P79).

    Everything after the credential is identical whether the prize is held or
    promised, and two copies of it would be the divergence this file's own
    rules keep catching.
    """
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
                 # Which rail backs the prize. For a held prize this came from
                 # the verified credential; for a promised one it is what the
                 # funder declared, and nothing was verified because nothing was
                 # committed.
                 "rail": rail,
                 "state": "open",
             }), now),
        )
    await rs.broadcast_global({
        "type": "bounty_created", "listing_id": listing_id,
        "title": req.title, "module_type": spec.module_type,
        "prize_cents": spec.prize_cents,
    })
    strength = rules.escrow_strength(rail)
    return {"listing_id": listing_id, "prize_cents": spec.prize_cents,
            "module_type": spec.module_type,
            "entry_deadline": spec.entry_deadline.isoformat(), "state": "open",
            "rail": rail,
            # Named, not a bare boolean: "escrowed: true" would say the same
            # word for money set aside and a funder's word.
            "escrow_strength": strength,
            "prize_escrowed": strength == rules.ESCROW_HELD}


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
        _require_hosted_here(row)
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
        payment_rules={}, state=state,
        # P76: `state.current_agent`, not `seated[0]`. Who moves first is the
        # module's answer and it already gave it — the ordinary session route
        # has always read it from here. Hardcoding the first participant was
        # invisible for chess, where white is participants[0], and wrong for
        # `service`, where participants[0] is the buyer but the *seller* acts
        # first. A contest started that way is stuck from the opening move:
        # every action returns "Not your turn", nobody can play, and the prize
        # stays escrowed until the hold expires days later.
        current_agent=state.current_agent,
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
            # P77: the rail comes from the verified payment, recorded on the
            # bounty at funding. P75 replaced a "stripe" literal with the
            # RAIL_STRIPE constant, which was better but still this file
            # choosing an answer `_verify_and_confirm_payment` had already
            # given. A PaymentRef knows which rail it was verified on; that is
            # the tag, and nothing here needs an opinion.
            session_id=session_id, rail=meta.get("rail") or RAIL_STRIPE,
        )

    with rs.DB_LOCK, rs.get_db() as conn:
        meta |= {"state": "running", "session_id": session_id, "seated": seated}
        conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                     (json.dumps(meta), listing_id))
    await rs.broadcast_global({
        "type": "bounty_started", "listing_id": listing_id,
        "session_id": session_id, "participants": seated,
    })
    # P92: the seated entrants are told on their own channel, the same event an
    # ordinary session start sends. `bounty_started` goes only to the global
    # feed, so an entrant who was not watching it never learned the contest
    # they entered had begun, or that they had the first move.
    for a in seated:
        await rs.broadcast_to_agent(a, {
            "type": "session_started", "session_id": session_id,
            "module_type": spec.module_type, "participants": seated,
            "current_agent": state.current_agent, "bounty_id": listing_id,
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
        _require_hosted_here(row)
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
        if meta.get("state", "open") != "open":
            raise HTTPException(409, f"This bounty is {meta.get('state')}, not open.")
        pi = meta.get("payment_intent_id")
        # P85: claimed before the hold is released. Releasing it is a network
        # call, and the row used to stay `open` across it — an agent could
        # enter, the contest start, and this then overwrite `running` with
        # `cancelled` from its stale copy, leaving a live contest with no hold.
        # Enter and start both require `open`, so `cancelling` shuts them out.
        meta["state"] = "cancelling"
        conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                     (json.dumps(meta), listing_id))

    released = False
    if pi:
        try:
            await cancel_payment_hold(pi)
            released = True
        except Exception as e:
            # The hold's own 5-7 day expiry is the backstop, so a failure here
            # is reported rather than retried into a loop.
            with rs.DB_LOCK, rs.get_db() as conn:
                meta["state"] = "open"
                conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                             (json.dumps(meta), listing_id))
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
        # Seats are on the wire because `entrant_count` alone is a half-truth:
        # "2 entrants" reads as some interest when it can mean full and closed.
        # A reader deciding whether to enter needs the denominator.
        # P79: unknown modules are reachable here, so this cannot raise.
        # `seats_for()` raises by design — validation wants a hard failure — but
        # a *listing* row can name a module this relay does not have: federated
        # gossip carries bounties from peers running modules we never
        # registered, and a module can be removed after its bounties exist. One
        # such row raising would 500 the whole board, hiding every other bounty
        # behind an unrelated one. Unknown seats are reported as unknown.
        try:
            lo, hi = rules.seats_for(r["module_type"] or "", MODULE_REGISTRY)
        except KeyError:
            lo = hi = None
        out.append({
            "listing_id": r["listing_id"], "title": r["title"],
            "description": r["description"], "module_type": r["module_type"],
            "funder_id": r["agent_id"], "prize_cents": meta.get("prize_cents"),
            "entry_deadline": meta.get("entry_deadline"),
            "state": meta.get("state", "open"),
            "entrant_count": counts.get(r["listing_id"], 0),
            "min_entrants": lo, "max_entrants": hi,
            # Which backing this prize has: "held" means the funds are out of
            # the funder's reach and capture cannot fail for lack of money;
            # "promised" means nothing is set aside and the funder pays after the
            # result. An entrant deciding whether to spend an hour on a contest
            # is entitled to know which.
            # P83: guarded like seats above — a peer's bounty can name a rail
            # this relay does not know, and escrow_strength() raises for one.
            "escrow_strength": (
                rules.escrow_strength(meta["rail"])
                if meta.get("rail") in rules.BOUNTY_RAILS else None
            ),
            # Present only once the contest is running — the session anyone can
            # watch it in.
            "session_id": meta.get("session_id"),
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
        _require_hosted_here(row)
        spec = _spec_from_row(row)
        meta = json.loads(row["metadata_json"] or "{}")
        # P85: entry checked the deadline and the seat count but never the
        # state, so a cancelled bounty — whose deadline is still in the future —
        # kept taking entries for a prize that had been released.
        if meta.get("state", "open") != "open":
            raise HTTPException(409, f"This bounty is {meta.get('state')}; entry is closed.")
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
        # P84: a promised prize is paid by x402 straight to the winner's wallet.
        # An entrant with none could win a prize nobody can pay them, and the
        # funder would then be flagged for a default they could not avoid.
        if meta.get("rail") == RAIL_X402 and not rs.AGENT_REGISTRY.get_x402_pay_to_all(caller):
            raise HTTPException(403, (
                "This prize is promised and paid over x402 to the winner's wallet. "
                "Register an x402 payout wallet before entering, so a win can be paid."
            ))
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


def _mark_awaiting_payment(conn, listing_id: str, meta: dict, winner_id: str, now: datetime) -> dict:
    """A promised contest has resolved: the funder now owes the winner."""
    meta |= {
        "state": "awaiting_payment",
        "winner_id": winner_id,
        "resolved_at": now.isoformat(),
        "payment_due_at": rules.payment_due_at(now).isoformat(),
    }
    conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                 (json.dumps(meta), listing_id))
    return meta


@router.post("/bounties/{listing_id}/settle-promise")
async def settle_promise(listing_id: str, request: Request):
    """The funder pays a promised prize, after the contest named a winner.

    This is the step that could not exist at funding: the winner is known now,
    so an x402 authorization can finally name them. The funder presents it in
    PAYMENT-SIGNATURE exactly as any other x402 payment, and it settles
    immediately — there is nothing to defer any more.

    Only the funder may call it, and only while the bounty is awaiting payment.
    Paying late is still paying: a bounty already marked `defaulted` can be
    settled, which clears the flag. The consequence is for not paying, not for
    being slow — a funder with no way back would have no reason to settle at
    all, and the winner is the one who loses by that.
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
        _require_hosted_here(row)
        meta = json.loads(row["metadata_json"] or "{}")

    if row["agent_id"] != caller:
        raise HTTPException(403, "Only the funder pays their own bounty.")
    if meta.get("rail") != RAIL_X402:
        raise HTTPException(400, (
            "This bounty is backed by a held prize — it settles from escrow when the "
            "contest resolves and needs no payment from you."
        ))
    if meta.get("state") not in ("awaiting_payment", "defaulted"):
        raise HTTPException(409, (
            f"This bounty is {meta.get('state')!r}; nothing is owed. A promised prize "
            f"becomes payable when the contest names a winner."
        ))
    winner_id = meta.get("winner_id")
    if not winner_id:
        raise HTTPException(409, "No winner recorded for this bounty yet.")
    # P84: refused before `_verify_and_confirm_payment` would charge it. A card
    # payment lands on the platform's balance, and nothing here transfers it on
    # — so the bounty was marked settled while the winner received nothing.
    if extract_mpp_token(request.headers.get("Authorization")):
        raise HTTPException(400, (
            "A promised prize is paid over x402, straight to the winner's wallet. A "
            "card payment would reach the platform, not the winner, so it is not "
            "accepted here. Retry with a PAYMENT-SIGNATURE credential."
        ))

    paid = await rs._verify_and_confirm_payment(
        request,
        agent_id=caller,
        identity_verified=True,
        amount_cents=int(meta.get("prize_cents") or 0),
        currency="usd",
        resource_type="bounty",
        resource_id=listing_id,
        service_type=row["module_type"] or "emporia:bounty",
        capture_method="automatic",      # nothing to hold — this is the payment
        allowed_statuses=("succeeded",),
        req_payment_intent_id=None,
        challenge_message=(
            f"Owed to {winner_id}: {meta.get('prize_cents')} usd cents. "
            f"Retry with a credential paying that agent."
        ),
        challenge_resource=f"emporia:bounty:{listing_id}",
        creator_id=winner_id,            # the winner is who gets paid
    )
    if isinstance(paid, JSONResponse):
        return paid

    with rs.DB_LOCK, rs.get_db() as conn:
        was_late = meta.get("state") == "defaulted"
        meta |= {
            "state": "settled",
            "paid_at": datetime.now(UTC).isoformat(),
            "payment_reference": paid.reference,
            "paid_rail": paid.rail,
            # Kept rather than cleared: a funder who paid only after defaulting
            # has a different record from one who paid on time, and an entrant
            # deciding whether to trust them is entitled to both facts.
            "settled_late": was_late,
        }
        conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                     (json.dumps(meta), listing_id))
    return {"listing_id": listing_id, "state": "settled", "winner_id": winner_id,
            "settled_late": was_late, "payment_reference": paid.reference}


async def reconcile_bounties(limit: int = 500) -> dict[str, Any]:
    """Move bounties through the states nobody triggers by hand (P80).

    P79 shipped this covering one rail and one outcome, which left two dead
    ends and one loop that never ran:

    * **A held bounty never left `running`.** Nothing moved it — the session
      settlement path pays the winner and knows nothing about bounties, and
      `settle_promise` is x402-only. So a finished, paid contest showed on the
      board as still running, permanently.
    * **A refunded contest never left `running` on either rail.** A dispute, a
      timeout or a draw ends the session without a winner, and this only looked
      at `status == "settled"`. For a promised bounty that also consumed one of
      the funder's outstanding slots forever.
    * **Nothing called it.** The default flag is the only thing backing a
      promised prize, and it arrived exactly never outside a test. Same shape
      as P72-4, where `bounties.py` was a well-tested module with no callers.

    Every terminal session outcome now reaches a terminal bounty state, on both
    rails:

        running  + settled  + winner + held      -> settled   (already paid)
        running  + settled  + winner + held, capture failed -> unfunded
        running  + settled  + winner + promised  -> awaiting_payment (+ due date)
        running  + refunded                      -> refunded  (no winner, nothing owed)
        awaiting_payment past its due date       -> defaulted

    Deliberately a reconciler rather than a branch inside session settlement:
    that path pays people and is the last place to add a rail-conditional
    branch. Same reasoning as P51's payout loop.
    """
    now = datetime.now(UTC)
    counts: dict[str, list[str]] = {
        "settled": [], "awaiting_payment": [], "refunded": [], "defaulted": [],
        "unfunded": [],
    }
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE listing_type = ? AND origin_relay = ? "
            # P85: only the states this can move. It used to read the oldest
            # `limit` bounties of *any* state, so once that many had finished,
            # every run re-read the same finished rows and no newer bounty was
            # ever settled, refunded or defaulted again.
            "AND json_extract(metadata_json, '$.state') IN ('running', 'awaiting_payment') "
            "ORDER BY created_at LIMIT ?",
            (rules.BOUNTY_LISTING_TYPE, rs.RELAY_BASE_URL, limit),
        ).fetchall()

        for row in rows:
            meta = json.loads(row["metadata_json"] or "{}")
            state = meta.get("state")
            rail = meta.get("rail")

            if state == "running" and meta.get("session_id"):
                s = conn.execute(
                    "SELECT winner_id, status, transfer_status FROM settlements "
                    "WHERE session_id = ?",
                    (meta["session_id"],),
                ).fetchone()
                if not s:
                    continue                      # contest still being played
                if s["status"] == "settled" and s["winner_id"]:
                    if rail == RAIL_X402:
                        _mark_awaiting_payment(conn, row["listing_id"], meta,
                                               s["winner_id"], now)
                        counts["awaiting_payment"].append(row["listing_id"])
                    elif s["transfer_status"] == "capture_failed":
                        # P82: the funder's hold could not be captured (it
                        # expired or was released), so the winner was not
                        # paid. Saying "settled" would claim a payout that did
                        # not happen; this is its own terminal state, visible
                        # on the board and in promise-status.
                        meta |= {"state": "unfunded", "winner_id": s["winner_id"],
                                 "resolved_at": now.isoformat()}
                        conn.execute(
                            "UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                            (json.dumps(meta), row["listing_id"]),
                        )
                        counts["unfunded"].append(row["listing_id"])
                    else:
                        # The session path already captured the hold and paid
                        # the winner. Nothing is owed; the bounty is just done,
                        # and saying "running" about a paid contest is the kind
                        # of stale claim this board keeps being fixed for.
                        meta |= {"state": "settled", "winner_id": s["winner_id"],
                                 "resolved_at": now.isoformat()}
                        conn.execute(
                            "UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                            (json.dumps(meta), row["listing_id"]),
                        )
                        counts["settled"].append(row["listing_id"])
                else:
                    # Refund: dispute, timeout, or a draw the module could not
                    # decide. No winner, so nothing is owed on either rail —
                    # the held prize was released by the session path and the
                    # promised one never existed. Terminal either way, and a
                    # funder's outstanding-promise slot is freed.
                    meta |= {"state": "refunded", "resolved_at": now.isoformat(),
                             "refund_reason": s["status"]}
                    conn.execute(
                        "UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                        (json.dumps(meta), row["listing_id"]),
                    )
                    counts["refunded"].append(row["listing_id"])
                continue

            if state == "awaiting_payment":
                due = meta.get("payment_due_at")
                if due and now >= datetime.fromisoformat(due):
                    meta |= {"state": "defaulted", "defaulted_at": now.isoformat()}
                    conn.execute(
                        "UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                        (json.dumps(meta), row["listing_id"]),
                    )
                    counts["defaulted"].append(row["listing_id"])

    return counts


@router.get("/bounties/{listing_id}/promise-status")
async def promise_status(listing_id: str):
    """Whether a promised prize was actually paid.

    Public, and deliberately so: this is the record an entrant needs before
    deciding whether to compete for a prize backed by a funder's word rather
    than by money. A default that only the relay operator can see would not be
    a consequence.
    """
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE listing_id = ? AND listing_type = ?",
            (listing_id, rules.BOUNTY_LISTING_TYPE),
        ).fetchone()
        if not row:
            raise HTTPException(404, "No such bounty")
        unpaid, outstanding = _funder_promise_record(conn, row["agent_id"])
    meta = json.loads(row["metadata_json"] or "{}")
    rail = meta.get("rail")
    return {
        "listing_id": listing_id,
        "funder_id": row["agent_id"],
        "rail": rail,
        "escrow_strength": rules.escrow_strength(rail) if rail in rules.BOUNTY_RAILS else None,
        "state": meta.get("state"),
        "winner_id": meta.get("winner_id"),
        "payment_due_at": meta.get("payment_due_at"),
        "paid_at": meta.get("paid_at"),
        # A funder who paid only after defaulting has a different record from
        # one who paid on time. Both facts, not the kinder one.
        "settled_late": meta.get("settled_late"),
        "defaulted_at": meta.get("defaulted_at"),
        # This funder's standing overall, so an entrant does not have to walk
        # every bounty they ever posted to learn it.
        "funder_unpaid_defaults": unpaid,
        "funder_outstanding_promises": outstanding,
    }
