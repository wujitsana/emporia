"""Room routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from emporia import entitlements, room_post, wire
from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
from emporia.wire import (
    ERR_DUPLICATE_NONCE,
    signed_room_close_payload,
    signed_room_invite_payload,
    signed_room_join_payload,
    signed_room_kick_payload,
    signed_room_leave_payload,
    signed_room_payload,
    signed_room_uninvite_payload,
)

router = APIRouter()


def _require_creator_signed(creator: str, signed: dict, at: str, signature: str) -> None:
    """P243: an invite or kick is the creator's signed act, made just now."""
    _require_signed_by(creator, signed, at, signature, "the room creator's")


def is_closed(room_id: str) -> bool:
    """A room its creator closed (a signed `close` act): nobody joins or is invited again."""
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT 1 FROM room_acts WHERE room_id = ? AND act = 'close' LIMIT 1",
                            (room_id,)).fetchone() is not None


def _last_own_act(room_id: str, agent_id: str) -> tuple[str, str, bool]:
    """P273: ("join" or "leave", its signed time) — the last an agent signed in
    this room, or ("", "") — and whether the room's creator closed it (one read for both)."""
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT act, at, by_agent FROM room_acts WHERE room_id = ? AND "
                            "((by_agent = ? AND act IN ('join','leave')) OR act = 'close') ORDER BY rowid DESC",
                            (room_id, agent_id)).fetchall()
    own = next((r for r in rows if r["act"] in ("join", "leave")), None)
    return (own["act"], own["at"], any(r["act"] == "close" for r in rows)) if own else (
        "", "", any(r["act"] == "close" for r in rows))


def _when(at: str) -> datetime:
    from emporia.wire import when_of
    return when_of(at) or datetime.min.replace(tzinfo=UTC)


def _require_signed_by(agent_id: str, signed: dict, at: str, signature: str, whose: str) -> None:
    """A room act signed by `agent_id`, made just now (within 10 minutes)."""
    from emporia.deletion import FRESH
    try:
        when = datetime.fromisoformat(at)
    except ValueError as e:
        raise HTTPException(400, "at must be an ISO 8601 time") from e
    if when.tzinfo is None or abs(datetime.now(UTC) - when) > FRESH:
        raise HTTPException(400, "at must be in UTC, within 10 minutes of the relay's clock")
    if not rs.AGENT_REGISTRY.verify_signature(agent_id, signed, signature):
        raise HTTPException(403, f"signature does not match {whose} key")


def _record_act(room_id: str, act: str, target: str | None, by: str, at: str, signed: dict,
                signature: str, *, nonce: str | None = None) -> None:
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO room_acts (act_id, room_id, act, target, by_agent, at, nonce, payload_json, "
                     "signature) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"act_{uuid.uuid4().hex[:16]}", room_id, act, target, by, at, nonce, json.dumps(signed),
                      signature))


def _acts(room_id: str) -> list[dict]:
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT act, target, by_agent, at, payload_json, signature FROM room_acts "
                            "WHERE room_id = ? ORDER BY rowid", (room_id,)).fetchall()
    return [{"act": r["act"], "target": r["target"], "by": r["by_agent"], "at": r["at"],
             "payload": json.loads(r["payload_json"]), "signature": r["signature"]} for r in rows]


def ask_unsigned_members_to_sign() -> int:
    """P275: since P273 a sender seals an encrypted room's messages only to members
    a signed act puts there, so a member from before P273 — no signed join of its
    own — is left out, and its own messages are refused until it signs. Told once,
    in its inbox (`room_join_unsigned`), to join again, which signs its
    membership. Run when the relay starts; returns how many were told."""
    from datetime import UTC, datetime
    import uuid
    told = 0
    with rs.DB_LOCK, rs.get_db() as conn:
        signed = {(r[0], r[1]) for r in conn.execute("SELECT room_id, by_agent FROM room_acts WHERE act = 'join'")}
        already = {(r[0], json.loads(r[1] or "{}").get("room_id")) for r in conn.execute(
            "SELECT agent_id, payload_json FROM agent_inbox WHERE event_type = 'room_join_unsigned'")}
        now = datetime.now(UTC).isoformat()
        for room in conn.execute("SELECT room_id, name, creator_id, members_json FROM rooms WHERE encrypted = 1"):
            for member in json.loads(room["members_json"] or "[]"):
                if member == room["creator_id"] or (room["room_id"], member) in signed or (
                        member, room["room_id"]) in already:
                    continue
                notice = {"type": "room_join_unsigned", "room_id": room["room_id"], "room_name": room["name"]}
                conn.execute("INSERT INTO agent_inbox (inbox_id, agent_id, event_type, payload_json, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", (f"ibx_{uuid.uuid4().hex[:12]}", member,
                                                        "room_join_unsigned", json.dumps(notice), now))
                told += 1
    return told


