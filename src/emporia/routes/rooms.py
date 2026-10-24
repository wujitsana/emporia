"""Room routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from emporia import entitlements
from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay

router = APIRouter()


@router.post("/rooms")
async def create_room_endpoint(req: rs.CreateRoomRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P21-5 (B5): name/description bounded automatically — see
    # _require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if req.room_type not in ("public", "private"):
        raise HTTPException(400, "room_type must be 'public' or 'private'")
    if req.gate_type not in ("open", "invite", "paid"):
        raise HTTPException(400, "gate_type must be 'open', 'invite', or 'paid'")
    if req.room_type == "public" and req.gate_type != "open":
        raise HTTPException(400, "Public rooms must use gate_type='open'")
    if req.gate_type == "paid" and req.entry_fee_cents <= 0:
        raise HTTPException(400, "paid rooms require entry_fee_cents > 0")
    if req.access_duration_days is not None and req.access_duration_days <= 0:
        raise HTTPException(400, "access_duration_days must be a positive integer or omitted")
    # P18-4: same rule encrypted DM threads already have. An encrypted room whose
    # creator has no published key is a room in which nothing can ever be sent —
    # every message would fail envelope validation with an error nowhere near the
    # cause. Joiners are checked at join time, not here, since they don't exist yet.
    if req.encrypted:
        rs._require_encryption_keys_published([caller])
    if req.linked_session_id:
        s = rs.load_session(req.linked_session_id)
        if not s:
            raise HTTPException(404, f"Session not found: {req.linked_session_id}")
        # P141: a linked room sets the session's visibility, so only its players
        # (or the operator) may link one.
        if caller not in s["participants"] and caller != rs._relay_operator_id():
            raise HTTPException(403, "Only a player in that session may link a room to it")
    try:
        room = rooms_db.create_room(
            name=req.name,
            creator_id=caller,
            room_type=req.room_type,
            gate_type=req.gate_type,
            entry_fee_cents=req.entry_fee_cents,
            currency=req.currency,
            description=req.description,
            max_members=req.max_members,
            encrypted=req.encrypted,
            linked_session_id=req.linked_session_id,
            access_duration_days=req.access_duration_days,
            db_path=rs.DATABASE_PATH,
        )
        if req.room_type == "public":
            await rs.broadcast_global({
                "type": "room_created",
                "room_id": room.room_id,
                "name": room.name,
                "creator_id": room.creator_id,
            })
        return room.to_dict(viewer_id=caller, db_path=rs.DATABASE_PATH)
    except Exception as e:
        raise HTTPException(422, str(e))
@router.get("/rooms")
async def list_rooms_endpoint(
    request: Request,
    room_type: str | None = None,
    limit: int = 50,
):
    viewer_id = rs._dashboard_agent_id(request)
    room_list = rooms_db.list_rooms(
        viewer_id=viewer_id,
        room_type=room_type,
        limit=rs._clamp_limit(limit),
        db_path=rs.DATABASE_PATH,
    )
    return {
        "rooms": [r.to_dict(viewer_id=viewer_id, db_path=rs.DATABASE_PATH) for r in room_list],
        "count": len(room_list),
    }
@router.get("/rooms/{room_id}")
async def get_room_endpoint(room_id: str, request: Request):
    viewer_id = rs._dashboard_agent_id(request)
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    return room.to_dict(viewer_id=viewer_id, db_path=rs.DATABASE_PATH)
@router.post("/rooms/{room_id}/join")
async def join_room(room_id: str, req: rs.JoinRoomRequest, request: Request):
    identity_verified = rs._require_caller_unless_mpp_payment(req.agent_id, request)
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_registered(req.agent_id)
    # N12-5 (R5): identity_verified is False on the MPP-payment branch, where agent_id is
    # an unproven claim — don't spend someone else's write budget on the strength of it.
    if identity_verified:
        rs._enforce_agent_rate_limit(req.agent_id)
    if rooms_db.is_member(room_id, req.agent_id, db_path=rs.DATABASE_PATH):
        # P50: membership is not proof of purchase — finish the settlement a
        # crash may have cut short before answering. See
        # _finish_interrupted_access_purchase().
        await rs._finish_interrupted_access_purchase(
            resource_type="room", resource_id=room_id,
            outcome_type="room_entry", payment_type="room_entry",
            payer_id=req.agent_id, creator_id=room.creator_id,
            access_duration_days=room.access_duration_days,
        )
        return {"status": "already_member", "room_id": room_id}
    # P18-4: joining an encrypted room without a published key would make every
    # subsequent message in that room fail coverage validation — one member with no
    # key blocks the whole room, not just themselves. Refused at the door, where the
    # error names the one thing the joiner has to do.
    if room.encrypted:
        rs._require_encryption_keys_published([req.agent_id])
    # P2-6/P2r-1 (R2/R1): a kick is permanent — checked before the gate branch
    # so a kicked agent can't buy or invite their way back in with a fresh
    # PaymentIntent or a leftover invite. Runs for every gate type, including
    # `open`: is_removed() (not is_revoked()) is what makes that true.
    # is_revoked() only answers "was there a paid entitlement to revoke here",
    # which is False for an open-room kick — revoke() had nothing to revoke,
    # so the old check silently let a kicked open-room member walk back in.
    # kick_from_room() calls both record_removal() (the actual ban) and
    # entitlements.revoke(reason="kicked") (ends any paid window); this checks
    # the former.
    if entitlements.is_removed("room", room_id, req.agent_id, reason="kicked",
                                db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Removed from this room")

    # P28-1: capacity is checked BEFORE the payment gate below. It used to be
    # checked only by add_member(), which runs *after* the entry fee has been
    # confirmed and recorded — and the room path confirms with
    # capture_method="automatic"/status "succeeded", so the money is captured,
    # not held. Joining a full paid room therefore charged the payer and then
    # answered 403 with no membership, no entitlement and no refund. add_member()
    # still re-checks under the write lock (two agents can race the last seat);
    # this is the early refusal that keeps a doomed join away from the money.
    if rooms_db.room_is_full(room_id, db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Room is full")

    confirmed_pi = None  # PaymentRef | None — imported function-local, see L2
    amount_cents = 0
    currency = "usd"
    if room.gate_type == "invite":
        if not rooms_db.has_invite(room_id, req.agent_id, db_path=rs.DATABASE_PATH):
            raise HTTPException(403, "No invite — ask the room creator")
    elif room.gate_type == "paid":
        amount_cents = room.entry_fee_cents or 0
        currency = (room.currency or "usd").lower()

        # P2-4 (R1): a payment buys access, not a single entry — see
        # entitlements.py's module docstring. If this agent already has an
        # active (not expired, not revoked) entitlement for this room, this is
        # a free rejoin: skip the payment gate entirely, confirmed_pi stays
        # None, and the payout/settlement block below (guarded on
        # `confirmed_pi`) never fires. Before this, every join re-ran
        # _verify_and_confirm_payment() and record_payment() (which is
        # deliberately idempotent on a replayed PaymentIntent), so a rejoin
        # with the same PI was free but still fired a second real
        # payout_winner() call, and a rejoin with a *fresh* PI was a second
        # real charge for one purchase.
        if not entitlements.has_access("room", room_id, req.agent_id, db_path=rs.DATABASE_PATH):
            # P157: a card entry is a destination charge to the creator.
            dest, fee, card_off = rs.card_payee(room.creator_id, amount_cents)
            result = await rs._verify_and_confirm_payment(
                request,
                agent_id=req.agent_id,
                identity_verified=identity_verified,
                amount_cents=amount_cents,
                currency=currency,
                resource_type="room",
                resource_id=room_id,
                service_type="emporia:room_entry",
                capture_method="automatic",
                allowed_statuses=("succeeded",),
                req_payment_intent_id=req.payment_intent_id,
                challenge_message=(
                    f"Room entry fee: {amount_cents} {currency} cents. "
                    "Retry with Authorization: Payment <spt_token>."
                ),
                challenge_resource=f"emporia:room:{room_id}",
                creator_id=room.creator_id,
                destination_account=dest,
                application_fee_cents=fee,
                card_unavailable=card_off,
            )
            if isinstance(result, JSONResponse):
                return result
            confirmed_pi = result

            # N4 review R1: record_payment() raises ValueError on a replayed PaymentIntent
            # id (already recorded for a different agent/room/amount) — surface as a
            # rejected join, not a 500.
            try:
                rs.record_payment(
                    payment_intent_id=confirmed_pi.reference,
                    agent_id=req.agent_id,
                    amount_cents=amount_cents,
                    payment_type="room_entry",
                    room_id=room_id,
                    currency=currency,
                    rail=confirmed_pi.rail,
                )
            except ValueError as e:
                raise HTTPException(402, str(e))

    # Membership grant — N5 review R2: this must commit before the payout below (and
    # before returning) so that a retry after a mid-flight failure is caught by the
    # already_member short-circuit above rather than re-entering payout work.
    #
    # P50 correction: "payout_winner is a real, non-idempotent Stripe transfer" was
    # true when N5 wrote it and stopped being true at P1a-3, which made
    # `idempotency_key` required and derived it from settlement_dedupe_key() — a
    # resumed payout for the same payment reuses the same key and is a no-op at
    # Stripe. The ordering is still right, and the residual reason is narrower than
    # the one recorded: Stripe expires an Idempotency-Key after 24 hours, so a
    # *very* late replay would move money twice. What was wrong was treating the
    # short-circuit as free — it also ended the request, stranding any purchase the
    # crash had left unfinished. See _finish_interrupted_access_purchase().
    try:
        rooms_db.add_member(room_id, req.agent_id, db_path=rs.DATABASE_PATH)
    except ValueError as e:
        raise HTTPException(403, str(e))

    if room.gate_type == "paid" and confirmed_pi:
        await rs._settle_access_payment(
            resource_type="room",
            resource_id=room_id,
            outcome_type="room_entry",
            payer_id=req.agent_id,
            creator_id=room.creator_id,
            amount_cents=amount_cents,
            payment_ref=confirmed_pi,
            access_duration_days=room.access_duration_days,
        )

    await rs.broadcast_to_session(f"room:{room_id}", {
        "type": "member_joined",
        "room_id": room_id,
        "agent_id": req.agent_id,
    })
    return {"status": "joined", "room_id": room_id}
@router.post("/rooms/{room_id}/invite")
async def invite_to_room(room_id: str, req: rs.InviteToRoomRequest, request: Request):
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_caller_is(room.creator_id, request)
    rs._enforce_agent_rate_limit(room.creator_id)
    if room.gate_type == "open":
        raise HTTPException(400, "Open rooms don't use invites")
    rs._require_registered(req.invitee_id)
    rooms_db.add_invite(room_id, req.invitee_id, room.creator_id, db_path=rs.DATABASE_PATH)
    await rs.broadcast_to_agent(req.invitee_id, {
        "type": "room_invite",
        "room_id": room_id,
        "room_name": room.name,
        "invited_by": room.creator_id,
    })
    return {"status": "invited", "room_id": room_id, "invitee_id": req.invitee_id}
@router.post("/rooms/{room_id}/kick")
async def kick_from_room(room_id: str, req: rs.KickFromRoomRequest, request: Request):
    """P2-6/P2r-1 (R2/R1): a kick is a permanent removal, not a temporary one —
    by product decision, kicked means out, on every room type. remove_member()
    alone was not enforceable: a paid room's kicked payer could
    rejoin by replaying their PaymentIntent (closed by P2-4's entitlement
    check), and an invite-gated room's kicked invitee could rejoin because
    add_invite()/has_invite() never consumed the invite on join.

    Two separate records close two separate gaps:
    - `record_removal(reason="kicked")` is the ban itself — checked by
      join_room()'s ban check, the first thing it does after the
      already_member short-circuit, and it works regardless of gate type or
      whether anything was ever paid.
    - `entitlements.revoke(reason="kicked")` ends any active paid window, so a
      still-valid entitlement can't outlive the ban even before the ban check
      runs (belt-and-suspenders with the above, not a substitute for it — P2-6
      shipped only this half and a kick from an `open` room didn't stick at
      all, since `revoke()` has nothing to revoke when nothing was ever
      bought; see entitlements.py's module docstring).

    `remove_invite()` closes the invite-gated room's own version of the same
    class of bug — a stale invite doing the job a stale entitlement used to.
    """
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_caller_is(room.creator_id, request)
    rs._enforce_agent_rate_limit(room.creator_id)
    if req.target_agent_id == room.creator_id:
        raise HTTPException(400, "Creator cannot kick themselves")
    removed = rooms_db.remove_member(room_id, req.target_agent_id, db_path=rs.DATABASE_PATH)
    if not removed:
        raise HTTPException(404, f"Agent not in room: {req.target_agent_id}")
    entitlements.record_removal("room", room_id, req.target_agent_id, reason="kicked",
                                 removed_by=room.creator_id, db_path=rs.DATABASE_PATH)
    entitlements.revoke("room", room_id, req.target_agent_id, reason="kicked",
                         db_path=rs.DATABASE_PATH)
    rooms_db.remove_invite(room_id, req.target_agent_id, db_path=rs.DATABASE_PATH)

    # P2-7: revocation above happens unconditionally; the refund is a
    # separate, optional, best-effort step on top of it. Its failure must not
    # undo the kick or the revocation already committed.
    refund_result = None
    if req.refund:
        refund_result = await rs._refund_kicked_room_payer(room_id, req.target_agent_id)

    await rs.broadcast_to_session(f"room:{room_id}", {
        "type": "member_kicked",
        "room_id": room_id,
        "agent_id": req.target_agent_id,
    })
    await rs.broadcast_to_agent(req.target_agent_id, {
        "type": "kicked_from_room",
        "room_id": room_id,
        "room_name": room.name,
    })
    return {"status": "kicked", "agent_id": req.target_agent_id, "refund": refund_result}
@router.post("/rooms/{room_id}/message")
async def send_room_message(room_id: str, req: rs.SendRoomMessageRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_registered(caller)
    if not rooms_db.is_member(room_id, caller, db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Join the room first")
    rs._enforce_agent_rate_limit(caller)

    # P12: msg_type resolved here (not left as a bare pydantic field default) so it's
    # single-sourced from wire.py the same way confirm/dispute's rationale is — a raw
    # client omitting the optional field used to be verified against this file's own
    # hardcoded "chat" copy, a hand-copied literal that could silently diverge from the
    # one mcp_server.py/agent_sdk.py's signers actually send. Resolved once and reused
    # for both signature verification and persistence below, so an omitted field can
    # never land as an empty msg_type in storage. Local import (not module-level) is
    # deliberate, matching confirm_delivery/dispute_delivery: it re-reads
    # wire.DEFAULT_MSG_TYPE's current value on every call rather than binding a copy at
    # module-import time, which is what makes both this and the rationale constants
    # actually re-patchable at test/runtime rather than merely another hardcoded copy.
    from emporia.wire import (
        ERR_DUPLICATE_NONCE,
        ERR_MISSING_RECIPIENTS,
        DEFAULT_MSG_TYPE,
        signed_room_message_payload,
    )
    effective_msg_type = req.msg_type or DEFAULT_MSG_TYPE
    rs._require_valid_msg_type(effective_msg_type)
    rs._require_content_within_limit(req.content)

    # Encrypted rooms: relay stores opaque ciphertext — never scan it.
    # Ciphertext is binary/base64 and WILL false-positive on injection patterns.
    # Membership check above is the only gate; clients own key exchange + decryption.
    # P15: msg_type is no longer in this payload. P14 added it (a real gap at the time —
    # the field was free-form and prose-capable), but _require_valid_msg_type() above now
    # rejects anything outside a closed six-value enum, every member of which is exempt
    # from scanning anyway as a token-shaped value, so scanning it here would be provably
    # dead code behind a strictly stronger control. The enum runs before the scan on both
    # message routes so the same bad input gets the same actionable 400 naming the valid
    # values, rather than one route saying 400 and the other a generic guardrails 403.
    # P18-4: enforce the room's mode, the room analogue of the DM check. The relay
    # reads no plaintext and holds no key — it inspects the envelope's structure and
    # the *set of agent_ids* the content key was wrapped to.
    from emporia import crypto_box
    if room.encrypted:
        # P19 (H5): the DM route (dm_send, below) has this exact guard for the exact
        # same reason — the room AAD binds msg_type, and this handler *resolves*
        # msg_type two lines above (`req.msg_type or DEFAULT_MSG_TYPE`). A client
        # that omits the field seals under "" while the row stores "chat"; every
        # reader then computes different AAD and the message is permanently
        # unreadable, accepted with a 200. P18-4 shipped the room's coverage check
        # without this one — the same "X or DEFAULT" resolution-shape defect class
        # (P12-3, P13-1, P15-1, P17-1) recurring in the very code that closed the
        # DM instance of it.
        if not req.msg_type:
            raise HTTPException(
                400,
                "msg_type must be sent explicitly on an end-to-end encrypted room: "
                "it is bound into the message's AAD, and the relay's own default "
                f"resolution (to '{DEFAULT_MSG_TYPE}') would not match what you "
                "sealed under, making the message permanently unreadable",
            )
        try:
            # Coverage is checked against room.members (structural membership), the
            # same set _room_member_encryption_keys() hands clients — see there for
            # why a lapsed paid member stays in it.
            crypto_box.validate_envelope_for(req.content, set(room.members))
        except crypto_box.MissingRecipients as e:
            # 409, not 400: membership churns, so a joiner arriving between the
            # client's key-fetch and its send is a *race*, not a malformed request.
            # Naming the missing members makes it recoverable — re-fetch the
            # bundle, re-seal, retry — instead of silently producing a message
            # they can never read. P19: this used to be a substring match on
            # crypto_box's error text ("no wrapped key for" in str(e)) — a
            # dedicated exception type means rewording that message can no longer
            # silently turn every membership race into a terminal 400.
            # P21-3 (B4): structured `code` -- this room route's 409 is otherwise
            # ambiguous with the duplicate-nonce 409 below, and clients used to
            # tell them apart the same substring-matching way, one exception type
            # later. See wire.ERR_MISSING_RECIPIENTS.
            raise HTTPException(409, {
                "code": ERR_MISSING_RECIPIENTS,
                "message": f"this room is end-to-end encrypted — {e}",
            })
        except ValueError as e:
            raise HTTPException(400, f"this room is end-to-end encrypted — {e}")
    elif crypto_box.looks_like_envelope(req.content):
        raise HTTPException(
            400,
            "this room is not encrypted, so an encrypted envelope cannot be stored "
            "in it — create the room with encrypted=true instead",
        )

    if not req.signature:
        raise HTTPException(401, "Signature required")
    # P19-5 (H4): the DM route (dm_send, above) has this exact guard for the exact
    # same reason — without a nonce bound into the signature, an identical message
    # replayed to this room verifies and stores again indistinguishably from the
    # original. Checked here rather than earlier so a missing *signature* still
    # answers 401 first, matching the DM route's ordering.
    if not req.nonce:
        raise HTTPException(
            400,
            "nonce is required — it binds this message's signature (and, on an "
            "encrypted room, its ciphertext) to this message, so a stored row "
            "cannot be replayed as another. Generate one with "
            "wire.new_message_nonce().",
        )
    # P20-5 (A9): moved ahead of the guardrails scan below — a cheap regex check
    # shouldn't run after the expensive (paid NIM) scan P15 already hoisted checks
    # like this one to avoid wasting.
    if not rs._NONCE_RE.fullmatch(req.nonce):
        raise HTTPException(
            400, "nonce must be 32-64 hex characters (see wire.new_message_nonce())",
        )

    # N3-8 (R7): bound to room_id + msg_type, not just content — a content-only
    # signature was valid in *any* room the caller belongs to, forever (replayable
    # across rooms and across msg_type, e.g. a signed "chat" replayed as "accept").
    # P21-2 (B3): moved ahead of the guardrails scan below — Ed25519 verification is
    # microseconds; the scan is a paid NIM call. A9 hoisted the cheap nonce-shape
    # check over the scan and stopped one statement short of this one, so a
    # registered member sending a garbage signature still burned scan budget on
    # every request until now.
    signed_envelope = signed_room_message_payload(room_id, req.content, effective_msg_type, req.nonce)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid message signature")

    if not room.encrypted:
        try:
            await rs._assert_payload_safe_counted({"content": req.content})
        except PermissionError as e:
            raise HTTPException(403, str(e))

    # Negotiation broker: route counter_offer/accept/reject in 2-member private rooms
    broker_response: dict[str, Any] | None = None
    if effective_msg_type in ("counter_offer", "accept", "reject") and room.is_negotiation_room():
        # P19-6 (M8): the broker used to run unconditionally here — on an
        # encrypted room, req.content is the *envelope*, not the offer: it's a
        # JSON dict (crypto_box.seal()'s output serialized), so
        # `req.content.strip().startswith("{")` is true and json.loads()
        # parses it cleanly, handing process_offer() the envelope's structural
        # fields (v, alg, epk, ct, ...) as if they were an offer. Worse than a
        # no-op: on ACCEPT/COUNTER it posted `broker_response` as a *plaintext*
        # system row via rooms_db.post_message() below, bypassing
        # validate_envelope_for() entirely — a guaranteed plaintext write into
        # a room whose entire point is that the relay never holds plaintext.
        # The relay cannot decrypt, so it cannot broker; refuse cleanly instead.
        if room.encrypted:
            raise HTTPException(
                400,
                "the negotiation broker is unavailable in an end-to-end encrypted "
                "room — it would need to read the offer's plaintext to apply the "
                "midpoint algorithm, which is exactly what this room's encryption "
                "denies the relay. Negotiate out of band, or use an unencrypted room.",
            )
        from emporia.negotiation import process_offer
        try:
            offer_payload = json.loads(req.content) if req.content.strip().startswith("{") else {"text": req.content}
            constraints = req.negotiation_constraints or {}
            decision, response = process_offer(caller, offer_payload, constraints)
            broker_response = {"decision": decision, "response": response}
        except Exception:
            pass  # non-JSON content — treat as plain chat

    try:
        msg = rooms_db.post_message(
            room_id=room_id,
            sender_id=caller,
            content=req.content,
            msg_type=effective_msg_type,
            parent_message_id=req.parent_message_id,
            signature=req.signature,
            nonce=req.nonce,
            metadata={"broker": broker_response} if broker_response else {},
            db_path=rs.DATABASE_PATH,
        )
    except sqlite3.IntegrityError as e:
        # P19-5 (H4): idx_room_messages_nonce's UNIQUE(room_id, nonce) is the
        # database-level half of the replay defense, mirroring the DM fix in
        # dm_send above — same reasoning, same 409.
        raise HTTPException(409, {
            "code": ERR_DUPLICATE_NONCE,
            "message": "duplicate nonce — this message has already been posted to this room",
        }) from e

    event: dict[str, Any] = {"type": "room_message", "room_id": room_id, **msg.to_dict()}
    if broker_response:
        event["broker"] = broker_response
    await rs.broadcast_to_session(f"room:{room_id}", event)
    # Push to member inboxes so offline agents don't miss messages
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT members_json FROM rooms WHERE room_id=?", (room_id,)).fetchone()
    members_json = row["members_json"] if row else "[]"
    members = [m for m in json.loads(members_json) if m != caller]
    for member in members:
        await rs.broadcast_to_agent(member, event)

    # If broker decided, post a system follow-up
    if broker_response and broker_response["decision"] in ("ACCEPT", "COUNTER"):
        sys_content = (
            f"[Emporia broker] {broker_response['decision']}: "
            f"{json.dumps(broker_response['response'])}"
        )
        sys_msg = rooms_db.post_message(
            room_id=room_id,
            sender_id="emporia:broker",
            content=sys_content,
            msg_type="system",
            parent_message_id=msg.message_id,
            db_path=rs.DATABASE_PATH,
        )
        await rs.broadcast_to_session(f"room:{room_id}", {
            "type": "room_message",
            "room_id": room_id,
            **sys_msg.to_dict(),
        })

    result = msg.to_dict()
    if broker_response:
        result["broker"] = broker_response
    return result
@router.get("/rooms/{room_id}/encryption-keys")
async def get_room_encryption_keys(room_id: str, request: Request):
    """Every member's encryption key in one round trip — what a client needs to
    seal a message to an encrypted room.

    Exists as a bundle rather than N calls to `/agents/{id}/encryption-key` for two
    reasons: N requests to send one message is a poor trade, and more importantly
    the member set could drift between the first fetch and the last, producing a
    message that silently excludes someone. One snapshot, one answer.

    Members only — the key material is public, but the *membership list* is not,
    and this returns both.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if not rooms_db.is_member(room_id, caller, db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Not a member of this room")
    return {"room_id": room_id, "encrypted": room.encrypted,
            "keys": rs._room_member_encryption_keys(room)}
@router.get("/rooms/{room_id}/messages")
async def get_room_messages(
    room_id: str,
    request: Request,
    limit: int = 50,
    before: str | None = None,
    before_seq: int | None = None,
):
    viewer_id = rs._dashboard_agent_id(request)
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    # P31-3: `before_seq` completes the pagination cursor — see get_messages().
    msgs = rooms_db.get_messages(
        room_id, limit=rs._clamp_limit(limit), before=before,
        before_seq=before_seq, db_path=rs.DATABASE_PATH,
    )
    return {"messages": [m.to_dict() for m in msgs], "count": len(msgs)}
@router.get("/rooms/{room_id}/chain")
async def get_room_chain(room_id: str, request: Request):
    """Chain-integrity verification for a room — the fix direction SECURITY.md
    has recorded since P19: "the relay signs each chain head with its
    federation keypair, a route serves it, and clients recompute and verify on
    read." Mirrors GET /sessions/{id}/audit (session_audit.verify_chain(),
    signed the same way GET /gaming/v1/federate/listings signs a batch —
    federation_signable_bytes() already invites a third `key`, see its
    docstring) — same gate as GET /rooms/{id}/messages (private rooms require
    membership; public rooms are open to any connection).

    Deliberately returns hashes, not message content — the caller already has
    every message's own `content`/`chain_hash` from GET /rooms/{id}/messages;
    this route's only job is the piece that read can't provide on its own: an
    attestation, signed with a key the caller doesn't hold, of what the
    current chain head actually is.

    **What the signature does and does not prove.** It proves the response
    came from this relay's own key, unaltered in transit — a third party
    (network MITM, a compromised CDN in front of the relay) cannot forge a
    different head. It does **not** prove the relay itself is honest: an
    operator with DB write access can rewrite a message's content, recompute
    a self-consistent chain from that point forward (verify_chain() would
    report `verified: true` for the tampered chain, since recomputation-and-
    compare only catches inconsistency, not a rewrite that stays internally
    consistent), and sign the new head with the same key — the signature
    changes nothing about that, since the relay holds it. Catching *that*
    is what client-side head-pinning is for (agent_sdk.py's/mcp_server.py's
    verify_room_chain(), which persist the last head they saw per room and
    refuse a chain that doesn't extend it — the same TOFU shape
    _FEDERATION_PEER_PUBKEYS already uses for peer relays). This route is the
    signed-attestation half of that; without it, there would be nothing for a
    client to pin at all.

    **What the cap changes, and what it doesn't** (P44). The recompute is bounded
    to the newest `CHAIN_VERIFY_LIMIT` messages, and `chain_verified_scope` says
    which happened — `"genesis"` when the walk reached the room's first message,
    `"recent_window"` when it stopped short. `chain_head` and `message_count` are
    unaffected: both are indexed lookups, not walk products, so the *signed*
    payload is byte-identical at any cap and the five independent verifiers that
    reconstruct those bytes need no change. What a window gives up is the relay's
    own opinion about old messages — which the paragraph above already says is
    not the guarantee. A client that wants more asks for more: the SDK's and MCP
    server's `verify_room_chain()` page backward through
    GET /rooms/{id}/messages and recompute the history themselves, against a head
    they pinned earlier, which is the check that actually catches an operator.
    """
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    viewer_id = rs._dashboard_agent_id(request)
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    result = await run_in_threadpool(
        rooms_db.verify_chain, room_id, db_path=rs.DATABASE_PATH, limit=rs.CHAIN_VERIFY_LIMIT,
    )
    signature = sign_raw_relay(federation_signable_bytes(
        rs.RELAY_BASE_URL, "room_chain",
        [{
            "room_id": room_id,
            "chain_head": result.head or "",
            "message_count": result.message_count,
        }],
    ))
    return {
        "room_id": room_id,
        "verified": result.ok,
        "message": result.message,
        "chain_head": result.head,
        "message_count": result.message_count,
        "verified_message_count": result.verified_count,
        "chain_verified_scope": result.scope,
        "origin_relay": rs.RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
    }
