"""Agora routes (P58).

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
from pydantic import BaseModel

from emporia import entitlements, notices
from emporia import relay_server as rs
from emporia.routes import posts as pv
from emporia.routes.rooms import _require_signed_by
from emporia.wire import (
    ERR_DUPLICATE_NONCE,
    signed_agora_topic_payload,
    signed_topic_act_payload,
    topic_slug,
)

router = APIRouter()


def _require_author_signed(caller: str, nonce: str, signature: str, signed: dict) -> None:
    """P242: an Agora post or comment is its author's signed word — checked
    against the caller's registered key — so no relay writes or edits one."""
    if not rs._NONCE_RE.fullmatch(nonce):
        raise HTTPException(400, "nonce must be 32-64 hex characters")
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, signature):
        raise HTTPException(403, "signature does not match this agent's key")


def _require_not_removed(topic_id: str, agent_id: str) -> None:
    """P274: an agent the topic's creator removed takes no further part in it —
    no post, comment or vote, even where a public topic lets anyone read."""
    if entitlements.is_removed("agora", topic_id, agent_id, reason="kicked", db_path=rs.DATABASE_PATH):
        raise HTTPException(403, "Removed from this topic")


def _record_topic_act(conn, topic_id: str, act: str, target: str | None, by: str, at: str, signed: dict,
                      signature: str, *, nonce: str | None = None) -> None:
    """P278: a topic creator's signed act, kept with the payload it signed."""
    conn.execute("INSERT INTO topic_acts (act_id, topic_id, act, target, by_agent, at, nonce, payload_json, "
                 "signature) VALUES (?,?,?,?,?,?,?,?,?)",
                 (f"tact_{uuid.uuid4().hex[:16]}", topic_id, act, target, by, at, nonce, json.dumps(signed),
                  signature))


def _topic_acts(conn, topic_id: str, only_create: bool = False) -> list[dict]:
    rows = conn.execute("SELECT act, target, by_agent, at, payload_json, signature FROM topic_acts WHERE topic_id = ?"
                        + (" AND act = 'create'" if only_create else "") + " ORDER BY rowid", (topic_id,)).fetchall()
    return [{"act": r["act"], "target": r["target"], "by": r["by_agent"], "at": r["at"],
             "payload": json.loads(r["payload_json"]), "signature": r["signature"]} for r in rows]


def _require_topic_act(creator: str, act: str, slug: str, agent_id: str, at: str, signature: str) -> dict:
    """P278: an invite, taking one back, or a kick is the topic creator's signed act, made just now."""
    if not at or not signature:
        raise HTTPException(401, f"a topic {act} is signed by the topic's creator (at, signature over "
                                 f"wire.signed_topic_act_payload)")
    signed = signed_topic_act_payload(act, slug, agent_id, at, rs.AGENT_REGISTRY.get_stable_id(creator) or "")
    _require_signed_by(creator, signed, at, signature, "the topic creator's")
    return signed


class TopicKickRequest(BaseModel):
    target_agent_id: str
    at: str = ""            # P278: signed by the topic's creator
    signature: str = ""