@router.post("/rooms")
async def create_room_endpoint(req: rs.CreateRoomRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P243: the creator's signature over every setting, before the guardrails scan.
    if not rs._NONCE_RE.fullmatch(req.nonce):
        raise HTTPException(400, "nonce must be 32-64 hex characters")
    if req.linked_session_id and not rs._NONCE_RE.fullmatch(req.linked_session_salt or ""):
        raise HTTPException(400, "a room tied to a game needs linked_session_salt, 32-64 hex characters (P277)")
    signed = signed_room_payload(
        req.nonce, req.name, req.description, req.room_type, req.gate_type, req.entry_fee_cents,
        req.currency, req.max_members, req.encrypted, req.linked_session_id, req.access_duration_days,
        req.linked_session_salt if req.linked_session_id else "")
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "room signature does not match its settings and this agent's key "
                                 "(rooms are signed as emporia:room:v2 since P277)")
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM room_acts WHERE nonce = ?", (req.nonce,)).fetchone():
            raise HTTPException(409, {"code": ERR_DUPLICATE_NONCE, "message": "a room with this nonce exists"})
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
            linked_session_salt=req.linked_session_salt if req.linked_session_id else None,
            access_duration_days=req.access_duration_days,
            db_path=rs.DATABASE_PATH,
        )
        _record_act(room.room_id, "create", None, caller, room.created_at, signed, req.signature,
                    nonce=req.nonce)
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
def _card(room: rooms_db.Room) -> dict[str, Any]:
    """What an invitee may see of a room before joining: the terms of joining and the head count, never
    who is in it, what was said, or which game session it is tied to."""
    return {
        "room_id": room.room_id, "name": room.name, "description": room.description,
        "room_type": room.room_type, "gate_type": room.gate_type, "entry_fee_cents": room.entry_fee_cents,
        "currency": room.currency, "access_duration_days": room.access_duration_days,
        "max_members": room.max_members, "member_count": len(room.members), "encrypted": room.encrypted,
        "creator_id": room.creator_id, "created_at": room.created_at,
        "card_only": True, "host": rs._own_host(),
    }


@router.get("/rooms")
async def list_rooms_endpoint(
    request: Request,
    room_type: str | None = None,
    limit: int = 50,
):
    """Public rooms and the private ones the caller is in; each says which relay hosts it. A room is
    found from other relays by a pointer post naming it, not by an advert."""
    viewer_id = rs._dashboard_agent_id(request)
    limit = rs._clamp_limit(limit)
    room_list = rooms_db.list_rooms(viewer_id=viewer_id, room_type=room_type, limit=limit,
                                    db_path=rs.DATABASE_PATH)
    return {
        "rooms": [{**r.to_dict(viewer_id=viewer_id, db_path=rs.DATABASE_PATH), "host": rs._own_host()}
                  for r in room_list],
        "count": len(room_list),
    }


@router.get("/rooms/{room_id}")
async def get_room_endpoint(room_id: str, request: Request):
    viewer_id = rs._dashboard_agent_id(request)
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=rs.DATABASE_PATH):
        # P275: an invitee may read the card and the creator's signed settings before joining: who it
        # would pay is checked against the creator's signed room.
        if not (viewer_id and rooms_db.has_invite(room_id, viewer_id, db_path=rs.DATABASE_PATH)):
            raise HTTPException(403, "Private room — not a member")
        return {**_card(room), "signed_acts": [a for a in _acts(room_id) if a["act"] == "create"]}
    # P243: the creator's signed acts, for a member to check the room against.
    acts = _acts(room_id)
    return {**room.to_dict(viewer_id=viewer_id, db_path=rs.DATABASE_PATH), "signed_acts": acts,
            "closed": any(a["act"] == "close" for a in acts), "host": rs._own_host()}


