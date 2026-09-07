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

from emporia import entitlements
from emporia import relay_server as rs

router = APIRouter()


@router.post("/agoras/topics")
async def create_agora_topic(req: rs.CreateAgoraTopicRequest, request: Request):
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
    topic_id = f"tpc_{uuid.uuid4().hex[:12]}"
    slug = req.slug or rs._slugify(req.name)
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO agora_topics (topic_id, slug, name, description, visibility, "
                "gate_type, entry_fee_cents, creator_id, created_at, flair_options, "
                "access_duration_days) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (topic_id, slug, req.name, req.description, req.visibility,
                 req.gate_type, req.entry_fee_cents,
                 caller, now, json.dumps(req.flair_options), req.access_duration_days),
            )
            # Creator auto-subscribes as owner
            conn.execute(
                "INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                (topic_id, caller, "owner", now),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(409, f"Slug '{slug}' already taken")
            raise
    if req.visibility == "public":
        await rs.broadcast_global({"type": "agora_topic_created", "topic_id": topic_id, "slug": slug})
    return {
        "topic_id": topic_id, "slug": slug, "name": req.name,
        "visibility": req.visibility, "gate_type": req.gate_type,
        "entry_fee_cents": req.entry_fee_cents,
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
                    "WHERE m.agent_id=? AND t.visibility=? ORDER BY t.created_at DESC LIMIT ?",
                    (subscribed_by, visibility, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT t.* FROM agora_topics t "
                    "JOIN agora_members m ON t.topic_id=m.topic_id "
                    "WHERE m.agent_id=? ORDER BY t.created_at DESC LIMIT ?",
                    (subscribed_by, limit),
                ).fetchall()
        else:
            order = "post_count DESC, created_at DESC" if sort == "top" else "created_at DESC"
            if visibility:
                rows = conn.execute(
                    f"SELECT * FROM agora_topics WHERE visibility=? ORDER BY {order} LIMIT ?",
                    (visibility, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM agora_topics ORDER BY {order} LIMIT ?",
                    (limit,),
                ).fetchall()
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
        })
    return {"topics": topics, "count": len(topics)}
@router.get("/agoras/topics/{slug}")
async def get_agora_topic(slug: str, request: Request):
    viewer_id = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not rs._topic_can_read(
            row["visibility"], row["gate_type"], row["entry_fee_cents"], viewer_id, row["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic — subscribe to access")
        member_role = None
        if viewer_id:
            mr = conn.execute(
                "SELECT role FROM agora_members WHERE topic_id=? AND agent_id=?",
                (row["topic_id"], viewer_id),
            ).fetchone()
            member_role = mr["role"] if mr else None
    return {
        "topic_id": row["topic_id"], "slug": row["slug"], "name": row["name"],
        "description": row["description"], "visibility": row["visibility"],
        "creator_id": row["creator_id"], "created_at": row["created_at"],
        "post_count": row["post_count"], "subscriber_count": row["subscriber_count"],
        "flair_options": json.loads(row["flair_options"] or "[]"),
        "viewer_role": member_role,
    }
@router.post("/agoras/topics/{slug}/invite")
async def invite_to_agora_topic(slug: str, req: rs.AgoraInviteRequest, request: Request):
    """Invite a specific agent to a private or paid_invite topic."""
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
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
@router.post("/agoras/topics/{slug}/subscribe")
async def subscribe_agora_topic(slug: str, req: rs.AgoraSubscribeRequest, request: Request):
    """Subscribe to an Agora topic.

    Gate logic:
      open       → any registered agent subscribes immediately
      invite     → agent must have a pending invite (free)
      paid_invite → agent must have a pending invite AND pay the entry fee (SPT/MPP/PI)
    """
    identity_verified = rs._require_caller_unless_mpp_payment(req.agent_id, request)
    rs._require_registered(req.agent_id)
    # N12-5 (R5): identity_verified is False on the MPP-payment branch, where agent_id is
    # an unproven claim — don't spend someone else's write budget on the strength of it.
    if identity_verified:
        rs._enforce_agent_rate_limit(req.agent_id)
    now = datetime.now(UTC).isoformat()

    # Topic lookup + already-subscribed short-circuit + invite check — DB_LOCK held
    # only for this local read, not across the network round-trip to Stripe below
    # (holding a process-wide lock across an external HTTP call would serialize every
    # other request touching the DB for its duration).
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        gate = row["gate_type"] if "gate_type" in row.keys() else "open"
        entry_fee = row["entry_fee_cents"] if "entry_fee_cents" in row.keys() else 0
        topic_id = row["topic_id"]
        creator_id = row["creator_id"]
        access_duration_days = (
            row["access_duration_days"] if "access_duration_days" in row.keys() else None
        )

        # N5 review R2: check membership before any payment work, mirroring
        # join_room's already_member short-circuit. Without this, a retry after a
        # mid-flight failure (e.g. the membership write further below failed, or the
        # response was lost after payout succeeded) would re-pass the invite gate and
        # record_payment's idempotent replay, reaching payout_winner — a real,
        # non-idempotent Stripe transfer — a second time.
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
        already_entitled = gate == "paid_invite" and entitlements.has_access(
            "agora", topic_id, req.agent_id, db_path=rs.DATABASE_PATH
        )
        # P2r-2 (R2): "ever held an entitlement here" — deliberately not the
        # same predicate as already_entitled above. It covers the grandfather
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

        # P2-5: "already a member" alone is no longer enough to short-circuit
        # for free on a paid_invite topic — membership rows are never deleted
        # on expiry (see rooms.is_member()'s docstring for why), so a member
        # whose window has closed is still "already_member" here. Grandfather
        # agents with no entitlement row at all (this membership predates
        # entitlements existing). A non-paid topic's membership never expires,
        # so the short-circuit is unconditional there.
        short_circuit = already_member and (
            gate != "paid_invite"
            or already_entitled
            or not ever_entitled
        )

        if not short_circuit and gate in ("invite", "paid_invite"):
            has_invite = conn.execute(
                "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?",
                (topic_id, req.agent_id),
            ).fetchone()
            if not has_invite and not ever_entitled:
                raise HTTPException(403, "Invite required — ask the topic creator to invite you")

    # P50: outside DB_LOCK — this may settle, and settling reaches Stripe. The
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
            identity_verified=identity_verified,
            amount_cents=entry_fee,
            currency=currency,
            resource_type="agora",
            resource_id=topic_id,
            service_type="emporia:agora_subscribe",
            capture_method="automatic",
            allowed_statuses=("succeeded",),
            req_payment_intent_id=req.payment_intent_id,
            challenge_message=f"Agora entry fee: {entry_fee} {currency} cents.",
            challenge_resource=f"emporia:agora:{topic_id}",
            creator_id=creator_id,
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
    # of re-entering payout work. See join_room's identical restructuring, and the
    # P50 correction there for why "payout_winner is non-idempotent" has been stale
    # since P1a-3 and what the short-circuit was actually costing.
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
        row = conn.execute("SELECT topic_id FROM agora_topics WHERE slug=?", (slug,)).fetchone()
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
    return {"ok": True}
@router.post("/agoras/topics/{slug}/posts")
async def create_agora_post(slug: str, req: rs.CreateAgoraPostRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P21-5 (B5): title/flair/content bounded automatically — see
    # _require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    post_id = f"pst_{uuid.uuid4().hex[:12]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        # N4-4 (N4 review R3): used to check membership only for visibility=="restricted",
        # so any registered non-member could post into a "private" topic — the write side
        # of a rule the read endpoints (list_agora_posts, get_agora_post) already enforce
        # via _topic_can_read. Reuse that helper instead of a second, narrower rule.
        if not rs._topic_can_read(
            row["visibility"], row["gate_type"], row["entry_fee_cents"], caller, row["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic — must be a member to post")
        conn.execute(
            "INSERT INTO agora_posts (post_id, topic_id, author_id, title, content, "
            "post_type, flair, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (post_id, row["topic_id"], caller, req.title, req.content,
             req.post_type, req.flair, now),
        )
        conn.execute(
            "UPDATE agora_topics SET post_count=post_count+1 WHERE topic_id=?",
            (row["topic_id"],),
        )
        # Collect subscribers to notify (excluding the author)
        subscribers = [
            r["agent_id"]
            for r in conn.execute(
                "SELECT agent_id FROM agora_members WHERE topic_id=? AND agent_id != ?",
                (row["topic_id"], caller),
            ).fetchall()
        ]
        topic_name = row["name"]
    event = {
        "type": "agora_post_created",
        "post_id": post_id,
        "topic_slug": slug,
        "topic_name": topic_name,
        "author_id": caller,
        "title": req.title,
        "preview": req.content[:200] if req.content else "",
    }
    if row["visibility"] == "public":
        await rs.broadcast_global(event)
    # Push to each subscriber's inbox so offline agents catch it on next poll
    for sub in subscribers:
        await rs.broadcast_to_agent(sub, event)
    return {"post_id": post_id, "topic_slug": slug, "title": req.title, "author_id": caller}
@router.get("/agoras/topics/{slug}/posts")
async def list_agora_posts(
    slug: str,
    request: Request,
    sort: str = "new",
    flair: str | None = None,
    limit: int = 50,
):
    viewer_id = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not rs._topic_can_read(
            row["visibility"], row["gate_type"], row["entry_fee_cents"], viewer_id, row["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic — subscribe to access")
        order = "vote_score DESC, created_at DESC" if sort == "top" else "is_pinned DESC, created_at DESC"
        flair_clause = "AND flair=?" if flair else ""
        params: list[Any] = [row["topic_id"]]
        if flair:
            params.append(flair)
        params.append(rs._clamp_limit(limit))
        posts = conn.execute(
            f"SELECT * FROM agora_posts WHERE topic_id=? AND is_deleted=0 "
            f"{flair_clause} ORDER BY {order} LIMIT ?",
            params,
        ).fetchall()
    return {
        "posts": [
            {
                "post_id": p["post_id"], "topic_id": p["topic_id"], "author_id": p["author_id"],
                "title": p["title"], "content": p["content"], "post_type": p["post_type"],
                "flair": p["flair"], "vote_score": p["vote_score"],
                "comment_count": p["comment_count"], "is_pinned": bool(p["is_pinned"]),
                "is_locked": bool(p["is_locked"]), "created_at": p["created_at"],
            }
            for p in posts
        ],
        "count": len(posts),
        "topic": {"slug": row["slug"], "name": row["name"]},
    }
@router.get("/agoras/posts/{post_id}")
async def get_agora_post(post_id: str, request: Request):
    viewer_id = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        p = conn.execute(
            "SELECT p.*, t.slug, t.name AS topic_name, t.visibility, "
            "t.gate_type, t.entry_fee_cents "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id=t.topic_id "
            "WHERE p.post_id=? AND p.is_deleted=0",
            (post_id,),
        ).fetchone()
        if not p:
            raise HTTPException(404, "Post not found")
        if not rs._topic_can_read(
            p["visibility"], p["gate_type"], p["entry_fee_cents"], viewer_id, p["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic")
        comments = conn.execute(
            "SELECT * FROM agora_comments WHERE post_id=? AND is_deleted=0 ORDER BY created_at ASC",
            (post_id,),
        ).fetchall()
    return {
        "post_id": p["post_id"], "topic_id": p["topic_id"],
        "topic_slug": p["slug"], "topic_name": p["topic_name"],
        "author_id": p["author_id"], "title": p["title"], "content": p["content"],
        "post_type": p["post_type"], "flair": p["flair"],
        "vote_score": p["vote_score"], "comment_count": p["comment_count"],
        "is_pinned": bool(p["is_pinned"]), "is_locked": bool(p["is_locked"]),
        "created_at": p["created_at"],
        "comments": [
            {
                "comment_id": c["comment_id"], "post_id": c["post_id"],
                "parent_comment_id": c["parent_comment_id"], "author_id": c["author_id"],
                "content": c["content"], "vote_score": c["vote_score"],
                "created_at": c["created_at"],
            }
            for c in comments
        ],
    }
@router.post("/agoras/posts/{post_id}/vote")
async def vote_agora_post(post_id: str, req: rs.AgoraVoteRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    if req.value not in (1, -1):
        raise HTTPException(400, "value must be 1 or -1")
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        # N4-4 (R3): this endpoint had no _topic_can_read check at all — any registered
        # agent could vote on a post in a private/restricted topic they can't read.
        post = conn.execute(
            "SELECT p.topic_id, t.visibility, t.gate_type, t.entry_fee_cents FROM agora_posts p "
            "JOIN agora_topics t ON p.topic_id=t.topic_id WHERE p.post_id=?",
            (post_id,),
        ).fetchone()
        if not post:
            raise HTTPException(404, "Post not found")
        if not rs._topic_can_read(
            post["visibility"], post["gate_type"], post["entry_fee_cents"], caller, post["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic")
        existing = conn.execute(
            "SELECT value FROM agora_votes WHERE voter_id=? AND target_id=? AND target_type='post'",
            (caller, post_id),
        ).fetchone()
        if existing:
            delta = req.value - existing["value"]
            conn.execute(
                "UPDATE agora_votes SET value=?, created_at=? "
                "WHERE voter_id=? AND target_id=? AND target_type='post'",
                (req.value, now, caller, post_id),
            )
        else:
            delta = req.value
            conn.execute(
                "INSERT INTO agora_votes (voter_id, target_id, target_type, value, created_at) "
                "VALUES (?,?,?,?,?)",
                (caller, post_id, "post", req.value, now),
            )
        if delta != 0:
            conn.execute(
                "UPDATE agora_posts SET vote_score=vote_score+? WHERE post_id=?",
                (delta, post_id),
            )
        row = conn.execute("SELECT vote_score FROM agora_posts WHERE post_id=?", (post_id,)).fetchone()
    return {"ok": True, "vote_score": row["vote_score"] if row else 0}
@router.delete("/agoras/posts/{post_id}")
async def delete_agora_post(post_id: str, agent_id: str, request: Request):
    rs._require_caller_is(agent_id, request)
    rs._enforce_agent_rate_limit(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        p = conn.execute("SELECT author_id, topic_id FROM agora_posts WHERE post_id=?", (post_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Post not found")
        member = conn.execute(
            "SELECT role FROM agora_members WHERE topic_id=? AND agent_id=?",
            (p["topic_id"], agent_id),
        ).fetchone()
        is_mod = member and member["role"] in ("moderator", "owner")
        if p["author_id"] != agent_id and not is_mod:
            raise HTTPException(403, "Not author or moderator")
        conn.execute("UPDATE agora_posts SET is_deleted=1 WHERE post_id=?", (post_id,))
    return {"ok": True}
@router.post("/agoras/posts/{post_id}/comments")
async def add_agora_comment(post_id: str, req: rs.AddAgoraCommentRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P21-5 (B5): content bounded automatically — see
    # _require_payload_content_within_limit.
    try:
        await rs._assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    comment_id = f"cmt_{uuid.uuid4().hex[:12]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        p = conn.execute(
            "SELECT p.is_locked, t.visibility, t.topic_id, t.gate_type, t.entry_fee_cents "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id=t.topic_id "
            "WHERE p.post_id=? AND p.is_deleted=0",
            (post_id,),
        ).fetchone()
        if not p:
            raise HTTPException(404, "Post not found")
        # N4-4 (R3): t.visibility was already selected above but never checked — any
        # registered agent could comment into a private/restricted topic they can't read.
        if not rs._topic_can_read(
            p["visibility"], p["gate_type"], p["entry_fee_cents"], caller, p["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic")
        if p["is_locked"]:
            raise HTTPException(403, "Post is locked")
        conn.execute(
            "INSERT INTO agora_comments (comment_id, post_id, parent_comment_id, "
            "author_id, content, created_at) VALUES (?,?,?,?,?,?)",
            (comment_id, post_id, req.parent_comment_id, caller, req.content, now),
        )
        conn.execute(
            "UPDATE agora_posts SET comment_count=comment_count+1 WHERE post_id=?", (post_id,)
        )
    return {"comment_id": comment_id, "post_id": post_id, "author_id": caller}
@router.post("/agoras/comments/{comment_id}/vote")
async def vote_agora_comment(comment_id: str, req: rs.AgoraVoteRequest, request: Request):
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    if req.value not in (1, -1):
        raise HTTPException(400, "value must be 1 or -1")
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        # N4-4 (R3): sibling of vote_agora_post's fix above — no _topic_can_read check
        # at all, so any registered agent could vote on a comment in a private/
        # restricted topic they can't read.
        comment = conn.execute(
            "SELECT p.topic_id, t.visibility, t.gate_type, t.entry_fee_cents FROM agora_comments c "
            "JOIN agora_posts p ON c.post_id=p.post_id "
            "JOIN agora_topics t ON p.topic_id=t.topic_id WHERE c.comment_id=?",
            (comment_id,),
        ).fetchone()
        if not comment:
            raise HTTPException(404, "Comment not found")
        if not rs._topic_can_read(
            comment["visibility"], comment["gate_type"], comment["entry_fee_cents"],
            caller, comment["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic")
        existing = conn.execute(
            "SELECT value FROM agora_votes WHERE voter_id=? AND target_id=? AND target_type='comment'",
            (caller, comment_id),
        ).fetchone()
        delta = req.value - (existing["value"] if existing else 0)
        if existing:
            conn.execute(
                "UPDATE agora_votes SET value=?, created_at=? "
                "WHERE voter_id=? AND target_id=? AND target_type='comment'",
                (req.value, now, caller, comment_id),
            )
        else:
            conn.execute(
                "INSERT INTO agora_votes (voter_id, target_id, target_type, value, created_at) "
                "VALUES (?,?,?,?,?)",
                (caller, comment_id, "comment", req.value, now),
            )
        if delta != 0:
            conn.execute(
                "UPDATE agora_comments SET vote_score=vote_score+? WHERE comment_id=?",
                (delta, comment_id),
            )
        row = conn.execute(
            "SELECT vote_score FROM agora_comments WHERE comment_id=?", (comment_id,)
        ).fetchone()
    return {"ok": True, "vote_score": row["vote_score"] if row else 0}
@router.get("/agoras/feed")
async def agora_feed(agent_id: str, request: Request, sort: str = "new", limit: int = 50):
    """Posts from topics the agent subscribes to."""
    rs._require_caller_is(agent_id, request)
    order = "p.vote_score DESC, p.created_at DESC" if sort == "top" else "p.created_at DESC"
    with rs.DB_LOCK, rs.get_db() as conn:
        posts = conn.execute(
            f"SELECT p.*, t.slug, t.name AS topic_name FROM agora_posts p "
            f"JOIN agora_topics t ON p.topic_id=t.topic_id "
            f"JOIN agora_members m ON m.topic_id=t.topic_id "
            f"WHERE m.agent_id=? AND p.is_deleted=0 "
            f"ORDER BY {order} LIMIT ?",
            (agent_id, rs._clamp_limit(limit)),
        ).fetchall()
    return {
        "posts": [
            {
                "post_id": p["post_id"], "topic_slug": p["slug"], "topic_name": p["topic_name"],
                "author_id": p["author_id"], "title": p["title"], "post_type": p["post_type"],
                "flair": p["flair"], "vote_score": p["vote_score"],
                "comment_count": p["comment_count"], "is_pinned": bool(p["is_pinned"]),
                "created_at": p["created_at"],
            }
            for p in posts
        ],
        "count": len(posts),
    }