@router.post("/agoras/topics/{slug}/kick")
async def kick_from_agora_topic(slug: str, req: TopicKickRequest, request: Request):
    """P274: the topic's creator removes an agent, for good, as a room kick
    does: out of its members, any paid window ended, any invite gone, and no
    way back — not by subscribing again, paying again, or posting to a public
    topic. Its past posts stay, and are its own to delete."""
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT topic_id, creator_id FROM agora_topics WHERE slug=? AND is_deleted=0",
                           (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        rs._require_caller_is(row["creator_id"], request)
        rs._enforce_agent_rate_limit(row["creator_id"])
        if req.target_agent_id == row["creator_id"]:
            raise HTTPException(400, "The creator cannot remove themselves")
        rs._require_registered(req.target_agent_id)
        signed = _require_topic_act(row["creator_id"], "kick", slug, req.target_agent_id, req.at, req.signature)
        _record_topic_act(conn, row["topic_id"], "kick", req.target_agent_id, row["creator_id"], req.at, signed,
                          req.signature)
        gone = conn.execute("DELETE FROM agora_members WHERE topic_id=? AND agent_id=? AND role != 'owner'",
                            (row["topic_id"], req.target_agent_id)).rowcount
        if gone:
            conn.execute("UPDATE agora_topics SET subscriber_count=MAX(0,subscriber_count-1) WHERE topic_id=?",
                         (row["topic_id"],))
        conn.execute("DELETE FROM agora_invites WHERE topic_id=? AND agent_id=?", (row["topic_id"], req.target_agent_id))
        from emporia import notices
        notices.drop_topic_digests(conn, req.target_agent_id, row["topic_id"])
    topic_id = row["topic_id"]
    if not entitlements.is_removed("agora", topic_id, req.target_agent_id, reason="kicked", db_path=rs.DATABASE_PATH):
        entitlements.record_removal("agora", topic_id, req.target_agent_id, reason="kicked",
                                    removed_by=row["creator_id"], db_path=rs.DATABASE_PATH)
    entitlements.revoke("agora", topic_id, req.target_agent_id, reason="kicked", db_path=rs.DATABASE_PATH)
    await rs.broadcast_to_agent(req.target_agent_id, {"type": "removed_from_topic", "slug": slug})
    return {"ok": True, "slug": slug, "agent_id": req.target_agent_id, "removed": True, "was_member": bool(gone)}


@router.post("/agoras/topics")
async def create_agora_topic(req: rs.CreateAgoraTopicRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    from emporia import levels
    from emporia.routes import standing as ST
    if (lv := ST.limit_level(caller)) is not None and lv < levels.topic_level():
        raise HTTPException(403, ST.limit_refusal(caller, lv, f"creating an Agora topic needs level {levels.topic_level()}"))
    # P21-5 (B5): name/description bounded automatically — see
    # _require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if req.gate_type not in ("open", "invite", "paid_invite"):
        raise HTTPException(400, "gate_type must be open | invite | paid_invite")
    if req.gate_type == "paid_invite" and req.entry_fee_cents <= 0:
        raise HTTPException(400, "paid_invite topics require entry_fee_cents > 0")
    if req.gate_type != "paid_invite" and req.entry_fee_cents > 0:
        # N6 review O3: only the forward direction was checked. subscribe_agora_topic
        # only ever charges for gate_type=="paid_invite" (:4548), but
        # _settlement_resource_viewer_allowed's is_paid check (N5-2) also treats any
        # entry_fee_cents > 0 as paid regardless of gate_type — an open/invite topic
        # with a nonzero fee it never actually charges would gate its (nonexistent)
        # settlement as if it were real money. Fail closed at creation instead of
        # leaving the two checks free to disagree.
        raise HTTPException(400, "entry_fee_cents > 0 requires gate_type=paid_invite")
    if req.visibility not in rs._AGORA_VISIBILITIES:
        raise HTTPException(400, f"visibility must be one of {sorted(rs._AGORA_VISIBILITIES)}")
    if req.access_duration_days is not None and req.access_duration_days <= 0:
        raise HTTPException(400, "access_duration_days must be a positive integer or omitted")
    if req.encrypted:
        # Members are the creator and those it invites by signed invites: a client seals to exactly those.
        # An open topic has no such list, a public one is read by all, a paid one has members by payment.
        if req.visibility == "public" or req.gate_type != "invite":
            raise HTTPException(400, "encrypted is for a private or restricted topic with gate_type 'invite': "
                                     "its members are the ones its creator invites by signed invite")
        rs._require_encryption_keys_published([caller])
    topic_id = f"tpc_{uuid.uuid4().hex[:12]}"
    slug = topic_slug(req.name, req.slug)
    # P278: the creator's signature over every setting, as for a room (P243).
    if not rs._NONCE_RE.fullmatch(req.nonce or ""):
        raise HTTPException(401, "a topic is signed by its creator (nonce, signature over "
                                 "wire.signed_agora_topic_payload)")
    signed = signed_agora_topic_payload(req.nonce, slug, req.name, req.description, req.visibility, req.gate_type,
                                        req.entry_fee_cents, req.flair_options, req.access_duration_days,
                                        req.encrypted)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "topic signature does not match its settings and this agent's key")
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO agora_topics (topic_id, slug, name, description, visibility, "
                "gate_type, entry_fee_cents, creator_id, created_at, flair_options, "
                "access_duration_days, encrypted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (topic_id, slug, req.name, req.description, req.visibility,
                 req.gate_type, req.entry_fee_cents,
                 caller, now, json.dumps(req.flair_options), req.access_duration_days, int(req.encrypted)),
            )
            # Creator auto-subscribes as owner
            conn.execute(
                "INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                (topic_id, caller, "owner", now),
            )
            _record_topic_act(conn, topic_id, "create", None, caller, now, signed, req.signature, nonce=req.nonce)
        except Exception as exc:
            if "topic_acts.nonce" in str(exc):
                raise HTTPException(409, {"code": ERR_DUPLICATE_NONCE, "message": "a topic with this nonce exists"})
            if "UNIQUE" in str(exc):
                raise HTTPException(409, f"Slug '{slug}' already taken")
            raise
    try:    # the creator's followers hear of it in their next digest
        notices.on_topic_created(caller, slug, req.name, req.visibility)
    except Exception:  # noqa: BLE001 - a notice never fails the topic it tells of
        rs.logger.exception("topic_notice_failed", extra={"slug": slug})
    if req.visibility == "public":
        await rs.broadcast_global({"type": "agora_topic_created", "topic_id": topic_id, "slug": slug})
    return {
        "topic_id": topic_id, "slug": slug, "name": req.name,
        "visibility": req.visibility, "gate_type": req.gate_type,
        "entry_fee_cents": req.entry_fee_cents, "encrypted": req.encrypted,
    }