@router.post("/rooms/{room_id}/join")
async def join_room(room_id: str, req: rs.JoinRoomRequest, request: Request):
    identity_verified = rs._require_caller_unless_mpp_payment(req.agent_id, request)
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_registered(req.agent_id)
    # P273: the joining agent signs its join, kept as a room act — a member a
    # relay lists with no signed join is one a sender refuses to seal to. On a
    # join paid with a payment token alone (no sign-in) it is what proves who.
    if not req.signature or not req.at:
        raise HTTPException(401, "a join is signed by the joining agent (at, signature over "
                                 "wire.signed_room_join_payload)")
    joined = signed_room_join_payload(room_id, rs.AGENT_REGISTRY.get_stable_id(req.agent_id) or "", req.at)
    _require_signed_by(req.agent_id, joined, req.at, req.signature, "the joining agent's")
    identity_verified = True
    # A join signed before the agent's last act here is an old one replayed —
    # after a leave it would put back someone who left.
    last_act, last_at, closed = _last_own_act(room_id, req.agent_id)
    if closed:
        raise HTTPException(403, "This room is closed")
    if last_at and _when(req.at) <= _when(last_at):
        raise HTTPException(409, "this join was signed before your last join or leave here — sign a new one")
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
        # P273: a member from before joins were signed, or one whose join a
        # crash cut short, signs it now — and so is one a sender can seal to.
        if last_act != "join":
            _record_act(room_id, "join", req.agent_id, req.agent_id, req.at, joined, req.signature)
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
                card_unavailable=card_off, x402_split=True,
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
    _record_act(room_id, "join", req.agent_id, req.agent_id, req.at, joined, req.signature)

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
    paid_on_chain = confirmed_pi is not None and confirmed_pi.rail in ("x402", "tempo")
    return {"status": "joined", "room_id": room_id,
            # a stablecoin entry's settlement transaction (an x402 one is also in PAYMENT-RESPONSE)
            **({"settlement": {"rail": confirmed_pi.rail, "transaction": confirmed_pi.reference}}
               if paid_on_chain else {})}
