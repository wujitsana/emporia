"""Session routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from emporia import relay_server as rs
from emporia.module_sdk import (
    STAKE_PURCHASE,
    STAKE_WAGER,
    MODULE_REGISTRY,
    SessionAction,
    SessionState,
    get_interaction_module,
)
from emporia.session_audit import (
    get_public_log,
    log_private_event,
    log_public_receipt,
    open_dual_session,
    verify_chain_entries,
)

router = APIRouter()


def _stake(payment_rules: dict[str, Any]) -> tuple[int, str]:
    """(stake in cents, currency) for a session's rules; 0 when it is free."""
    stake = payment_rules.get("stake_per_participant", "0")
    if payment_rules.get("mode", "free") == "free" or stake in ("0", "0.00", "", None):
        return 0, "usd"
    return int(float(stake) * 100), (payment_rules.get("currency") or "usd").lower()


def _is_bounty(s: dict[str, Any]) -> bool:
    """A bounty contest's stake is the funder's ordinary hold, whatever the module."""
    return bool((s.get("config") or {}).get("bounty_id"))


@router.post("/sessions")
async def create_session(req: rs.CreateSessionRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    if req.module_type not in MODULE_REGISTRY:
        raise HTTPException(400, f"Unknown module: {req.module_type}")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)

    module = get_interaction_module(req.module_type)
    payment_rules = req.payment_rules or module.PAYMENT_RULES.model_dump()
    if req.payment_rules:
        payment_rules = {**module.PAYMENT_RULES.model_dump(), **req.payment_rules}

    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    state = module.initial_state([caller], req.config)

    # P95: who stakes is the module's answer (STAKE_MODEL), not one rule for
    # every module. A wager's creator holds the same stake as the challenger —
    # before this they risked nothing, collecting the challenger's stake on a
    # win and returning it, less the fee, on a loss. A purchase's buyer funds
    # it once a seller has joined (/fund). A module with no stake model takes
    # no stake, rather than holding money it has no way to pay out.
    amount_cents, currency = _stake(payment_rules)
    creator_payment = None
    if amount_cents and module.STAKE_MODEL is None:
        raise HTTPException(400, (
            f"{req.module_type} takes no stake: it names no winner and no payee, so a "
            f"held stake could never be paid out. For paid work, create an "
            f"emporia:service:v1 session — the buyer pays the seller on delivery."
        ))
    if amount_cents and module.STAKE_MODEL == STAKE_WAGER:
        result = await rs._verify_and_confirm_payment(
            request,
            agent_id=caller,
            identity_verified=True,
            amount_cents=amount_cents,
            currency=currency,
            resource_type="session",
            resource_id=session_id,
            service_type=req.module_type,
            capture_method="manual",
            allowed_statuses=("requires_capture",),
            req_payment_intent_id=req.payment_intent_id,
            challenge_message=(
                f"A wager's creator stakes too: {amount_cents} {currency} cents, held until "
                f"the result. Retry with Authorization: Payment <spt_token>."
            ),
            challenge_resource=f"emporia:session:{session_id}",
        )
        if isinstance(result, JSONResponse):
            return result
        creator_payment = result
        try:
            rs.record_payment(
                payment_intent_id=creator_payment.reference, agent_id=caller,
                amount_cents=amount_cents, payment_type="session_stake",
                session_id=session_id, currency=currency, rail=creator_payment.rail,
            )
        except ValueError as e:
            raise HTTPException(402, str(e))

    rs.save_session(
        session_id=session_id,
        module_type=req.module_type,
        config=req.config,
        payment_rules=payment_rules,
        state=state,
        current_agent=state.current_agent,
        step_number=0,
        status="waiting",
        participants=[caller],
        created_at=now,
        updated_at=now,
    )
    rs.add_participant(session_id, caller, stake_paid=creator_payment is not None)
    open_dual_session(session_id, caller, req.module_type)

    await rs.broadcast_to_agent(caller, {
        "type": "session_created",
        "session_id": session_id,
        "module_type": req.module_type,
    })
    session = rs.load_session(session_id)
    if rs._session_visibility(session) == "public":
        await rs.broadcast_global({
            "type": "session_created",
            "session_id": session_id,
            "module_type": req.module_type,
            "creator_agent_id": caller,
        })
    return session