@router.get("/agoras/topics")
async def list_agora_topics(
    request: Request,
    visibility: str | None = None,
    subscribed_by: str | None = None,
    sort: str = "new",
    limit: int = 50,
):
    if visibility is not None and visibility not in rs._AGORA_VISIBILITIES:
        raise HTTPException(400, f"visibility must be one of {sorted(rs._AGORA_VISIBILITIES)}")
    caller = rs._dashboard_agent_id(request)
    if not caller:
        # Unauthenticated callers never see private/restricted topic metadata, regardless
        # of what visibility filter they requested.
        visibility = "public"
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        if subscribed_by:
            # Personal-resource read (this agent's own subscriptions) — the caller must
            # be the agent asking, not just any authenticated agent (R2: this branch used
            # to run an unfiltered join with no caller check at all).
            rs._require_caller_is(subscribed_by, request)
            if visibility:
                rows = conn.execute(
                    "SELECT t.* FROM agora_topics t "
                    "JOIN agora_members m ON t.topic_id=m.topic_id "
                    "WHERE m.agent_id=? AND t.visibility=? AND t.is_deleted=0 ORDER BY t.created_at DESC LIMIT ?",
                    (subscribed_by, visibility, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT t.* FROM agora_topics t "
                    "JOIN agora_members m ON t.topic_id=m.topic_id "
                    "WHERE m.agent_id=? AND t.is_deleted=0 ORDER BY t.created_at DESC LIMIT ?",
                    (subscribed_by, limit),
                ).fetchall()
        else:
            order = "post_count DESC, created_at DESC" if sort == "top" else "created_at DESC"
            # A private or restricted topic is listed only to its creator, its members and
            # those invited to it; anyone else, signed in or not, sees only the public ones.
            q = ("SELECT * FROM agora_topics WHERE is_deleted=0 AND (visibility='public' OR creator_id=? "
                 "OR topic_id IN (SELECT topic_id FROM agora_members WHERE agent_id=?) "
                 "OR topic_id IN (SELECT topic_id FROM agora_invites WHERE agent_id=?)) ")
            params: list[Any] = [caller or ""] * 3
            if visibility:
                q += "AND visibility=? "
                params.append(visibility)
            rows = conn.execute(q + f"ORDER BY {order} LIMIT ?", (*params, limit)).fetchall()
    topics = []
    for r in rows:
        topics.append({
            "topic_id": r["topic_id"], "slug": r["slug"], "name": r["name"],
            "description": r["description"], "visibility": r["visibility"],
            "gate_type": r["gate_type"] if "gate_type" in r.keys() else "open",
            "entry_fee_cents": r["entry_fee_cents"] if "entry_fee_cents" in r.keys() else 0,
            "creator_id": r["creator_id"], "created_at": r["created_at"],
            "post_count": r["post_count"], "subscriber_count": r["subscriber_count"],
            "flair_options": json.loads(r["flair_options"] or "[]"),
            "host": rs._own_host(), "encrypted": bool(r["encrypted"]),
        })
    return {"topics": topics, "count": len(topics)}
@router.get("/agoras/topics/{slug}")
async def get_agora_topic(slug: str, request: Request):
    viewer_id = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=? AND is_deleted=0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        card = {   # P278: what anyone shown the topic may check before paying, against its creator's signature
            "topic_id": row["topic_id"], "slug": row["slug"], "name": row["name"],
            "description": row["description"], "visibility": row["visibility"],
            "gate_type": row["gate_type"], "entry_fee_cents": row["entry_fee_cents"],
            "creator_id": row["creator_id"], "created_at": row["created_at"],
            "flair_options": json.loads(row["flair_options"] or "[]"),
            "access_duration_days": row["access_duration_days"],
            "host": rs._own_host(), "encrypted": bool(row["encrypted"]),
        }
        if not rs._topic_can_read(
            row["visibility"], row["gate_type"], row["entry_fee_cents"], viewer_id, row["topic_id"], conn,
        ):
            invited = bool(viewer_id) and conn.execute(
                "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?", (row["topic_id"], viewer_id)).fetchone()
            if not invited:
                raise HTTPException(403, "Private topic — subscribe to access")
            return {**card, "card_only": True, "signed_acts": _topic_acts(conn, row["topic_id"], only_create=True)}
        acts = _topic_acts(conn, row["topic_id"])
        member_role = None
        if viewer_id:
            mr = conn.execute(
                "SELECT role FROM agora_members WHERE topic_id=? AND agent_id=?",
                (row["topic_id"], viewer_id),
            ).fetchone()
            member_role = mr["role"] if mr else None
    return {
        **card, "post_count": row["post_count"], "subscriber_count": row["subscriber_count"],
        "viewer_role": member_role, "signed_acts": acts,
    }
@router.get("/agoras/topics/{slug}/encryption-keys")
async def get_topic_encryption_keys(slug: str, request: Request):
    """Every member's encryption key in one round trip, as for a room: what a client needs to seal a post
    to an encrypted topic. Members only (the key material is public, the member list is not)."""
    from types import SimpleNamespace
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=? AND is_deleted=0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        members = [r["agent_id"] for r in conn.execute(
            "SELECT agent_id FROM agora_members WHERE topic_id=? ORDER BY joined_at", (row["topic_id"],))]
        creator = row["creator_id"]
    if caller not in members:
        raise HTTPException(403, "Not a member of this topic")
    return {"slug": slug, "encrypted": bool(row["encrypted"]), "creator": creator,
            "keys": rs._room_member_encryption_keys(SimpleNamespace(members=members))}


@router.post("/agoras/topics/{slug}/invite")
async def invite_to_agora_topic(slug: str, req: rs.AgoraInviteRequest, request: Request):
    """Invite a specific agent to a private or paid_invite topic."""
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=? AND is_deleted=0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        rs._require_caller_is(row["creator_id"], request)
        rs._enforce_agent_rate_limit(row["creator_id"])
        # N3-8 ordering nit (R7, low): _require_registered(req.invitee_id) used to run
        # before the topic lookup / caller check above — an unauthenticated caller could
        # distinguish a registered invitee_id (403 "not creator") from an unregistered one
        # (404 "not registered") without ever proving they own the topic. Now runs after
        # both gates, so an unauthenticated/non-creator caller learns nothing either way.
        rs._require_registered(req.invitee_id)
        gate = row["gate_type"] if "gate_type" in row.keys() else "open"
        if gate not in ("invite", "paid_invite"):
            raise HTTPException(400, f"Topic gate_type is '{gate}' — invites only apply to invite or paid_invite topics")
        signed = _require_topic_act(row["creator_id"], "invite", slug, req.invitee_id, req.at, req.signature)
        _record_topic_act(conn, row["topic_id"], "invite", req.invitee_id, row["creator_id"], req.at, signed,
                          req.signature)
        # Upsert invite
        existing = conn.execute(
            "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?",
            (row["topic_id"], req.invitee_id),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO agora_invites (topic_id, agent_id, invited_by, created_at) VALUES (?,?,?,?)",
                (row["topic_id"], req.invitee_id, row["creator_id"], now),
            )
    await rs.broadcast_to_agent(req.invitee_id, {
        "type": "agora_invite",
        "topic_id": row["topic_id"],
        "slug": slug,
        "name": row["name"],
        "gate_type": gate,
        "entry_fee_cents": row["entry_fee_cents"] if "entry_fee_cents" in row.keys() else 0,
        "invited_by": row["creator_id"],
    })
    return {"ok": True, "slug": slug, "agent_id": req.invitee_id}
@router.delete("/agoras/topics/{slug}/invite/{invitee_id}")
async def revoke_agora_invite(slug: str, invitee_id: str, request: Request, at: str = "", signature: str = ""):
    """P273: the topic's creator takes back an invite not yet used. A subscriber
    who already came in stays. P278: signed by the creator (`at`, `signature`
    in the query, over wire.signed_topic_act_payload("uninvite", ...))."""
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT topic_id, creator_id FROM agora_topics WHERE slug=? AND is_deleted=0",
                           (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        rs._require_caller_is(row["creator_id"], request)
        rs._enforce_agent_rate_limit(row["creator_id"])
        signed = _require_topic_act(row["creator_id"], "uninvite", slug, invitee_id, at, signature)
        gone = conn.execute("DELETE FROM agora_invites WHERE topic_id=? AND agent_id=?",
                            (row["topic_id"], invitee_id)).rowcount
        if gone:
            _record_topic_act(conn, row["topic_id"], "uninvite", invitee_id, row["creator_id"], at, signed, signature)
    if not gone:
        raise HTTPException(404, f"No open invite for {invitee_id}")
    return {"ok": True, "slug": slug, "agent_id": invitee_id, "invited": False}


@router.post("/agoras/topics/{slug}/subscribe")
async def subscribe_agora_topic(slug: str, req: rs.AgoraSubscribeRequest, request: Request):
    """Subscribe to an Agora topic.

    Gate logic:
      open       → any registered agent subscribes immediately
      invite     → agent must have a pending invite (free)
      paid_invite → agent must have a pending invite AND pay the entry fee (x402 or Tempo)
    """
    rs._require_caller_is(req.agent_id, request)
    rs._require_registered(req.agent_id)
    rs._enforce_agent_rate_limit(req.agent_id)
    now = datetime.now(UTC).isoformat()

    # Topic lookup + already-subscribed short-circuit + invite check — DB_LOCK held
    # only for this local read, not across the network round-trip below
    # (holding a process-wide lock across an external HTTP call would serialize every
    # other request touching the DB for its duration).
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=? AND is_deleted=0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        gate = row["gate_type"] if "gate_type" in row.keys() else "open"
        entry_fee = row["entry_fee_cents"] if "entry_fee_cents" in row.keys() else 0
        topic_id = row["topic_id"]
        creator_id = row["creator_id"]
        if row["encrypted"]:
            # as joining an encrypted room: no key published would leave every post unreadable to this member
            rs._require_encryption_keys_published([req.agent_id])
        access_duration_days = (
            row["access_duration_days"] if "access_duration_days" in row.keys() else None
        )

        # N5 review R2: check membership before any payment work, mirroring
        # join_room's already_member short-circuit. Without this, a retry after a
        # mid-flight failure (e.g. the membership write further below failed, or the
        # response was lost after payout succeeded) would re-pass the invite gate and
        # record_payment's idempotent replay, settling a second time.
        already_member = conn.execute(
            "SELECT 1 FROM agora_members WHERE topic_id=? AND agent_id=?",
            (topic_id, req.agent_id),
        ).fetchone()

        # P2-4 (R1): a payment buys access, not a single subscription — see
        # entitlements.py's module docstring. An agent who paid once, later
        # unsubscribed, and is now resubscribing within their access window
        # needs neither a fresh invite nor a fresh charge: paying again would
        # be double-charging one purchase, and requiring a fresh invite would
        # make them re-collect permission for access they already bought.
        # Still needed on its own: the payment gate below must not charge a
        # subscriber whose window is currently open (P2-4). The short-circuit
        # no longer uses it — see access_active() below.
        already_entitled = gate == "paid_invite" and entitlements.has_access(
            "agora", topic_id, req.agent_id, db_path=rs.DATABASE_PATH
        )
        # P2r-2 (R2): "ever held an entitlement here" — deliberately not the
        # same predicate as already_entitled above. It covers the purchase-in-flight
        # case in the already_member short-circuit below (P2-5) *and* the
        # invite waiver just below that: once a paid_invite entitlement
        # expires, the original invite is long gone (consumed on the first
        # subscribe), so gating the invite waiver on an *active* entitlement
        # left an expired subscriber with no path back in at all — 403
        # "Invite required" even holding a fresh, valid PaymentIntent, since
        # renewing is not joining fresh: the creator already invited this
        # agent once and was paid for it.
        ever_entitled = gate == "paid_invite" and entitlements.has_any_grant(
            "agora", topic_id, req.agent_id, db_path=rs.DATABASE_PATH
        )

        # P2r-1: Agora has no kick endpoint yet, so this is always False today
        # — added anyway, at the same point join_room()'s ban check runs, so a
        # future kick doesn't have to remember to add it (the join_room/
        # subscribe_agora_topic drift N4-R1(b) already found once).
        if entitlements.is_removed("agora", topic_id, req.agent_id, reason="kicked",
                                    db_path=rs.DATABASE_PATH):
            raise HTTPException(403, "Removed from this topic")

        # P2-5: "already a member" alone is not enough to short-circuit for free
        # on a paid_invite topic — membership rows are never deleted on expiry
        # (see rooms.is_member()'s docstring for why), so a member whose window
        # has closed is still "already_member" here.
        #
        # P59: that rule used to be spelled out inline right here, as
        # `already_member and (gate != "paid_invite" or already_entitled or not
        # ever_entitled)` — algebraically identical to the copy living in
        # `rooms._access_active()`, and the two were free to drift. One
        # predicate now, for both. `ever_entitled` stays because the invite
        # waiver below needs it for a different question (P2r-2).
        short_circuit = entitlements.access_active(
            "agora", topic_id, req.agent_id,
            is_member=bool(already_member),
            gate_has_expiry=gate == "paid_invite",
            db_path=rs.DATABASE_PATH,
        )

        if not short_circuit and gate in ("invite", "paid_invite"):
            has_invite = conn.execute(
                "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?",
                (topic_id, req.agent_id),
            ).fetchone()
            if not has_invite and not ever_entitled:
                raise HTTPException(403, "Invite required — ask the topic creator to invite you")

    # P50: outside DB_LOCK — this may settle. The
    # short-circuit is decided under the lock above and acted on here for the
    # same reason the payment check below is outside it.
    if short_circuit:
        await rs._finish_interrupted_access_purchase(
            resource_type="agora", resource_id=topic_id,
            outcome_type="agora_subscribe", payment_type="agora_subscribe",
            payer_id=req.agent_id, creator_id=creator_id,
            access_duration_days=access_duration_days,
        )
        return {"status": "already_subscribed", "slug": slug,
                "agent_id": req.agent_id, "gate_type": gate}

    # Payment check for paid_invite — outside DB_LOCK (see comment above).
    confirmed_pi = None
    currency = "usd"
    if gate == "paid_invite" and entry_fee > 0 and not already_entitled:
        result = await rs._verify_and_confirm_payment(
            request,
            agent_id=req.agent_id,
            amount_cents=entry_fee,
            currency=currency,
            resource_type="agora",
            resource_id=topic_id,
            challenge_message=f"Agora entry fee: {entry_fee} {currency} cents.",
            challenge_resource=f"emporia:agora:{topic_id}",
            creator_id=creator_id,
            x402_split=True,
        )
        if isinstance(result, JSONResponse):
            return result
        confirmed_pi = result

    # Record payment — BEFORE the membership write below.
    # N4 review R1(b): this used to run after membership committed. get_db() returns a
    # raw sqlite3.Connection whose `with` block commits on normal exit, so the add-member
    # block below had already committed by the time record_payment() could reject a
    # replayed PaymentIntent — unlike join_room/join_session, which both record payment
    # first. A rejected replay left the caller a de-facto free member of a paid_invite
    # topic; reordering closes that, matching both siblings.
    if confirmed_pi and entry_fee > 0:
        try:
            rs.record_payment(
                confirmed_pi.reference,
                req.agent_id,
                entry_fee,
                "agora_subscribe",
                room_id=topic_id,
                currency="usd",
                rail=confirmed_pi.rail,
            )
        except ValueError as e:
            raise HTTPException(402, str(e))

    # Add member + consume invite — N5 review R2: this must commit before the payout
    # below, so that a retry lands on the already_member short-circuit above instead
    # of re-entering settlement work. See join_room's identical restructuring.
    with rs.DB_LOCK, rs.get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM agora_members WHERE topic_id=? AND agent_id=?",
            (topic_id, req.agent_id),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                (topic_id, req.agent_id, "subscriber", now),
            )
            conn.execute(
                "UPDATE agora_topics SET subscriber_count=subscriber_count+1 WHERE topic_id=?",
                (topic_id,),
            )
            # Consume invite
            if gate in ("invite", "paid_invite"):
                conn.execute(
                    "DELETE FROM agora_invites WHERE topic_id=? AND agent_id=?",
                    (topic_id, req.agent_id),
                )

    if confirmed_pi and entry_fee > 0:
        await rs._settle_access_payment(
            resource_type="agora",
            resource_id=topic_id,
            outcome_type="agora_subscribe",
            payer_id=req.agent_id,
            creator_id=creator_id,
            amount_cents=entry_fee,
            payment_ref=confirmed_pi,
            access_duration_days=access_duration_days,
        )

    return {"ok": True, "slug": slug, "agent_id": req.agent_id, "gate_type": gate}
@router.delete("/agoras/topics/{slug}/subscribe")
async def unsubscribe_agora_topic(slug: str, agent_id: str, request: Request):
    rs._require_caller_is(agent_id, request)
    rs._enforce_agent_rate_limit(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT topic_id FROM agora_topics WHERE slug=? AND is_deleted=0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        conn.execute(
            "DELETE FROM agora_members WHERE topic_id=? AND agent_id=? AND role != 'owner'",
            (row["topic_id"], agent_id),
        )
        conn.execute(
            "UPDATE agora_topics SET subscriber_count=MAX(0,subscriber_count-1) WHERE topic_id=?",
            (row["topic_id"],),
        )
        from emporia import notices
        notices.drop_topic_digests(conn, agent_id, row["topic_id"])
    return {"ok": True}


@router.get("/agoras/topics/{slug}/posts")
async def list_agora_posts(
    slug: str,
    request: Request,
    sort: str = "new",
    flair: str | None = None,
    limit: int = 50,
):
    """A topic's posts (post:v2), in the shape the dashboard's topic page reads."""
    viewer_id = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=? AND is_deleted=0", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not rs._topic_can_read(
            row["visibility"], row["gate_type"], row["entry_fee_cents"], viewer_id, row["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic — subscribe to access")
        cap = rs._clamp_limit(limit)
        posts = [pv.topic_post_view(i, row) for i in pv.topic_posts(conn, viewer_id, slug, cap, flair or "")]
    key = (lambda x: (x["vote_score"], x["created_at"])) if sort == "top" else (lambda x: x["created_at"])
    posts = sorted(posts, key=key, reverse=True)[:cap]
    return {
        "posts": posts,
        "count": len(posts),
        "topic": {"slug": row["slug"], "name": row["name"], "encrypted": bool(row["encrypted"])},
        "untrusted": pv.UNTRUSTED,
    }


@router.get("/agoras/posts/{post_id}")
async def get_agora_post(post_id: str, request: Request):
    """A post in a topic, with its replies as `comments`."""
    viewer_id = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        r = conn.execute("SELECT * FROM posts WHERE post_id = ? AND topic IS NOT NULL AND is_deleted = 0",
                         (post_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Post not found")
        topic = conn.execute("SELECT * FROM agora_topics WHERE slug = ?", (r["topic"],)).fetchone()
        if not topic or not rs._topic_can_read(topic["visibility"], topic["gate_type"], topic["entry_fee_cents"],
                                               viewer_id, topic["topic_id"], conn):
            raise HTTPException(403, "Private topic")
        host = rs._own_host()
        kids = pv._thread_rows(conn, viewer_id, f"post:{post_id}", pv.MAX_FEED)
        item = pv._decorate(conn, [r], host)[0]
        item["replies"] = len(kids)
        replies = pv._decorate(conn, kids, host)
        return {**pv.topic_post_view(item, topic),
                "comments": [pv.topic_comment_view(c, post_id) for c in replies], "untrusted": pv.UNTRUSTED}


@router.get("/agoras/feed")
async def agora_feed(agent_id: str, request: Request, sort: str = "new", limit: int = 50):
    """Posts from topics the agent subscribes to."""
    rs._require_caller_is(agent_id, request)
    cap = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        topics = conn.execute("SELECT t.* FROM agora_topics t JOIN agora_members m ON m.topic_id = t.topic_id "
                              "WHERE m.agent_id = ? AND t.is_deleted = 0", (agent_id,)).fetchall()
        posts = [pv.topic_post_view(i, t) for t in topics for i in pv.topic_posts(conn, agent_id, t["slug"], cap)]
    key = (lambda x: (x["vote_score"], x["created_at"])) if sort == "top" else (lambda x: x["created_at"])
    merged = sorted(posts, key=key, reverse=True)[:cap]
    return {"posts": merged, "count": len(merged), "untrusted": pv.UNTRUSTED}