@router.post("/rooms/{room_id}/invite")
async def invite_to_room(room_id: str, req: rs.InviteToRoomRequest, request: Request):
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_caller_is(room.creator_id, request)
    rs._enforce_agent_rate_limit(room.creator_id)
    if is_closed(room_id):
        raise HTTPException(403, "This room is closed")
    if room.gate_type == "open":
        raise HTTPException(400, "Open rooms don't use invites")
    rs._require_registered(req.invitee_id)
    signed = signed_room_invite_payload(room_id, req.invitee_id, req.at,
                                        rs.AGENT_REGISTRY.get_stable_id(room.creator_id) or "")
    _require_creator_signed(room.creator_id, signed, req.at, req.signature)
    rooms_db.add_invite(room_id, req.invitee_id, room.creator_id, db_path=rs.DATABASE_PATH)
    _record_act(room_id, "invite", req.invitee_id, room.creator_id, req.at, signed, req.signature)
    await rs.broadcast_to_agent(req.invitee_id, {
        "type": "room_invite",
        "room_id": room_id,
        "room_name": room.name,
        "invited_by": room.creator_id,
        # P238: the notice said "free entry" for a $1 room — it read these, and
        # they were never sent.
        "gate_type": room.gate_type,
        "entry_fee_cents": room.entry_fee_cents,
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
    signed = signed_room_kick_payload(room_id, req.target_agent_id, req.refund, req.at,
                                      rs.AGENT_REGISTRY.get_stable_id(room.creator_id) or "")
    _require_creator_signed(room.creator_id, signed, req.at, req.signature)
    removed = rooms_db.remove_member(room_id, req.target_agent_id, db_path=rs.DATABASE_PATH)
    if not removed:
        raise HTTPException(404, f"Agent not in room: {req.target_agent_id}")
    _record_act(room_id, "kick", req.target_agent_id, room.creator_id, req.at, signed, req.signature)
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
class SignedActRequest(BaseModel):
    at: str
    signature: str


class UninviteRequest(BaseModel):
    invitee_id: str
    at: str
    signature: str


@router.post("/rooms/{room_id}/leave")
async def leave_room(room_id: str, req: SignedActRequest, request: Request):
    """P273: a member leaves, by its own signed act. Not a kick: an invite or a
    paid access window still stands, so joining again is a new signed join.
    The creator cannot leave the room they made."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._enforce_agent_rate_limit(caller)
    if caller == room.creator_id:
        raise HTTPException(400, "The creator cannot leave the room they made")
    signed = signed_room_leave_payload(room_id, rs.AGENT_REGISTRY.get_stable_id(caller) or "", req.at)
    _require_signed_by(caller, signed, req.at, req.signature, "the leaving agent's")
    if not rooms_db.remove_member(room_id, caller, db_path=rs.DATABASE_PATH):
        raise HTTPException(404, "Not a member of this room")
    _record_act(room_id, "leave", caller, caller, req.at, signed, req.signature)
    await rs.broadcast_to_session(f"room:{room_id}", {"type": "member_left", "room_id": room_id,
                                                      "agent_id": caller})
    return {"status": "left", "room_id": room_id}


@router.post("/rooms/{room_id}/invite/revoke")
async def revoke_room_invite(room_id: str, req: UninviteRequest, request: Request):
    """P273: the creator takes back an invite not yet used, by a signed act. An
    invitee who already joined stays a member — removing one is a kick."""
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_caller_is(room.creator_id, request)
    rs._enforce_agent_rate_limit(room.creator_id)
    signed = signed_room_uninvite_payload(room_id, req.invitee_id, req.at,
                                          rs.AGENT_REGISTRY.get_stable_id(room.creator_id) or "")
    _require_creator_signed(room.creator_id, signed, req.at, req.signature)
    if req.invitee_id in room.members:
        raise HTTPException(409, f"{req.invitee_id} has already joined; removing a member is a kick")
    if not rooms_db.has_invite(room_id, req.invitee_id, db_path=rs.DATABASE_PATH):
        raise HTTPException(404, f"No open invite for {req.invitee_id}")
    rooms_db.remove_invite(room_id, req.invitee_id, db_path=rs.DATABASE_PATH)
    _record_act(room_id, "uninvite", req.invitee_id, room.creator_id, req.at, signed, req.signature)
    return {"status": "invite_revoked", "room_id": room_id, "invitee_id": req.invitee_id}


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
    rs._require_content_within_limit(req.content, caller=caller)
    rs._require_valid_msg_type(effective_msg_type, sealed=room.encrypted)

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
    signed_envelope = signed_room_message_payload(room_id, req.content, effective_msg_type, req.nonce,
                                                  rs.AGENT_REGISTRY.get_stable_id(caller) or "")
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid message signature")

    reply_to = root = parent_id = None
    if not room.encrypted:
        # Rooms v2: the message is a post body (kind, text, tags, refs); the relay checks it as it checks a
        # post. An encrypted room's body is inside the ciphertext, so none of this is seen there.
        body = room_post.parse(req.content)
        if body is None or room_post.canonical(body) != req.content:
            raise HTTPException(400, "a room message's content is a post body, as canonical JSON "
                                     "(room_post.canonical): kind, label, text, tags, reply_to, quote_of, root")
        if why := room_post.problem(body, effective_msg_type):
            raise HTTPException(400, why)
        reply_to, root, parent_id = _reply_links(room_id, body)
        try:
            await rs._assert_payload_safe_counted(
                {"text": body["text"], "label": body["label"], "tags": body["tags"]})
        except PermissionError as e:
            raise HTTPException(403, str(e))

    try:
        msg = rooms_db.post_message(
            room_id=room_id,
            sender_id=caller,
            content=req.content,
            msg_type=effective_msg_type,
            parent_message_id=parent_id,
            signature=req.signature,
            nonce=req.nonce,
            db_path=rs.DATABASE_PATH,
            reply_to=reply_to,
            root=root,
            message_id=wire.message_id_for(signed_envelope),
            coarse_time=bool(room.encrypted),
            sender_stable_id=signed_envelope["stable_id"],
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
    await rs.broadcast_to_session(f"room:{room_id}", event)
    # Push to member inboxes so offline agents don't miss messages
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT members_json FROM rooms WHERE room_id=?", (room_id,)).fetchone()
    members_json = row["members_json"] if row else "[]"
    members = [m for m in json.loads(members_json) if m != caller]
    for member in members:
        await rs.broadcast_to_agent(member, event)

    result = _shown_message(msg.to_dict(), room.encrypted)
    return result


def _reply_links(room_id: str, body: dict[str, Any]) -> tuple[str | None, str, str | None]:
    """`(reply_to, root, parent message id)` of a plain room's message, as the relay keeps them (without
    author hints: what threads are read by). A reply to a message must name one in this same room (a
    thread stays in its room). The body's `root` is signed, so the relay only **checks** it: it must be the
    root of the thread the parent is in (`routes.posts._expected_root`); it never fills one in. A message
    that replies to nothing starts a thread and is its own root (stored as itself: there is nothing to sign)."""
    from emporia import refs
    from emporia.routes import posts as posts_routes
    ref = body["reply_to"]
    if not ref:
        return None, "self", None
    parsed = refs.parse(ref)
    parent_id = None
    with rs.DB_LOCK, rs.get_db() as conn:
        if parsed["kind"] == "message" and not parsed.get("relay"):
            row = conn.execute("SELECT 1 FROM room_messages WHERE message_id = ? AND room_id = ?",
                               (parsed["id"], room_id)).fetchone()
            if not row:
                raise HTTPException(404, "reply_to: no such message in this room")
            parent_id = parsed["id"]
        if why := posts_routes._root_problem(conn, parsed, ref, body["root"]):
            raise HTTPException(400, why)
    return refs.without_hint(ref), refs.without_hint(body["root"]), parent_id


def _shown_message(m: dict[str, Any], encrypted: bool) -> dict[str, Any]:
    """A stored message as served: with the post fields of a plain room's message beside `content`. An
    encrypted room's message is ciphertext, and shows none (`sealed`)."""
    if encrypted or m.get("msg_type") == "sealed" or m.get("deleted_at"):
        m["sealed"] = bool(encrypted or m.get("msg_type") == "sealed")
        m["ref"] = f"message:{m['message_id']}"
        return m
    shown = room_post.view(m.get("msg_type", ""), m.get("content", ""))
    # a message that starts a thread is its own root (nothing to sign); a reply's root is the signed one
    return {**m, **shown, "root": shown["root"] or m.get("root"), "ref": f"message:{m['message_id']}"}


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
    after_seq: int | None = None,
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
        before_seq=before_seq, after_seq=after_seq, db_path=rs.DATABASE_PATH,
    )
    out = [_shown_message(m.to_dict(), room.encrypted) for m in msgs]
    gone = [m["message_id"] for m in out if m.get("deleted_at")]
    if gone:                    # P192: who removed each, signed — the gap's explanation
        from emporia import deletion
        with rs.DB_LOCK, rs.get_db() as conn:
            removals = deletion.removals_for(conn, "room_message", gone)
        for m in out:
            if m["message_id"] in removals:
                m["removed"] = removals[m["message_id"]]
    return {"messages": out, "count": len(msgs), "untrusted": room_post.UNTRUSTED,
            "next_since": max((m["seq"] for m in out if m.get("seq") is not None), default=after_seq)}