@router.get("/sessions")
async def list_sessions(request: Request, status: str | None = None, module_type: str | None = None):
    with rs.DB_LOCK, rs.get_db() as conn:
        q = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if module_type:
            q += " AND module_type = ?"
            params.append(module_type)
        q += " ORDER BY created_at DESC LIMIT 100"
        rows = conn.execute(q, params).fetchall()
    caller = rs._dashboard_agent_id(request)
    sessions = [rs._session_row_to_dict(r) for r in rows]
    return {"sessions": [s for s in sessions if rs._session_viewer_allowed(s, caller)]}
@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not rs._session_viewer_allowed(s, rs._dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    return s
@router.get("/sessions/{session_id}/actions")
async def get_session_actions(session_id: str, request: Request):
    """Return all recorded actions for a session — used for board replay."""
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not rs._session_viewer_allowed(s, rs._dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT action_id, agent_id, action_type, payload_json, result_json, created_at "
            "FROM actions WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
    return {
        "session_id": session_id,
        "actions": [
            {
                "action_id": r["action_id"],
                "agent_id": r["agent_id"],
                "action_type": r["action_type"],
                "payload": json.loads(r["payload_json"]),
                "result": json.loads(r["result_json"]) if r["result_json"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }
@router.get("/sessions/{session_id}/audit")
async def get_session_audit(session_id: str, request: Request):
    """Public hash-chained receipt log + chain-integrity verification for a session.

    Surfaces the tamper-evident audit trail (session_audit.py) to the dashboard so
    judges can see the "verified" badge, not just trust that hashing happens.

    "Public" is the chain-verification model (anyone with the log can recompute the
    hashes), not the access model — the log itself contains the full action payload
    (session_audit.py's log_public_receipt()), so this endpoint is gated the same as
    /sessions/{id}/actions rather than actually being open to anyone.
    """
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not rs._session_viewer_allowed(s, rs._dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    # P44: one read, not two. This used to call verify_chain(session_id) and
    # then get_public_log(session_id), reading and JSON-parsing the same file
    # twice per request — and verifying a *different* read than the one it
    # served, so a write landing between them would be returned unverified.
    # Off the loop for the same reason as the two chain routes: this parses the
    # whole log and serializes it back out, all of it blocking CPU.
    entries = await run_in_threadpool(get_public_log, session_id)
    ok, message = verify_chain_entries(entries)
    return {
        "session_id": session_id,
        "verified": ok,
        "message": message,
        "chain": entries,
    }
AWAITING_FUNDING = "awaiting_funding"


async def _start(session_id: str, s: dict[str, Any], participants: list[str],
                 payment_rules: dict[str, Any]) -> None:
    """Build the opening state and tell every participant the session began."""
    module = get_interaction_module(s["module_type"])
    state = module.initial_state(participants, s["config"])
    rs.save_session(
        session_id=session_id,
        module_type=s["module_type"],
        config=s["config"],
        payment_rules=payment_rules,
        state=state,
        current_agent=state.current_agent,
        step_number=0,
        status="active",
        participants=participants,
        created_at=s["created_at"],
        updated_at=datetime.now(UTC).isoformat(),
    )
    for p in participants:
        await rs.broadcast_to_agent(p, {
            "type": "session_started",
            "session_id": session_id,
            "module_type": s["module_type"],
            "participants": participants,
            # P92: who moves first, so the first mover is told it is their turn
            # without having to fetch the session to find out.
            "current_agent": state.current_agent,
        })


@router.post("/sessions/{session_id}/fund")
async def fund_session(session_id: str, request: Request):
    """The buyer funds a purchase, as a hold that pays the seller directly (P95).

    A destination charge: the hold names the seller's connected account and the
    operator fee, so on capture Stripe pays the seller and the platform its fee,
    and nothing sits on the platform balance in between. Possible only now that
    the seller has joined — Stripe fixes a charge's destination when the hold is
    created. Stripe only: an x402 payment cannot be held and released on a
    dispute. Credential as everywhere else: `Authorization: Payment <spt>`.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != AWAITING_FUNDING:
        raise HTTPException(409, f"Session is {s['status']}, not awaiting funding.")
    buyer, seller = s["participants"][0], s["participants"][1]
    if caller != buyer:
        raise HTTPException(403, "Only the buyer funds a purchase.")
    rs._enforce_agent_rate_limit(caller)

    account = rs.AGENT_REGISTRY.get_stripe_account(seller)
    if not account or rs.AGENT_REGISTRY.get_stripe_account_status(seller) != "active":
        raise HTTPException(409, (
            f"{seller} cannot receive payments yet: their Stripe account is not set up. "
            f"A purchase pays the seller directly, so it waits for that — they can run "
            f"start_stripe_onboarding, or abandon the session to release it."
        ))
    if rs._stripe_account_mode_mismatch(rs.AGENT_REGISTRY.get_stripe_account_mode(seller)):
        raise HTTPException(409, (
            f"{seller}'s Stripe account belongs to the other key mode (test vs live) "
            f"and cannot receive this payment."
        ))

    amount_cents, currency = _stake(s["payment_rules"])
    from emporia.payments import platform_fee
    rs._assert_agent_budget(caller, amount_cents)
    paid = await rs._verify_and_confirm_payment(
        request,
        agent_id=caller,
        identity_verified=True,
        amount_cents=amount_cents,
        currency=currency,
        resource_type="session",
        resource_id=session_id,
        service_type=s["module_type"],
        capture_method="manual",
        allowed_statuses=("requires_capture",),
        # SPT only: the direct-PaymentIntent route mints a hold without a
        # destination, which would pay the platform instead of the seller.
        req_payment_intent_id=None,
        challenge_message=(
            f"Fund this purchase: {amount_cents} {currency} cents, held and paid to "
            f"{seller} when you confirm delivery. Retry with Authorization: Payment <spt_token>."
        ),
        challenge_resource=f"emporia:session:{session_id}",
        destination_account=account,
        application_fee_cents=platform_fee(amount_cents),
    )
    if isinstance(paid, JSONResponse):
        return paid
    try:
        rs.record_payment(
            payment_intent_id=paid.reference, agent_id=caller, amount_cents=amount_cents,
            # The session's stake, like any other: settlement and abandon read
            # stake rows, and STAKE_MODEL decides how this one settles.
            payment_type="session_stake", session_id=session_id, currency=currency,
            rail=paid.rail,
        )
    except ValueError as e:
        raise HTTPException(402, str(e))
    # The opening state is built now, not at join, so a slow buyer does not
    # spend the seller's delivery window.
    await _start(session_id, s, s["participants"], s["payment_rules"])
    return rs.load_session(session_id)


@router.post("/sessions/{session_id}/join")
async def join_session(session_id: str, req: rs.JoinSessionRequest, request: Request):
    identity_verified = rs._require_caller_unless_mpp_payment(req.agent_id, request)
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != "waiting":
        raise HTTPException(400, f"Session not joinable: {s['status']}")
    if req.agent_id in s["participants"]:
        raise HTTPException(400, "Already joined")
    rs._require_registered(req.agent_id)
    # N12-5 (R5): identity_verified is False on the MPP-payment branch, where agent_id is
    # an unproven claim — don't spend someone else's write budget on the strength of it.
    if identity_verified:
        rs._enforce_agent_rate_limit(req.agent_id)

    module = get_interaction_module(s["module_type"])
    valid, err = module.validate_participants(s["participants"] + [req.agent_id])
    if not valid:
        raise HTTPException(400, err)

    # Payment gate. P95: only a wager charges the joiner — every participant
    # stakes, and the creator already has. A purchase's joiner is the seller,
    # who used to be the one charged here while the buyer paid nothing; the
    # buyer now funds it after this join, through /fund, to this seller.
    payment_rules = s["payment_rules"]
    amount_cents, currency = _stake(payment_rules)
    purchase = module.STAKE_MODEL == STAKE_PURCHASE and bool(amount_cents)

    stake_paid = False
    if amount_cents and not purchase:
        result = await rs._verify_and_confirm_payment(
            request,
            agent_id=req.agent_id,
            identity_verified=identity_verified,
            amount_cents=amount_cents,
            currency=currency,
            resource_type="session",
            resource_id=session_id,
            service_type=s["module_type"],
            capture_method="manual",
            allowed_statuses=("requires_capture",),
            req_payment_intent_id=req.payment_intent_id,
            challenge_message=(
                f"Session requires {amount_cents} {currency} cents stake. "
                "Retry with Authorization: Payment <spt_token> "
                "or POST to /payments/create-intent first."
            ),
            challenge_resource=f"emporia:session:{session_id}",
        )
        if isinstance(result, JSONResponse):
            return result
        payment_ref = result
        req.payment_intent_id = payment_ref.reference
        stake_paid = True

        # N4 review R1: record_payment() raises ValueError on a PaymentIntent id already
        # recorded for a different payment (different agent/session/amount) — a replay
        # attempt, not an internal error. Must surface as a rejected join, not a 500.
        try:
            rs.record_payment(
                payment_intent_id=req.payment_intent_id,
                agent_id=req.agent_id,
                amount_cents=amount_cents,
                payment_type="session_stake",
                session_id=session_id,
                currency=currency,
                rail=payment_ref.rail,
            )
        except ValueError as e:
            raise HTTPException(402, str(e))

    participants = s["participants"] + [req.agent_id]
    rs.add_participant(session_id, req.agent_id, stake_paid=stake_paid)

    if len(participants) >= module.MIN_PARTICIPANTS and purchase:
        # P95: the seller is known now, so the buyer can fund a hold that pays
        # them directly. Nothing can be played until it is.
        rs.save_session(
            session_id=session_id,
            module_type=s["module_type"],
            config=s["config"],
            payment_rules=payment_rules,
            state=SessionState.from_dict(s["state"]),
            current_agent=s["current_agent"],
            step_number=s["step_number"],
            status=AWAITING_FUNDING,
            participants=participants,
            created_at=s["created_at"],
            updated_at=datetime.now(UTC).isoformat(),
        )
        await rs.broadcast_to_agent(participants[0], {
            "type": "funding_required",
            "session_id": session_id,
            "module_type": s["module_type"],
            "seller_id": req.agent_id,
            "amount_cents": amount_cents,
        })
    elif len(participants) >= module.MIN_PARTICIPANTS:
        await _start(session_id, s, participants, payment_rules)
    else:
        rs.save_session(
            session_id=session_id,
            module_type=s["module_type"],
            config=s["config"],
            payment_rules=payment_rules,
            state=SessionState.from_dict(s["state"]),
            current_agent=s["current_agent"],
            step_number=s["step_number"],
            status="waiting",
            participants=participants,
            created_at=s["created_at"],
            updated_at=datetime.now(UTC).isoformat(),
        )
        await rs.broadcast_to_session(session_id, {
            "type": "participant_joined",
            "agent_id": req.agent_id,
        })

    return rs.load_session(session_id)
@router.post("/sessions/{session_id}/action")
async def submit_action(session_id: str, req: rs.SubmitActionRequest):
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != "active":
        raise HTTPException(400, f"Session not active: {s['status']}")
    if req.agent_id != s["current_agent"]:
        raise HTTPException(400, "Not your turn")
    if req.agent_id not in s["participants"]:
        raise HTTPException(400, "Not a participant")
    # P21-5 (B5): peer_text_rationale/payload used to be bounded by hand here
    # (payload's check also covered DisputeDeliveryRequest.reason, which flows
    # in as payload={"reason": ...}). _assert_payload_safe_counted below scans
    # full_payload, whose own top-level keys are exactly "payload" and
    # "peer_text_rationale" (see signed_action_payload) — same bound, for free.

    from emporia.wire import signed_action_payload
    full_payload = signed_action_payload(
        req.agent_id, req.action_type, req.payload, req.peer_text_rationale,
        session_id, s["step_number"],
    )

    # 1. Guardrails (regex firewall + optional NVIDIA NIM semantic check)
    try:
        await rs._assert_payload_safe_counted(full_payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    # 2. Ed25519 signature verify — mandatory. The signature binds the action to
    # this exact session_id + step_number, so a captured signature can't be
    # replayed against a different session or an earlier/later turn.
    if not req.signature:
        rs.SAFETY_STATS["unsigned_actions_rejected"] += 1
        rs.logger.warning("unsigned_action_rejected", extra={"reason": "missing_signature"})
        raise HTTPException(401, "Signature required for session actions")
    if not rs.AGENT_REGISTRY.verify_signature(req.agent_id, full_payload, req.signature):
        rs.SAFETY_STATS["unsigned_actions_rejected"] += 1
        rs.logger.warning("unsigned_action_rejected", extra={"reason": "invalid_signature"})
        raise HTTPException(403, "Invalid signature")
    # Rate-limit after signature verification, not before — keying on req.agent_id
    # pre-signature would let a forged, unsigned request burn any agent's rate-limit
    # budget with no credential at all.
    rs._enforce_agent_rate_limit(req.agent_id)

    # 3. Stripe payment gate handled at join; per-turn PoR gate next

    # 4. Proof-of-Reasoning density + bot fingerprint
    try:
        rs.validate_proof_of_reasoning(full_payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    # 5. Audit log
    log_private_event(session_id, req.agent_id, "action", full_payload)

    # 6. Module dispatch
    module = get_interaction_module(s["module_type"])
    state = SessionState.from_dict(s["state"])
    action = SessionAction(
        agent_id=req.agent_id,
        action_type=req.action_type,
        payload=req.payload,
    )
    valid, err = module.validate_action(state, action)
    if not valid:
        raise HTTPException(400, err)

    result = module.apply_action(state, action)
    if not result.success:
        raise HTTPException(400, result.error or "Action failed")

    new_state = result.new_state
    is_over, outcome = module.is_terminal(new_state)
    new_status = "completed" if is_over else "active"

    rs.save_session(
        session_id=session_id,
        module_type=s["module_type"],
        config=s["config"],
        payment_rules=s["payment_rules"],
        state=new_state,
        current_agent=new_state.current_agent,
        step_number=s["step_number"] + 1,
        status=new_status,
        participants=s["participants"],
        created_at=s["created_at"],
        updated_at=datetime.now(UTC).isoformat(),
    )
    rs.record_action(session_id, req.agent_id, req.action_type, req.payload, {
        "success": result.success,
        "artifacts": result.artifacts,
        "new_state": result.new_state.data if result.new_state else None,
    })

    # Public audit receipt
    if req.signature:
        log_public_receipt(session_id, req.agent_id, req.action_type, req.payload, req.signature)

    action_event: dict[str, Any] = {
        "type": "action_result",
        "session_id": session_id,
        "agent_id": req.agent_id,
        "action_type": req.action_type,
        "payload": req.payload,
        "success": True,
        "artifacts": result.artifacts,
        "new_state": result.new_state.data if result.new_state else None,
    }
    await rs.broadcast_to_session(session_id, action_event)
    if rs._session_visibility(s) == "public":
        await rs.broadcast_global(action_event)

    settlement_record: dict[str, Any] | None = None
    if is_over:
        winner_id = outcome.get("winner") if isinstance(outcome, dict) else None
        outcome_type = outcome.get("outcome_type", "won") if isinstance(outcome, dict) else "won"
        payments = rs.get_session_payments(session_id)
        pi_ids = [p["payment_intent_id"] for p in payments]
        total_cents = sum(p["amount_cents"] for p in payments)

        module_purchase = (
            module.STAKE_MODEL == STAKE_PURCHASE and not _is_bounty(s)
            and bool(_stake(s["payment_rules"])[0])
        )
        if module_purchase and winner_id and winner_id != s["participants"][1]:
            # A purchase pays only its seller. Any other winner is the buyer
            # getting their money back, whatever the module called it.
            outcome_type = "refund"
        if outcome_type in ("refund", "draw"):
            # Dispute, timeout, or a draw: cancel all authorized holds — no one
            # is charged. A draw used to reach neither branch (no winner, not a
            # "refund"), so it wrote no settlement row: holds sat until they
            # expired and a bounty on it stayed `running` forever.
            from emporia.payments import cancel_payment_hold
            cancel_results = []
            for pi_id in pi_ids:
                if pi_id:
                    try:
                        cancel_results.append(await cancel_payment_hold(pi_id))
                    except Exception as e:
                        cancel_results.append({"payment_intent_id": pi_id, "error": str(e)})
            settlement_record = {
                "session_id": session_id,
                "winner_id": winner_id,
                "outcome_type": outcome_type,
                "status": "refunded",
                "cancel_results": cancel_results,
                "total_stake_cents": total_cents,
                "platform_fee_cents": 0,
                "winner_payout_cents": 0,
                "payment_intent_ids": pi_ids,
                "transfer_id": None,
                "transfer_status": "refunded",
            }
            rs.record_settlement(settlement_record)
        elif winner_id:
            if total_cents and module_purchase:
                # P95: destination charges — capture is the settlement.
                from emporia.payments import settle_destination_charges
                settlement_record = await settle_destination_charges(
                    session_id, winner_id, total_cents, pi_ids,
                )
            elif total_cents:
                # Paid session: capture all escrowed holds, transfer to winner.
                from emporia.payments import settle
                winner_stripe_account = rs.AGENT_REGISTRY.get_stripe_account(winner_id)
                settlement_record = await settle(
                    session_id, winner_id, total_cents, pi_ids,
                    winner_stripe_account=winner_stripe_account,
                    winner_stripe_account_status=rs.AGENT_REGISTRY.get_stripe_account_status(winner_id),
                    winner_stripe_account_mode=rs.AGENT_REGISTRY.get_stripe_account_mode(winner_id),
                )
            else:
                # Free session: record outcome with zero financials.
                settlement_record = {
                    "session_id": session_id,
                    "winner_id": winner_id,
                    "outcome_type": outcome_type,
                    "total_stake_cents": 0,
                    "platform_fee_cents": 0,
                    "winner_payout_cents": 0,
                    "platform_fee_bps": rs.OPERATOR_FEE_BPS,
                    "payment_intent_ids": [],
                    "capture_results": [],
                    "transfer_id": None,
                    "transfer_status": "no_payment",
                    "status": "settled",
                }
            rs.record_settlement(settlement_record)

        terminal_event: dict[str, Any] = {
            "type": "session_completed",
            "session_id": session_id,
            "outcome": outcome,
            "settlement": settlement_record,
        }
        await rs.broadcast_to_session(session_id, terminal_event)
        if rs._session_visibility(s) == "public":
            await rs.broadcast_global(terminal_event)
        # P92: and to each participant's own channel. The session channel only
        # reaches an agent that subscribed to this session's socket; an agent
        # listening on its own channel (the Hermes platform adapter, or a later
        # inbox poll) never heard that its game, or its bounty contest, ended.
        for p in s["participants"]:
            await rs.broadcast_to_agent(p, terminal_event)
    elif new_state.current_agent and new_state.current_agent != req.agent_id:
        # P92: nothing told an agent it was their move unless it was watching
        # the session socket, so an agent driven by its own channel waited on a
        # turn that had already come.
        await rs.broadcast_to_agent(new_state.current_agent, {
            "type": "your_turn",
            "session_id": session_id,
            "module_type": s["module_type"],
            "step_number": s["step_number"] + 1,
            "last_action": {"agent_id": req.agent_id, "action_type": req.action_type},
        })

    return {
        "success": True,
        "session_id": session_id,
        "step_number": s["step_number"] + 1,
        "is_terminal": is_over,
        "outcome": outcome if is_over else None,
        "settlement": settlement_record,
        "artifacts": result.artifacts,
    }
@router.get("/sessions/{session_id}/state")
async def get_session_state(session_id: str, request: Request):
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not rs._session_viewer_allowed(s, rs._dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    return s
@router.post("/sessions/{session_id}/confirm-delivery")
async def confirm_delivery(session_id: str, req: rs.ConfirmDeliveryRequest, request: Request):
    """Buyer confirms service delivery. Triggers settlement: seller receives payment.

    Shorthand for submitting action_type='confirm' to an emporia:service:v1 session.
    Works on any module that accepts a 'confirm' action from the non-current participant.
    """
    from emporia.wire import DEFAULT_CONFIRM_RATIONALE
    return await submit_action(
        session_id=session_id,
        req=rs.SubmitActionRequest(
            agent_id=req.agent_id,
            action_type="confirm",
            payload={},
            peer_text_rationale=req.rationale or DEFAULT_CONFIRM_RATIONALE,
            signature=req.signature,
        ),
    )
@router.post("/sessions/{session_id}/dispute-delivery")
async def dispute_delivery(session_id: str, req: rs.DisputeDeliveryRequest, request: Request):
    """Buyer disputes delivery. Triggers refund: all escrowed holds released.

    Shorthand for submitting action_type='dispute' to an emporia:service:v1 session.
    """
    from emporia.wire import default_dispute_rationale
    return await submit_action(
        session_id=session_id,
        req=rs.SubmitActionRequest(
            agent_id=req.agent_id,
            action_type="dispute",
            payload={"reason": req.reason},
            peer_text_rationale=req.rationale or default_dispute_rationale(req.reason),
            signature=req.signature,
        ),
    )
@router.post("/sessions/{session_id}/abandon")
async def abandon_session(session_id: str, request: Request, req: dict = None):
    """Mark a session abandoned and release all escrowed stake holds.

    Call this when a session cannot complete (both agents disconnected, timeout,
    mutual agreement). The relay cancels all authorized PaymentIntent holds so
    both agents are fully refunded. Already-captured PIs cannot be released here —
    use /payments/refund for those (handled via arbitrate_and_refund).
    """
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    caller = rs._dashboard_agent_id(request)
    owner = rs._relay_operator_id()
    if not caller or (caller not in s["participants"] and caller != owner):
        raise HTTPException(403, "Only a participant or the relay operator may abandon this session")
    rs._enforce_agent_rate_limit(caller)
    # P5-1: was `== "complete"`, a status never actually written (the real
    # terminal values are "completed" and "abandoned") — this guard never
    # fired, so a completed session could be "abandoned" a second time,
    # re-running cancel_payment_hold() over payments already captured.
    # Re-abandoning an already-abandoned session is refused for the same
    # reason: cancelling the same holds twice is the exact double-action
    # this guard exists to prevent, not a distinct case to allow.
    if s["status"] in rs._SESSION_TERMINAL_STATUSES:
        raise HTTPException(400, f"Session already {s['status']} — cannot abandon")

    # Cancel all authorized holds for this session
    payments = rs.get_session_payments(session_id)
    from emporia.payments import cancel_payment_hold
    cancel_results = []
    for p in payments:
        pi_id = p.get("payment_intent_id")
        if pi_id:
            try:
                result = await cancel_payment_hold(pi_id)
                cancel_results.append(result)
            except Exception as e:
                cancel_results.append({"payment_intent_id": pi_id, "error": str(e)})

    with rs.get_db() as db:
        db.execute(
            "UPDATE sessions SET status='abandoned', updated_at=? WHERE session_id=?",
            (datetime.now(UTC).isoformat(), session_id),
        )

    await rs.broadcast_to_session(session_id, {
        "type": "session_abandoned",
        "session_id": session_id,
        "holds_released": len(cancel_results),
        "cancel_results": cancel_results,
    })

    return {
        "status": "abandoned",
        "session_id": session_id,
        "holds_released": cancel_results,
        "message": "Authorized stakes released. Agents will see refunds within 5–7 days.",
    }
