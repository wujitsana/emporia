"""Session routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from emporia import relay_server as rs
from emporia.module_sdk import (
    ENGINE_GAME_REFUSAL,
    STAKE_PURCHASE,
    STAKE_WAGER,
    MODULE_REGISTRY,
    SessionAction,
    SessionState,
    engine_game_stakes_allowed,
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
    ok, why = module.validate_config(req.config or {})
    if not ok:
        raise HTTPException(400, why)
    if req.module_type == SERVICE_MODULE:
        from emporia.modules.service import MAX_DEADLINE_HOURS
        try:
            hours = float((req.config or {}).get("deadline_hours", 0) or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "deadline_hours must be a number")
        if hours > MAX_DEADLINE_HOURS:
            raise HTTPException(400, (
                f"deadline_hours is at most {MAX_DEADLINE_HOURS}: delivery and the buyer's "
                f"confirmation must finish while the payment is still held (7 days)."
            ))
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
    if amount_cents and module.ENGINE_DECIDABLE and not engine_game_stakes_allowed():
        raise HTTPException(400, ENGINE_GAME_REFUSAL.format(mt=req.module_type))
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
# P111: held for the seconds an escrow authorize takes on chain, so a second
# fund request cannot place a second hold.
FUNDING = "funding"
SERVICE_MODULE = "emporia:service:v1"


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
    if not module.TURN_ORDER:
        # P136: a simultaneous game waits on everyone from the first move.
        for p in state.data.get("pending") or participants:
            await rs.broadcast_to_agent(p, {"type": "your_turn", "session_id": session_id,
                                            "module_type": s["module_type"], "step_number": 0})


ESCROW_HEADER = "PAYMENT-ESCROW"


def _escrow_hold_for(payment: dict[str, Any]):
    """(config, signer, PaymentInfo, hold) for an x402 escrow stake row."""
    from emporia import escrow, escrow_chain
    from emporia.operator_signer import load_operator_signer

    hold = rs.get_escrow_hold(payment["payment_intent_id"])
    if hold is None:
        raise RuntimeError(f"no escrow hold recorded for {payment['payment_intent_id']}")
    cfg = escrow_chain.load_config()
    if cfg is None or cfg.network != hold["network"]:
        raise RuntimeError(f"escrow for {hold['network']} is not configured on this relay")
    return cfg, load_operator_signer(cfg.network), escrow.PaymentInfo.from_json(
        hold["payment_info"]), hold


async def _chain_outcome(cfg, info) -> str | None:
    """What the escrow contract says happened to a hold (P111): "held" (still
    capturable), "captured", "gone" (voided or reclaimed), or None if the chain
    cannot be read. Used whenever a capture or void call fails — a missing
    receipt is not a failed transaction, and the contract is the record."""
    from emporia import escrow_chain
    try:
        st = await escrow_chain.payment_state(cfg, info)
    except Exception:
        return None
    if st["capturable"] > 0:
        return "held"
    return "captured" if st["refundable"] > 0 else "gone"


# What a release returned when the money is back with the payer.
_RELEASED = frozenset({"canceled", "voided", "already voided", "already reclaimed"})


async def _release_hold(payment: dict[str, Any]) -> dict[str, Any]:
    """Give a stake back, on whichever rail holds it, and record it on the
    payment row. P130: the row said `confirmed` after every release — abandon,
    dispute, draw, the idle sweep — so the ledger showed stakes as taken that
    Stripe had cancelled or the contract had voided."""
    result = await _release_on_rail(payment)
    if result.get("status") in _RELEASED and payment.get("payment_intent_id"):
        rs.mark_payment_released(payment["payment_intent_id"])
    return result


async def _release_on_rail(payment: dict[str, Any]) -> dict[str, Any]:
    """Cancel a Stripe hold, or void an escrow hold to its payer (P99). Errors
    are reported, not raised — both kinds of hold also lapse back to the payer
    on their own."""
    from emporia.payments import RAIL_X402, cancel_payment_hold

    ref = payment.get("payment_intent_id")
    try:
        if payment.get("rail") == RAIL_X402:
            from emporia import escrow_chain
            cfg, signer, info, hold = _escrow_hold_for(payment)
            if hold["state"] != "authorized":
                return {"payment_intent_id": ref, "status": f"already {hold['state']}"}
            try:
                tx = await escrow_chain.void(cfg, signer, info)
            except Exception as e:
                # P111: read the chain before calling it failed, and leave a
                # still-held hold for the reconciler to void, not to expire.
                outcome = await _chain_outcome(cfg, info)
                if outcome == "gone":
                    tx = getattr(e, "tx_hash", None)
                else:
                    rs.update_escrow_hold(ref, state="void_pending")
                    return {"payment_intent_id": ref, "error": str(e), "status": "void_pending"}
            rs.update_escrow_hold(ref, state="voided", **({"void_tx": tx} if tx else {}))
            return {"payment_intent_id": ref, "status": "voided", "transaction": tx}
        return await cancel_payment_hold(ref)
    except Exception as e:
        return {"payment_intent_id": ref, "error": str(e)}


async def _settle_escrow_purchase(session_id: str, seller: str, total_cents: int,
                                  payments: list[dict[str, Any]]) -> dict[str, Any]:
    """Capture an escrow hold to the seller (P99): the contract pays the seller
    and the fee receiver in the same transaction, so there is no transfer and
    no platform balance in between. A failed capture pays nobody and says so."""
    from emporia import escrow_chain
    from emporia.payments import OPERATOR_FEE_BPS, RAIL_X402, platform_fee

    from emporia.escrow import fee_amount

    captures, capture_tx, failed = [], None, False
    fee_atomic = payout_atomic = 0
    for p in payments:
        ref = p["payment_intent_id"]
        try:
            cfg, signer, info, hold = _escrow_hold_for(p)
        except Exception as e:
            failed = True
            captures.append({"payment_intent_id": ref, "status": f"capture_failed: {e}"})
            continue
        # P129: the split, to the atomic unit, with the same fee_amount() that
        # capture() passes on chain. Fixed at authorization, so it is also what
        # a capture the reconciler retries later will pay.
        amount = int(hold["amount_atomic"])
        fee_atomic += fee_amount(info, amount)
        payout_atomic += amount - fee_amount(info, amount)
        try:
            tx = await escrow_chain.capture(cfg, signer, info, amount)
        except Exception as e:
            # P111: the chain decides. A capture whose receipt timed out may
            # have landed; one that truly failed is left `capture_pending` for
            # reconcile_escrow_holds() to retry until the hold expires — before
            # this, one RPC hiccup at confirm meant the seller was never paid.
            if await _chain_outcome(cfg, info) == "captured":
                tx = getattr(e, "tx_hash", None)
            else:
                rs.update_escrow_hold(ref, state="capture_pending")
                failed = True
                captures.append({"payment_intent_id": ref, "status": f"capture_failed: {e}"})
                continue
        capture_tx = tx or capture_tx
        rs.update_escrow_hold(ref, state="captured", **({"capture_tx": tx} if tx else {}))
        captures.append({"payment_intent_id": ref, "status": "captured", "transaction": tx})
    fee = platform_fee(total_cents)
    return {
        "session_id": session_id, "winner_id": seller, "total_stake_cents": total_cents,
        "platform_fee_cents": fee, "winner_payout_cents": total_cents - fee,
        "platform_fee_bps": OPERATOR_FEE_BPS,
        "platform_fee_atomic": fee_atomic, "winner_payout_atomic": payout_atomic,
        "payment_intent_ids": [p["payment_intent_id"] for p in payments],
        "capture_results": captures, "transfer_id": None if failed else capture_tx,
        # Captured to the seller by the contract itself; nothing is owed, so
        # the (Stripe-only) payout reconciler never sees it.
        "transfer_status": "capture_failed" if failed else "escrow_captured",
        "status": "settled", "rail": RAIL_X402,
    }


async def reconcile_escrow_holds(limit: int = 100) -> dict[str, int]:
    """Finish what settlement could not (P111): retry a failed capture or void
    while the hold can still be acted on, and record what the chain says
    happened. Run by the relay's payout reconcile loop.

    `capture_pending` → captured (retry, or found already captured: the
    settlement row is corrected), or `expired` once the authorization window has
    passed (the payer may then reclaim; the seller's settlement stays failed).
    `void_pending` → voided, or `expired` (the payer reclaims it themselves).
    """
    import time

    from emporia import escrow, escrow_chain
    from emporia.operator_signer import load_operator_signer

    counts = {"escrow_captured": 0, "escrow_voided": 0, "escrow_expired": 0,
              "escrow_retry_later": 0}
    cfg = escrow_chain.load_config()
    if cfg is None:
        return counts
    holds = [h for h in rs.escrow_holds_in_states(("capture_pending", "void_pending"), limit)
             if h["network"] == cfg.network]
    if not holds:
        return counts
    signer = load_operator_signer(cfg.network)
    for hold in holds:
        ref, info = hold["hold_hash"], escrow.PaymentInfo.from_json(hold["payment_info"])
        outcome = await _chain_outcome(cfg, info)
        if outcome is None:
            counts["escrow_retry_later"] += 1
            continue
        capturing = hold["state"] == "capture_pending"
        if capturing and outcome == "captured":
            rs.update_escrow_hold(ref, state="captured")
            rs.mark_escrow_settlement_captured(hold["session_id"], None)
            counts["escrow_captured"] += 1
            continue
        if outcome == "gone" or (not capturing and outcome == "captured"):
            rs.update_escrow_hold(ref, state="voided" if outcome == "gone" else "captured")
            if outcome == "gone" and not capturing:
                rs.mark_payment_released(ref)
            counts["escrow_voided" if outcome == "gone" else "escrow_captured"] += 1
            continue
        if time.time() >= info.authorization_expiry:
            rs.update_escrow_hold(ref, state="expired")
            counts["escrow_expired"] += 1
            continue
        try:
            if capturing:
                tx = await escrow_chain.capture(cfg, signer, info, int(hold["amount_atomic"]))
                rs.update_escrow_hold(ref, state="captured", capture_tx=tx)
                rs.mark_escrow_settlement_captured(hold["session_id"], tx)
                counts["escrow_captured"] += 1
            else:
                tx = await escrow_chain.void(cfg, signer, info)
                rs.update_escrow_hold(ref, state="voided", void_tx=tx)
                rs.mark_payment_released(ref)
                counts["escrow_voided"] += 1
        except Exception:
            rs.logger.warning("escrow reconcile: %s for %s will be retried",
                              "capture" if capturing else "void", ref, exc_info=True)
            counts["escrow_retry_later"] += 1
    return counts


def _escrow_rail(seller: str):
    """(config, operator signer, seller wallet) when an x402 escrow hold can pay
    this seller, else None. Every part must be present: a configured escrow, an
    operator key that loads for its network, and an EVM payout wallet the seller
    registered (POST /agents/{id}/x402-wallet)."""
    from emporia import escrow_chain
    from emporia.operator_signer import OperatorSignerError, load_operator_signer

    cfg = escrow_chain.load_config()
    if cfg is None:
        return None
    wallet = rs.AGENT_REGISTRY.get_x402_pay_to(seller, "evm")
    if not wallet:
        return None
    try:
        signer = load_operator_signer(cfg.network)
    except OperatorSignerError:
        rs.logger.warning("escrow configured but the operator signer will not load",
                          exc_info=True)
        return None
    return cfg, signer, wallet


def _escrow_offer(rail, amount_cents: int) -> dict[str, Any]:
    """The x402 escrow half of a 402: the PaymentInfo the payer completes and
    signs. `payer` is left zero — the payer fills in their own address, and the
    signature does not cover it (the nonce is payer-agnostic) — and every other
    field is what check_submitted() will require back."""
    import time

    from emporia import escrow
    from emporia.payments import OPERATOR_FEE_BPS, _x402_amount_atomic

    cfg, signer, wallet = rail
    info = escrow.build_payment_info(
        operator=signer.address, payer=escrow.ZERO_ADDRESS, receiver=wallet, token=cfg.token,
        amount=int(_x402_amount_atomic(amount_cents)), fee_bps=OPERATOR_FEE_BPS,
        fee_receiver=cfg.fee_receiver, now=int(time.time()),
        pre_approval_window=escrow.PRE_APPROVAL_WINDOW,
        authorization_window=escrow.AUTHORIZATION_WINDOW, refund_window=escrow.REFUND_WINDOW,
    )
    return {
        "scheme": "emporia-escrow-v1", "network": cfg.network, "chain_id": cfg.chain_id,
        "escrow": cfg.contract, "collector": cfg.collector, "token": cfg.token,
        "token_name": cfg.token_name, "token_version": cfg.token_version,
        "payment_info": info.to_json(), "header": ESCROW_HEADER,
        "sign": ("Set payment_info.payer to your address, sign the ERC-3009 "
                 "ReceiveWithAuthorization it implies, and retry with header "
                 f"{ESCROW_HEADER}: base64(JSON {{payment_info, signature}})."),
    }


async def _fund_with_escrow(request: Request, s: dict[str, Any], session_id: str,
                            caller: str, rail, amount_cents: int):
    """Authorize the payer's signed hold on chain, then start the session."""
    import base64
    import time

    from emporia import escrow, escrow_chain
    from emporia.payments import OPERATOR_FEE_BPS, RAIL_X402, _x402_amount_atomic

    cfg, signer, wallet = rail
    try:
        submitted = json.loads(base64.b64decode(request.headers[ESCROW_HEADER]))
        info = escrow.PaymentInfo.from_json(submitted["payment_info"])
        signature = bytes.fromhex(str(submitted["signature"]).removeprefix("0x"))
    except Exception:
        raise HTTPException(400, f"{ESCROW_HEADER} must be base64(JSON {{payment_info, signature}}).")
    amount = int(_x402_amount_atomic(amount_cents))
    try:
        escrow.check_submitted(info, operator=signer.address, receiver=wallet, token=cfg.token,
                               amount=amount, fee_bps=OPERATOR_FEE_BPS,
                               fee_receiver=cfg.fee_receiver, now=int(time.time()))
        signed_by = escrow.recover_payer(
            info, signature, chain_id=cfg.chain_id, escrow=cfg.contract,
            collector=cfg.collector, token_name=cfg.token_name, token_version=cfg.token_version)
    except escrow.EscrowError as e:
        raise HTTPException(402, f"Escrow hold refused: {e}")
    except Exception:
        raise HTTPException(402, "Escrow hold refused: the signature does not verify.")
    if signed_by.lower() != info.payer.lower():
        raise HTTPException(402, "Escrow hold refused: the signature is not the payer's.")

    hold_hash = "0x" + escrow.payment_info_hash(info, chain_id=cfg.chain_id,
                                                escrow=cfg.contract).hex()
    if rs.get_escrow_hold(hold_hash):
        raise HTTPException(409, "This hold has already been submitted.")
    # P111: claim the session before any chain call. Two concurrent funds both
    # passed the status check above and could each place a hold — the buyer's
    # money held twice for one purchase. One UPDATE decides; the loser is told.
    if not rs.claim_session_status(session_id, AWAITING_FUNDING, FUNDING):
        raise HTTPException(409, "This purchase is already being funded.")
    try:
        try:
            tx = await escrow_chain.authorize(cfg, signer, info, amount, signature)
        except escrow_chain.EscrowChainError as e:
            # A missing receipt is not a failed authorize: ask the contract.
            if (await _chain_outcome(cfg, info)) != "held":
                rs._record_x402_settlement(request, {"success": False, "errorReason": str(e)},
                                           network=cfg.network, payer=info.payer)
                raise HTTPException(402, f"Escrow hold could not be placed: {e}")
            tx = e.tx_hash or ""
        state = await escrow_chain.payment_state(cfg, info)
        if state["capturable"] != amount:
            # Void it now rather than leave the buyer's money held for a week.
            try:
                await escrow_chain.void(cfg, signer, info)
                released = "It was voided back to the payer."
            except Exception:
                released = "Voiding it failed; the payer can reclaim it after it expires."
            raise HTTPException(502, (
                f"The hold was submitted ({tx}) but holds {state['capturable']}, not {amount}. "
                f"Nothing was started. {released}"
            ))
    except BaseException:
        rs.claim_session_status(session_id, FUNDING, AWAITING_FUNDING)
        raise
    rs._record_x402_settlement(request, {"success": True, "transaction": tx},
                               network=cfg.network, payer=info.payer)
    rs.record_escrow_hold({
        "hold_hash": hold_hash, "session_id": session_id, "payer_agent_id": caller,
        "payer_address": info.payer, "receiver_agent_id": s["participants"][1],
        "receiver_address": wallet, "network": cfg.network, "amount_atomic": amount,
        "payment_info": info.to_json(), "authorize_tx": tx,
    })
    rs.record_payment(
        payment_intent_id=hold_hash, agent_id=caller, amount_cents=amount_cents,
        payment_type="session_stake", session_id=session_id, currency="usd", rail=RAIL_X402,
    )
    await _start(session_id, s, s["participants"], s["payment_rules"])
    return rs.load_session(session_id)


@router.post("/sessions/{session_id}/fund")
async def fund_session(session_id: str, request: Request):
    """The buyer funds a purchase, as a hold that pays the seller directly.

    Two rails, both non-custodial, chosen by the credential sent:

    * **Stripe (P95)** — `Authorization: Payment <spt>`. A destination charge:
      the hold names the seller's connected account and the operator fee, so on
      capture Stripe pays the seller and the platform its fee directly.
    * **x402 escrow (P99)** — `PAYMENT-ESCROW`. A Commerce Payments hold on
      chain: the payer signs a PaymentInfo naming the seller's wallet, the
      operator authorizes it, and capture pays the seller less the fee, or void
      returns it. The relay can do nothing else with it.

    No credential: a 402 offering every rail this seller can be paid on. Both
    need the seller known, which is why funding waits for them to join.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] == FUNDING:
        raise HTTPException(409, "This purchase is already being funded.")
    if s["status"] != AWAITING_FUNDING:
        raise HTTPException(409, f"Session is {s['status']}, not awaiting funding.")
    buyer, seller = s["participants"][0], s["participants"][1]
    if caller != buyer:
        raise HTTPException(403, "Only the buyer funds a purchase.")
    rs._enforce_agent_rate_limit(caller)

    amount_cents, currency = _stake(s["payment_rules"])
    escrow_rail = _escrow_rail(seller) if currency == "usd" else None
    account = rs.AGENT_REGISTRY.get_stripe_account(seller)
    stripe_ok = bool(
        account and rs.AGENT_REGISTRY.get_stripe_account_status(seller) == "active"
        and not rs._stripe_account_mode_mismatch(rs.AGENT_REGISTRY.get_stripe_account_mode(seller))
    )
    rs._assert_agent_budget(caller, amount_cents)

    if request.headers.get(ESCROW_HEADER):
        if not escrow_rail:
            raise HTTPException(409, (
                f"An x402 escrow hold cannot pay {seller}: escrow is not configured on this "
                f"relay, or they have no EVM payout wallet registered."
            ))
        return await _fund_with_escrow(request, s, session_id, caller, escrow_rail, amount_cents)

    if not stripe_ok and not escrow_rail:
        raise HTTPException(409, (
            f"{seller} cannot receive payments yet: no active Stripe account and no x402 "
            f"payout wallet this relay can pay. A purchase pays the seller directly, so it "
            f"waits for one — they can run start_stripe_onboarding or register a wallet, or "
            f"abandon the session."
        ))

    if not stripe_ok:
        offer = _escrow_offer(escrow_rail, amount_cents)
        return JSONResponse(status_code=402, content={
            "error": "payment_required", "amount_cents": amount_cents,
            "resource": f"emporia:session:{session_id}",
            "message": f"Fund this purchase with an x402 escrow hold paid to {seller}.",
            "escrow": offer,
        })

    from emporia.payments import platform_fee
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
            f"{seller} when you confirm delivery. Retry with Authorization: Payment <spt_token>"
            + (f", or with an x402 escrow hold ({ESCROW_HEADER})." if escrow_rail else ".")
        ),
        challenge_resource=f"emporia:session:{session_id}",
        destination_account=account,
        application_fee_cents=platform_fee(amount_cents),
    )
    if isinstance(paid, JSONResponse):
        if escrow_rail:
            # Offer the escrow alongside Stripe's challenge, same 402.
            body = json.loads(paid.body)
            body["escrow"] = _escrow_offer(escrow_rail, amount_cents)
            headers = {k: v for k, v in paid.headers.items() if k.lower() != "content-length"}
            return JSONResponse(status_code=402, content=body, headers=headers)
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
    # P136: a join checks room left, not the minimum — the minimum is when the
    # game starts (ready_to_start). Checking it here refused the second of three
    # players, so no game of more than two could ever fill.
    if len(s["participants"]) + 1 > module.MAX_PARTICIPANTS:
        raise HTTPException(400, f"Max {module.MAX_PARTICIPANTS} participants — this game is full")

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

    if module.ready_to_start(participants, s["config"]) and purchase:
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
    elif module.ready_to_start(participants, s["config"]):
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
    # P136: a simultaneous-move module (TURN_ORDER False) decides who may act.
    if get_interaction_module(s["module_type"]).TURN_ORDER and req.agent_id != s["current_agent"]:
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
    # P112: the deadline sweep can end a service session too. Whoever moves it
    # from active to completed first settles it; the other is told.
    if is_over and not rs.claim_session_status(session_id, "active", "completed"):
        raise HTTPException(409, "This session has already ended.")

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
    block_hash = (log_public_receipt(session_id, req.agent_id, req.action_type, req.payload,
                                     req.signature) if req.signature else None)
    receipt = action_receipt(session_id, s["step_number"] + 1, req.agent_id, req.action_type,
                             req.payload, block_hash)

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
        settlement_record = await _finish_session(session_id, s, module, outcome)
    elif not module.TURN_ORDER:
        # P136: everyone may move; when a phase opens, tell everyone it waits on.
        before = (s.get("state") or {}).get("data") or {}
        if new_state.data.get("phase") != before.get("phase"):
            for agent in new_state.data.get("pending") or []:
                await rs.broadcast_to_agent(agent, {
                    "type": "your_turn", "session_id": session_id,
                    "module_type": s["module_type"], "step_number": s["step_number"] + 1,
                    "last_action": {"agent_id": req.agent_id, "action_type": req.action_type},
                })
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
        "receipt": receipt,
    }
async def sweep_service_deadlines(limit: int = 200) -> dict[str, int]:
    """End service sessions whose deadline passed with nobody acting (P112).

    The service module's timeouts (refund if never delivered, auto-confirm for
    the seller once delivered and the buyer's window lapsed) were evaluated only
    inside submit_action. A buyer who went silent after delivery was therefore
    never auto-confirmed: nothing captured the hold, and it lapsed back to them
    with the seller unpaid. Run by the relay's payout reconcile loop.
    """
    counts = {"service_timeouts": 0, "game_timeouts": 0}
    # P131: every module whose clock can end it (TIMED) — a chess game with a
    # move limit too, not only service deadlines.
    for module_type in sorted(MODULE_REGISTRY):
        module = get_interaction_module(module_type)
        if not module.TIMED:
            continue
        key = "service_timeouts" if module_type == SERVICE_MODULE else "game_timeouts"
        for s in rs.active_sessions_of(module_type, limit):
            is_over, outcome = module.is_terminal(SessionState.from_dict(s["state"]))
            if not is_over:
                continue
            # Claimed like a fund, so a racing submit_action and this sweep
            # cannot both settle the same session.
            if not rs.claim_session_status(s["session_id"], "active", "completed"):
                continue
            await _finish_session(s["session_id"], s, module, outcome)
            counts[key] += 1
    return counts


def action_receipt(session_id: str, step_number: int, agent_id: str, action_type: str,
                   payload: dict[str, Any], block_hash: str | None) -> dict[str, Any]:
    """P140: the relay's signed word that it accepted this action, and when.

    Commit-reveal hides moves from the relay, but the relay can still refuse
    or delay a move — a reveal that "never arrived" loses on time. A player
    holding this receipt can prove the relay accepted their commit (or
    reveal) at this time, against the relay's own published key. Verify with
    identity.verify_action_receipt()."""
    import hashlib

    from emporia import identity
    body = {
        "session_id": session_id, "step_number": step_number, "agent_id": agent_id,
        "action_type": action_type,
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "block_hash": block_hash,
        "accepted_at": datetime.now(UTC).isoformat(),
        "relay_public_key": identity.get_relay_public_key_hex(),
    }
    return {**body, "signature": identity.sign_raw_relay(identity.receipt_bytes(body))}


def _idle_hours() -> float:
    try:
        return float(os.environ.get("EMPORIA_SESSION_IDLE_HOURS", "24"))
    except ValueError:
        return 24.0


async def sweep_idle_sessions(limit: int = 200) -> dict[str, int]:
    """Abandon sessions nobody has acted in for EMPORIA_SESSION_IDLE_HOURS
    (default 24; 0 turns it off), returning every stake (P129).

    A chess wager whose player stopped answering (in testing: the agent's model
    budget ran out mid-game) stayed `active` for good, both card holds sitting
    until they lapsed, and the one who could still move could not end it.
    Abandoned, not forfeited: an idle player may be a broken one, and nobody
    is charged for a game that did not finish. Service sessions keep their own
    deadlines (sweep_service_deadlines) and bounty contests their own rules.
    """
    counts = {"idle_abandoned": 0}
    hours = _idle_hours()
    if hours <= 0:
        return counts
    before = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    for s in rs.idle_sessions(before, SERVICE_MODULE, limit):
        if _is_bounty(s):
            continue
        if await _abandon(s, reason=f"no action for {hours:g}h") is not None:
            counts["idle_abandoned"] += 1
    return counts


async def _abandon(s: dict[str, Any], reason: str) -> list[dict[str, Any]] | None:
    """End a session as abandoned and release every hold; None if it changed
    status meanwhile. Claimed, so an abandon racing a settle (or the sweep
    racing a participant) acts once — the route used to check, then write."""
    session_id = s["session_id"]
    if not rs.claim_session_status(session_id, s["status"], "abandoned"):
        return None
    payments = rs.get_session_payments(session_id)
    cancel_results = [await _release_hold(p) for p in payments if p.get("payment_intent_id")]
    event = {
        "type": "session_abandoned",
        "session_id": session_id,
        "reason": reason,
        "holds_released": len(cancel_results),
        "cancel_results": cancel_results,
    }
    await rs.broadcast_to_session(session_id, event)
    # As P92 did for a finished session: the session socket reaches only a
    # subscriber, so an agent on its own channel never heard its game ended.
    # Count only: cancel_results names every player's payment.
    for p in s["participants"]:
        await rs.broadcast_to_agent(p, {k: v for k, v in event.items() if k != "cancel_results"})
    return cancel_results


async def _finish_session(session_id: str, s: dict[str, Any], module, outcome) -> dict[str, Any] | None:
    """Settle a session that just ended and tell everyone (P112: shared by the
    action that ended it and the sweep that ends a timed-out one)."""
    settlement_record: dict[str, Any] | None = None
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
        cancel_results = [await _release_hold(p) for p in payments if p.get("payment_intent_id")]
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
        from emporia.payments import RAIL_X402
        if total_cents and module_purchase and any(p.get("rail") == RAIL_X402 for p in payments):
            # P99: an x402 escrow hold — capture on chain is the settlement.
            settlement_record = await _settle_escrow_purchase(
                session_id, winner_id, total_cents, payments,
            )
        elif total_cents and module_purchase:
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
    return settlement_record


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

    cancel_results = await _abandon(s, reason=f"abandoned by {caller}")
    if cancel_results is None:
        raise HTTPException(409, "The session changed meanwhile — load it again.")

    return {
        "status": "abandoned",
        "session_id": session_id,
        "holds_released": cancel_results,
        "message": "Authorized stakes released. Agents will see refunds within 5–7 days.",
    }