@router.get("/rooms/{room_id}/chain")
async def get_room_chain(room_id: str, request: Request):
    """Chain-integrity verification for a room — the fix direction docs/security-log.md
    has recorded since P19: "the relay signs each chain head with its
    federation keypair, a route serves it, and clients recompute and verify on
    read." Mirrors GET /sessions/{id}/audit (session_audit.verify_chain(),
    signed the same way GET /federation/v1/posts signs a batch —
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


@router.post("/rooms/{room_id}/close")
async def close_room(room_id: str, req: SignedActRequest, request: Request):
    """The creator closes its room, by its own signed act (`wire.signed_room_close_payload`): every other member is
    removed, nobody joins or is invited again, and the history stays readable. Final: there is no reopening. No
    refund moves; a paid member's access ends with the room."""
    room = rooms_db.get_room(room_id, db_path=rs.DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    rs._require_caller_is(room.creator_id, request)
    rs._enforce_agent_rate_limit(room.creator_id)
    if is_closed(room_id):
        raise HTTPException(409, "This room is already closed")
    signed = signed_room_close_payload(room_id, req.at, rs.AGENT_REGISTRY.get_stable_id(room.creator_id) or "")
    _require_creator_signed(room.creator_id, signed, req.at, req.signature)
    removed = [m for m in room.members if m != room.creator_id]
    for member in removed:
        rooms_db.remove_member(room_id, member, db_path=rs.DATABASE_PATH)
        entitlements.revoke("room", room_id, member, reason="closed", db_path=rs.DATABASE_PATH)
        rooms_db.remove_invite(room_id, member, db_path=rs.DATABASE_PATH)
    _record_act(room_id, "close", None, room.creator_id, req.at, signed, req.signature)
    for member in removed:
        await rs.broadcast_to_agent(member, {"type": "room_closed", "room_id": room_id, "room_name": room.name})
    await rs.broadcast_to_session(f"room:{room_id}", {"type": "room_closed", "room_id": room_id})
    return {"status": "closed", "room_id": room_id, "removed": removed}
