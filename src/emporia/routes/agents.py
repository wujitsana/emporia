"""Agent routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from emporia import moves
from emporia.addresses import address, fingerprint

from emporia import relay_server as rs
from emporia.standing import game_module_types, wins_sql

router = APIRouter()


@router.post("/agents/challenge")
async def create_registration_challenge():
    """Issue a one-time nonce. Include challenge_id + Ed25519 signature in /agents/register
    to prove possession of the private key matching the submitted public_key_hex."""
    challenge_id = f"ch_{secrets.token_urlsafe(16)}"
    nonce = secrets.token_hex(32)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=rs._CHALLENGE_TTL_SECONDS)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO reg_challenges (challenge_id, nonce, created_at, expires_at) VALUES (?,?,?,?)",
            (challenge_id, nonce, now.isoformat(), expires.isoformat()),
        )
        # Prune expired challenges (best-effort, fire-and-forget)
        conn.execute("DELETE FROM reg_challenges WHERE expires_at < ?", (now.isoformat(),))
    return {"challenge_id": challenge_id, "nonce": nonce, "expires_in": rs._CHALLENGE_TTL_SECONDS}
@router.post("/agents/register")
async def register_agent(req: rs.RegisterAgentRequest):
    if not req.public_key_hex:
        raise HTTPException(400, "public_key_hex is required")
    from emporia import relay_access
    if why := relay_access.registration_refusal(req.public_key_hex):   # P186
        raise HTTPException(403, why)
    if rs.REQUIRE_NOUS:
        has_nous = any(c.provider == "nous" and c.token for c in req.identity_claims)
        if not has_nous:
            raise HTTPException(
                403,
                "This relay requires Nous identity verification. "
                "Include a valid Nous JWT in identity_claims[{provider: 'nous', token: '...'}].",
            )
    # Challenge verification — proves the registrant holds the matching private key
    if req.challenge_id and req.challenge_signature:
        rs._consume_challenge(req.challenge_id, req.public_key_hex, req.challenge_signature)
    elif rs.REQUIRE_CHALLENGE:
        raise HTTPException(
            400,
            "This relay requires proof-of-key-possession. "
            "Request a nonce via POST /agents/challenge, sign it with your Ed25519 private key, "
            "and include challenge_id + challenge_signature in this request.",
        )

    try:
        # P53: off the event loop. AgentRegistry.register() verifies identity
        # claims inline, and the Nous provider fetches JWKS with a *blocking*
        # httpx.get carrying a 5s timeout — twice (discovery, then the keyset).
        # A slow or unreachable provider therefore stalled the entire relay for
        # up to 10 seconds per registration attempt, not just that request, and
        # the JWKS cache only stores on success so every attempt paid it again.
        # Same treatment P44 gave the three heaviest read routes, for the same
        # reason: blocking work does not belong on the loop.
        profile = await run_in_threadpool(
            functools.partial(
                rs.AGENT_REGISTRY.register,
                req.agent_id,
                req.public_key_hex,
                req.display_name,
                identity_claims=[c.model_dump() for c in req.identity_claims],
            )
        )
    except ValueError as e:
        msg = str(e)
        status = 409 if "already registered" in msg or "Key rotation" in msg or "different key" in msg else 400
        raise HTTPException(status, msg)

    canonical_id = profile["agent_id"]

    # P16: the X25519 encryption key, if the client published it inline. Runs after
    # AGENT_REGISTRY.register() because the binding is verified against the Ed25519
    # key that call just stored, and goes through the same
    # _validated_encryption_key() as the standalone endpoint so the two paths can't
    # apply different rules. A bad key fails the whole registration rather than
    # succeeding quietly without one — an agent who thinks they published a key but
    # didn't is invisible to their peers, who just see someone who doesn't do
    # encrypted DMs.
    if req.encryption_public_key_hex:
        enc_key = rs._validated_encryption_key(
            canonical_id, req.encryption_public_key_hex, req.encryption_key_signature,
        )
        rs.AGENT_REGISTRY.set_encryption_key(canonical_id, enc_key, req.encryption_key_signature)

    # Auto-provision a Stripe Connected Account in test mode so the agent can
    # receive real (test) payouts without manual Stripe onboarding.
    stripe_account_id: str | None = None
    # P75: mode, not prefix. A restricted test key (rk_test_) is test mode, and
    # a bare sk_test_ check skipped provisioning for it — leaving that operator
    # in a gap where registration provisions nothing and the onboarding route
    # below then waves them into the live-mode flow with a test key.
    from emporia.payments import stripe_key_mode
    if stripe_key_mode() == "test":
        try:
            from emporia.payments import create_connected_account
            acct = await create_connected_account(canonical_id)
            stripe_account_id = acct["stripe_account_id"]
            # P26-4: tag the key space that created it — this block only ever
            # runs under sk_test_, so "test" is a fact here, not an inference.
            rs.AGENT_REGISTRY.set_stripe_account(canonical_id, stripe_account_id, mode="test")
            # P25-2: test mode is the one case where 'active' is correct
            # immediately — Stripe auto-approves a Custom account's
            # capabilities in test mode (create_connected_account()'s own
            # docstring), so there's no webhook to wait for.
            rs.AGENT_REGISTRY.set_stripe_account_status(stripe_account_id, "active")
        except Exception as e:
            # Non-fatal — the agent can still use free sessions and x402 — but
            # never silent (P128): Stripe refused this for every agent on a new
            # Connect platform ("use Accounts v2 or enable Accounts v1 support")
            # and nothing said so until a payout found no account.
            detail = str(e)
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    detail = resp.json().get("error", {}).get("message", detail)
                except ValueError:
                    pass
            rs.logger.warning("stripe_connected_account_not_created", extra={
                "agent_id": canonical_id, "detail": detail[:300]})

    return {
        "status": "registered",
        "agent_id": canonical_id,
        # P47: the agent learns its own permanent identity here. Without it an
        # agent would have to make a second request to find out who it is, and
        # would have no way to notice a handle it re-registered was allocated to
        # a different identity than it expected.
        "stable_id": profile["stable_id"],
        "address": address(canonical_id, rs._own_host()),
        "fingerprint": fingerprint(req.public_key_hex),
        "display_name": profile["display_name"],
        "trust_level": profile["trust_level"],
        "nous_user_id": profile["nous_user_id"],
        "providers_verified": profile["providers_verified"],
        "stripe_account_id": stripe_account_id,
    }
@router.get("/agents")
async def list_agents(request: Request, limit: int = 100, search: str | None = None,
                      include_inactive: bool = False):
    """List registered agents. Optional ?search= filters by agent_id or display_name.

    N3-3 (N3 review R2): this was the identity-enumeration primitive the N2 review itself
    named as the reason every write-endpoint identity-as-proof attack was reachable
    (unauthenticated, uncapped `limit`) — N2 fixed every consumer of an enumerated identity
    and never closed the enumerator. Now requires an authenticated caller; agent discovery
    is a legitimate feature, so this makes it authenticated rather than removed.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        if search:
            pattern = f"%{search}%"
            rows, has_more = rs._fetch_page(
                conn,
                "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
                "nous_user_id, trust_level, identity_providers_json "
                "FROM authorized_agents WHERE (agent_id LIKE ? OR display_name LIKE ?) "
                + ("" if include_inactive else "AND is_active = 1 ")    # P222: one that left is gone
                + "ORDER BY registered_at DESC LIMIT ?",
                (pattern, pattern), limit,
            )
        else:
            rows, has_more = rs._fetch_page(
                conn,
                "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
                "nous_user_id, trust_level, identity_providers_json "
                "FROM authorized_agents " + ("" if include_inactive else "WHERE is_active = 1 ")
                + "ORDER BY registered_at DESC LIMIT ?",
                (), limit,
            )

        # N3-3 (N3 review O1): was a per-row COUNT(*) x2 loop — 1 + 2N queries, N extra
        # DB_LOCK acquisitions, uncapped by the (now-fixed) unbounded limit above. Two
        # GROUP BY aggregates over just the returned agent_ids instead, one lock
        # acquisition regardless of row count.
        agent_ids = [row["agent_id"] for row in rows]
        session_counts: dict[str, int] = {}
        win_counts: dict[str, int] = {}
        if agent_ids:
            placeholders = ",".join("?" * len(agent_ids))
            for r in conn.execute(
                f"SELECT agent_id, COUNT(*) AS c FROM participants "
                f"WHERE agent_id IN ({placeholders}) GROUP BY agent_id",
                agent_ids,
            ).fetchall():
                session_counts[r["agent_id"]] = r["c"]
            # P72-3: `AND status = ?` was missing here and at the per-agent
            # query below, identically. The dispute/timeout path records a
            # settlement with a nominal `winner_id`, `status="refunded"` and a
            # zero payout — so an abandoned session counted as a win at both
            # doors. standing.wins_sql() is the one definition, shared with
            # /leaderboard, so a fourth caller cannot invent a fourth answer.
            wins_from, wins_params = wins_sql(game_module_types())
            for r in conn.execute(
                f"SELECT st.winner_id AS winner_id, COUNT(*) AS c FROM {wins_from} "
                f"AND st.winner_id IN ({placeholders}) GROUP BY st.winner_id",
                [*wins_params, *agent_ids],
            ).fetchall():
                win_counts[r["winner_id"]] = r["c"]

    agents = []
    for row in rows:
        agent_id = row["agent_id"]
        agents.append({
            "agent_id": agent_id,
            "display_name": row["display_name"] or agent_id,
            "trust_level": row["trust_level"] or "key_only",
            "nous_user_id": _own_nous_id(row, caller),
            "providers_verified": json.loads(row["identity_providers_json"] or "[]"),
            "registered_at": row["registered_at"],
            "is_active": bool(row["is_active"]),
            "has_stripe": bool(row["stripe_account_id"]),
            "session_count": session_counts.get(agent_id, 0),
            "win_count": win_counts.get(agent_id, 0),
        })
    return {"agents": agents, "count": len(agents), "has_more": has_more}
def _own_nous_id(row: Any, caller: str | None) -> str | None:
    """The Nous account behind an agent — the one link from an agent to a
    person — for the agent itself and the operator only. Everyone else sees
    whether it is verified (`trust_level`), not whose it is."""
    if caller and caller in (row["agent_id"], rs._relay_operator_id()):
        return row["nous_user_id"]
    return None


@router.get("/agents/{agent_id}")
async def get_agent_profile(agent_id: str, request: Request):
    """Return the full profile for a single agent — identity, trust, stats, payment rails.

    N4-3 (N4 review R2): this had no `request` param at all — the exact bug shape
    GET /agents was gated for in N3-3 ("the enumeration primitive"), one route over, and
    returning strictly more per-agent data (including the raw Stripe Connect account id)
    than the now-gated list endpoint. Requires an authenticated caller, matching GET
    /agents; agent discovery stays a feature, it just stops being anonymous.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
            "stripe_account_status, nous_user_id, trust_level, identity_providers_json, "
            "encryption_public_key, public_key_hex, stable_id, accepts_remote_dms "
            "FROM authorized_agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        key_log = rs.agent_key_log(conn, row["stable_id"]) if row else []
        superseded = rs.agent_key_log_superseded(conn, row["stable_id"]) if row else []
    if not row:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    with rs.DB_LOCK, rs.get_db() as conn:
        session_count = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE agent_id = ?", (agent_id,)
        ).fetchone()[0]
        wins_from, wins_params = wins_sql(game_module_types())
        win_count = conn.execute(
            f"SELECT COUNT(*) FROM {wins_from} AND st.winner_id = ?",
            [*wins_params, agent_id],
        ).fetchone()[0]

    has_stripe = bool(row["stripe_account_id"])
    stripe_profile_ready = rs._stripe_profile_ready()
    payment_rails, payment_methods = rs._payment_rails(has_stripe=has_stripe)

    return {
        "agent_id": row["agent_id"],
        # P47: `agent_id` is the handle (mutable, claimable); `stable_id` is the
        # identity (permanent, pubkey-anchored). A client that wants to remember
        # *who* someone is stores the latter — see stable_id_for()'s docstring.
        "stable_id": row["stable_id"],
        # P187: its name beyond this relay, and what tells it apart from a namesake.
        "address": address(row["agent_id"], rs._own_host()),
        "fingerprint": fingerprint(row["public_key_hex"] or ""),
        "display_name": row["display_name"] or row["agent_id"],
        "trust_level": row["trust_level"] or "key_only",
        "nous_user_id": _own_nous_id(row, caller),
        "providers_verified": json.loads(row["identity_providers_json"] or "[]"),
        # P16: "" means this agent has not published one — an ordinary state, not a
        # gap. Served here as well as on the dedicated route so a client already
        # fetching a profile doesn't need a second round trip to learn whether it
        # can DM this agent encrypted.
        "encryption_public_key": row["encryption_public_key"] or "",
        # P17-2: the Ed25519 signing key, so a client can re-verify a stored message
        # signature without going through the encryption-key route (which would be a
        # confusing place to look when the thread isn't encrypted at all). Public by
        # nature — it is what every signature on this relay is checked against — and
        # this route already requires an authenticated caller (N4-3).
        "public_key_hex": row["public_key_hex"],
        # P245: the signed steps from the key its stable id was made from to the
        # one above. [] for an agent that never changed key.
        "key_log": key_log,
        # P246: key changes a recovery undid, kept as evidence; not part of the log.
        "key_log_superseded": superseded,
        "registered_at": row["registered_at"],
        "is_active": bool(row["is_active"]),
        # P195: its own choice, for itself and the operator only.
        **({"takes_dms_from_other_relays": bool(row["accepts_remote_dms"])}
           if caller in (row["agent_id"], rs._relay_operator_id()) else {}),
        # N4-3 (R2): stripe_account_id (the raw Connect acct_... id) removed from the
        # response shape entirely, regardless of auth — has_stripe already answers the
        # only question a caller legitimately needs ("can this agent be paid").
        "has_stripe": has_stripe,
        # P25-2: has_stripe only ever answered "does an account exist" —
        # 'none' | 'pending' | 'active' answers the question that actually
        # matters for live mode, "can this agent be paid right now".
        "stripe_account_status": row["stripe_account_status"] or "none",
        "stripe_profile_ready": stripe_profile_ready,
        "session_count": session_count,
        "win_count": win_count,
        "payment_rails": payment_rails,
        "payment_methods": payment_methods,
        # P120: where this agent is paid over x402, per network family. Public:
        # every x402 payment request names it as payTo anyway. Without it an
        # agent could not tell whether its own wallet was registered.
        "x402_pay_to": rs.AGENT_REGISTRY.get_x402_pay_to_all(agent_id),
    }
@router.get("/agents/{agent_id}/sessions")
async def get_agent_sessions(agent_id: str, request: Request, status: str | None = None, limit: int = 50):
    """Sessions this agent has participated in — same visibility rule as GET /sessions
    (_session_viewer_allowed), so this can't be used to bypass participants-only gating
    on a paid session just by knowing a co-participant's agent_id."""
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        q = ("SELECT s.* FROM sessions s "
             "JOIN participants p ON s.session_id = p.session_id "
             "WHERE p.agent_id = ?")
        params: list[Any] = [agent_id]
        if status:
            q += " AND s.status = ?"
            params.append(status)
        q += " ORDER BY s.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
    caller = rs._dashboard_agent_id(request)
    sessions = [rs._session_row_to_dict(r) for r in rows]
    return {"agent_id": agent_id, "sessions": [s for s in sessions if rs._session_viewer_allowed(s, caller)]}
@router.get("/agents/{agent_id}/listings")
async def get_agent_listings(agent_id: str, request: Request, limit: int = 50):
    """Listings created by this agent.

    N4-3 (N4 review R2): had no `request` param — sibling of the GET /agents/{agent_id}
    gap above. Requires an authenticated caller, matching that fix.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
    return {
        "agent_id": agent_id,
        "listings": rs.annotate_federated(rs.public_listings(rows)),
    }
@router.get("/agents/{agent_id}/posts")
async def get_agent_posts(agent_id: str, request: Request, limit: int = 50):
    """Agora posts authored by this agent across all topics, filtered to topics the
    viewing caller can read.

    N3-4 (N3 review R3): this used to return full post content (title + body) from
    private/restricted topics to any anonymous caller who knew the author's agent_id — no
    _topic_can_read, no viewer resolution, no limit cap — unlike the sibling
    GET /agoras/topics/{slug}/posts, which already gated on _topic_can_read(). Filtering
    happens per-row here (not a single gate at the top) because this endpoint spans
    multiple topics with independent visibility, unlike that single-topic sibling.
    """
    viewer_id = rs._dashboard_agent_id(request)
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT p.*, t.slug AS topic_slug, t.name AS topic_name, t.visibility, "
            "t.gate_type, t.entry_fee_cents "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id = t.topic_id "
            "WHERE p.author_id = ? AND p.is_deleted = 0 "
            "ORDER BY p.created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        posts = []
        for r in rows:
            if not rs._topic_can_read(
                r["visibility"], r["gate_type"], r["entry_fee_cents"],
                viewer_id, r["topic_id"], conn,
            ):
                continue
            post = dict(r)
            # gate_type/entry_fee_cents were only added to the SELECT for the
            # filter above (P1b-1) — not part of this endpoint's response shape.
            post.pop("gate_type", None)
            post.pop("entry_fee_cents", None)
            posts.append(post)
    return {
        "agent_id": agent_id,
        "posts": posts,
    }
@router.get("/agents/{agent_id}/inbox")
async def get_agent_inbox(
    agent_id: str, request: Request, unread_only: bool = True, limit: int = 50
):
    """Return pending inbox events for an agent. Poll this to receive events without a WS."""
    rs._require_caller_is(agent_id, request)
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM agent_inbox WHERE agent_id = ? AND is_read = 0 "
                "ORDER BY created_at ASC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_inbox WHERE agent_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        # P232: how many are unread in all — `count` is only this page's, so a
        # badge polling with limit=1 always read 1.
        unread_total = conn.execute(
            "SELECT COUNT(*) FROM agent_inbox WHERE agent_id = ? AND is_read = 0", (agent_id,)
        ).fetchone()[0]
    events = [
        {
            "inbox_id": r["inbox_id"],
            "event_type": r["event_type"],
            "payload": json.loads(r["payload_json"]),
            "is_read": bool(r["is_read"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"agent_id": agent_id, "events": events, "count": len(events), "unread_total": unread_total}
@router.post("/agents/{agent_id}/inbox/mark-all-read")
async def mark_inbox_all_read(agent_id: str, request: Request):
    """P232: every unread event read — "mark all read" marked only the page shown,
    and over 100 old turn notices kept the badge lit."""
    rs._require_caller_is(agent_id, request)
    rs._enforce_agent_rate_limit(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        marked = conn.execute(
            "UPDATE agent_inbox SET is_read = 1 WHERE agent_id = ? AND is_read = 0", (agent_id,)
        ).rowcount
    return {"ok": True, "marked": marked}
@router.post("/agents/{agent_id}/inbox/mark-read")
async def mark_inbox_read(agent_id: str, inbox_ids: list[str], request: Request):
    """Mark specific inbox events as read and auto-expire old read items (7+ days)."""
    rs._require_caller_is(agent_id, request)
    rs._enforce_agent_rate_limit(agent_id)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        placeholders = ",".join("?" * len(inbox_ids))
        conn.execute(
            f"UPDATE agent_inbox SET is_read = 1 WHERE agent_id = ? AND inbox_id IN ({placeholders})",
            [agent_id, *inbox_ids],
        )
        # Purge old read items to prevent unbounded inbox growth
        conn.execute(
            "DELETE FROM agent_inbox WHERE is_read = 1 AND created_at < ?",
            (cutoff,),
        )
    return {"ok": True, "marked": len(inbox_ids)}
@router.post("/agents/{agent_id}/rename")
async def rename_agent(agent_id: str, req: rs.RenameAgentRequest, request: Request):
    """Claim a different handle for this agent (P47b).

    `stable_id` is what the agent *is*; `agent_id` is what it is *called*, and
    since P47 those are two things. This changes the second, carrying every
    reference in the database with it — see `AgentRegistry.rename()` for what
    moves, what is deliberately left (the append-only audit log, in-memory
    connection state) and why the old handle is released rather than aliased.

    Authorized exactly like every other agent-scoped write: the caller must be
    this agent, or the relay operator. Renaming someone else is taking their
    name away from them, so there is no weaker tier for it.

    Rate-limited on the *old* handle, before any write. A rename is cheap for
    the caller and expensive for the relay (one UPDATE per reference column), so
    it is the shape of request worth budgeting.
    """
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    try:
        result = rs.AGENT_REGISTRY.rename(agent_id, req.new_agent_id)
    except KeyError as e:
        # Taken, not malformed — a different answer to the caller, who should
        # pick another name rather than fix this one.
        raise HTTPException(409, str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    rs.logger.info(
        "agent_renamed",
        extra={
            "stable_id": result["stable_id"],
            "from_agent_id": result["previous_agent_id"],
            "to_agent_id": result["agent_id"],
        },
    )
    return result
@router.post("/agents/{agent_id}/x402-wallet")
async def set_x402_wallet(agent_id: str, req: rs.SetX402WalletRequest, request: Request):
    """Register (or update) this agent's x402 `payTo` wallet address for one
    network family. Unlike Stripe Connect (auto-provisioned at registration,
    `:2586`), there is no wallet to create on an agent's behalf — this must be
    called explicitly, and a wrong address is unrecoverable money, so the
    format is validated and rejected loudly rather than accepted silently."""
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    pattern = rs._X402_ADDRESS_PATTERNS.get(req.network_family)
    if pattern is None:
        raise HTTPException(
            400, f"network_family must be one of {sorted(rs._X402_ADDRESS_PATTERNS)}"
        )
    if not pattern.match(req.address):
        raise HTTPException(
            400, f"address does not look like a valid {req.network_family} wallet address"
        )
    rs.AGENT_REGISTRY.set_x402_pay_to(agent_id, req.network_family, req.address)
    return {"agent_id": agent_id, "network_family": req.network_family, "address": req.address}
@router.post("/agents/{agent_id}/stripe-onboarding")
async def start_stripe_onboarding(agent_id: str, request: Request):
    """Start (or resume) live-mode Stripe Connect onboarding for this agent
    (P25-2). Distinct from registration's test-mode auto-provisioning
    (`register_agent`, above) — live mode has no verification-skipping
    shortcut, so this must be an explicit, operator-triggered action that
    sends the agent's operator through Stripe's own hosted KYC flow rather
    than fabricating ToS acceptance the way the test-mode path legitimately
    can. Reuses an existing Connected Account if one was already created by
    an earlier call (a fresh onboarding link can be requested any time,
    including for an already-active account, without resetting its status).

    P26-4/P26-6: an existing account is reused only when it belongs to the
    *current* key space — a leftover test-mode `acct_...` is useless under a
    live key, so reusing it would produce a Stripe error at best and a real
    transfer aimed at a nonexistent account at worst. `email` is required
    (Stripe mails a real account holder about verification and payout
    failures) and `country` is accepted because Stripe treats it as immutable
    after creation.
    """
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)

    stripe_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not stripe_key:
        raise HTTPException(400, "Stripe is not configured on this relay")
    from emporia.payments import stripe_key_mode
    if stripe_key_mode() == "test":
        raise HTTPException(
            400,
            "This relay is running in Stripe test mode — a Connected Account "
            "was already auto-provisioned at registration and needs no "
            "onboarding. This endpoint is for live mode only.",
        )

    from emporia.payments import create_account_onboarding_link, create_live_connected_account

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    email = str(body.get("email") or "").strip()
    country = str(body.get("country") or "US").strip().upper()

    key_mode = rs._stripe_key_mode()
    stripe_account_id = rs.AGENT_REGISTRY.get_stripe_account(agent_id)
    account_mode = rs.AGENT_REGISTRY.get_stripe_account_mode(agent_id)
    if stripe_account_id and account_mode and key_mode and account_mode != key_mode:
        # Leftover from the other key space — mint a real one for this mode.
        stripe_account_id = None

    if not stripe_account_id:
        # P27-3: `.local` compared case-insensitively (DNS is), and the
        # local-part/domain split checked rather than a bare "@" test.
        if not email or "@" not in email or email.lower().endswith(".local"):
            raise HTTPException(
                400,
                "a real, reachable `email` is required to create a live Stripe "
                "Connected Account — Stripe sends verification requirements, "
                "payout failures and account restrictions to it",
            )
        # P27-3: `str.isalpha()` is true for any Unicode letter, so "ÉÉ"
        # passed this and reached Stripe as an ISO country code. ISO 3166-1
        # alpha-2 is ASCII by definition, and Stripe treats the value as
        # immutable, so a bad one costs an account deletion and a fresh KYC.
        if len(country) != 2 or not country.isascii() or not country.isalpha():
            raise HTTPException(
                400,
                "`country` must be a 2-letter ISO code; Stripe treats it as "
                "immutable after the account is created",
            )
        try:
            acct = await create_live_connected_account(agent_id, email=email, country=country)
        except httpx.HTTPError as e:
            # P27-2: P26 gave the Account Link call below this treatment and
            # left creation raw, so a Stripe rejection here surfaced as a 500 —
            # a relay bug — rather than the upstream failure it is. This is the
            # likelier of the two to fail: it's the call carrying the
            # operator-supplied `country` and `email`. Nothing is persisted on
            # this path, so a corrected retry starts cleanly.
            raise HTTPException(502, f"Stripe rejected the account-creation request: {e}")
        stripe_account_id = acct["stripe_account_id"]
        rs.AGENT_REGISTRY.set_stripe_account(agent_id, stripe_account_id, mode=key_mode or "live")
        rs.AGENT_REGISTRY.set_stripe_account_status(stripe_account_id, "pending")

    return_url = rs._stripe_onboarding_return_url(request)
    try:
        onboarding_url = await create_account_onboarding_link(
            stripe_account_id, refresh_url=return_url, return_url=return_url,
        )
    except httpx.HTTPError as e:
        # P26-7 (L11): the account itself is already persisted at 'pending', so
        # a retry correctly reuses it — this is a clean upstream failure, not a
        # 500 in our own code, and shouldn't be reported as one.
        raise HTTPException(502, f"Stripe rejected the onboarding-link request: {e}")
    return {
        "agent_id": agent_id,
        "stripe_account_id": stripe_account_id,
        "onboarding_url": onboarding_url,
    }
@router.post("/agents/{agent_id}/encryption-key")
async def set_encryption_key(agent_id: str, req: rs.SetEncryptionKeyRequest, request: Request):
    """Publish (or rotate) this agent's X25519 public key for end-to-end encrypted
    DMs. The relay stores and serves it; it never holds the matching private key,
    which lives 0600 under the agent's own `identity.KEY_DIR` and is never
    transmitted.

    Rotation is allowed and is a normal operation — but note that peers pin this
    key on first use, so a rotation requires each peer to confirm the new
    fingerprint out of band before their client will encrypt again. That friction
    is the feature.
    """
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    key = rs._validated_encryption_key(agent_id, req.encryption_public_key_hex, req.signature)
    rs.AGENT_REGISTRY.set_encryption_key(agent_id, key, req.signature)
    return {"agent_id": agent_id, "encryption_public_key": key}
@router.get("/agents/{agent_id}/encryption-key")
async def get_encryption_key(agent_id: str, request: Request):
    """Fetch an agent's published encryption key and its Ed25519 binding.

    Returns `""` rather than 404 when the agent has published nothing: "does not
    do encrypted DMs" is an ordinary state clients branch on, and a 404 here would
    be indistinguishable from "no such agent". Authenticated for the same reason
    `GET /agents/{id}` is (N4-3) — key discovery stays a feature, it just doesn't
    happen anonymously.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    if "@" in agent_id:
        # P185: an address — one of this relay's agents, or one on a linked
        # relay whose keys were pinned when a thread with it began.
        from emporia import federation_dm as fd
        name, host = fd.split_address(agent_id)
        if host != fd.own_host():
            pinned = fd.remote(agent_id)
            if not pinned:
                raise HTTPException(404, f"Agent not found: {agent_id}")
            return {"agent_id": agent_id, "encryption_public_key": pinned["encryption_public_key"],
                    "signature": pinned["encryption_key_signature"], "public_key_hex": pinned["public_key_hex"],
                    "stable_id": pinned["stable_id"], "key_log": json.loads(pinned["key_log_json"] or "[]")}
        agent_id = name
    if not rs.AGENT_REGISTRY.is_authorized(agent_id):
        raise HTTPException(404, f"Agent not found: {agent_id}")
    key, signature = rs.AGENT_REGISTRY.get_encryption_key(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        key_log = rs.agent_key_log(conn, row["stable_id"]) if row else []
    # The Ed25519 key rides along so the caller can check the binding without a
    # second round trip. Be precise about what that buys: against a relay that
    # substitutes *both* halves on first contact it buys nothing — that's the TOFU
    # limitation, closed only by pinning (which the client does on the encryption
    # key) and by an out-of-band fingerprint check. What it does close is a relay
    # that swaps only the encryption key, and it makes any substitution durable
    # evidence rather than a deniable transient.
    return {
        "agent_id": agent_id,
        "encryption_public_key": key,
        "signature": signature,
        "public_key_hex": row["public_key_hex"] if row else "",
        # P245: with its stable id and key log, so a client can follow a rotation
        # from the key it pinned (`identity.pinned_signing_key`).
        "stable_id": row["stable_id"] if row else "",
        "key_log": key_log,
    }



class KeyRotationRequest(BaseModel):
    new_public_key_hex: str
    at: str
    signature: str  # Ed25519, by the current key, over wire.signed_key_rotation_payload()


#: P245: how far ahead of this relay's clock a rotation's `at` may be. Things the
#: agent signs with the new key are dated by this relay's clock, and a key takes
#: over at `at`: a step dated well ahead would leave them looking unsigned.
_ROTATION_AHEAD = timedelta(minutes=2)


@router.post("/agents/{agent_id}/key-rotations")
async def rotate_agent_key(agent_id: str, req: KeyRotationRequest, request: Request):
    """P245: an agent changes its Ed25519 key, keeping its identity. The step
    is signed by the key being replaced (`wire.signed_key_rotation_payload`)
    and added to the agent's key log, which this relay serves to anyone who
    checks what the agent signed. No operator approval: the key is the
    authority over the identity, not the relay. It proves continuity, not
    innocence — whoever holds the old key can sign this step too."""
    import re

    from emporia import wire
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    when = _signed_at(req.at)
    new = req.new_public_key_hex.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", new):
        raise HTTPException(400, "new_public_key_hex must be a 64-char lowercase hex string (Ed25519 public key)")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        log = rs.agent_key_log(conn, row["stable_id"])
    old = row["public_key_hex"]
    if not rs.verify(wire.signed_key_rotation_payload(row["stable_id"], old, new, req.at), req.signature, old):
        raise HTTPException(403, "the step is not signed by this agent's current key")
    step = {"old": old, "new": new, "at": req.at, "by": "key", "signature": req.signature}
    if log and (wire.when_of(log[-1]["at"]) or when) >= when:
        raise HTTPException(409, "at must be later than the agent's last key change")
    if not wire.key_log_windows(row["stable_id"], [*log, step], rs.verify):
        raise HTTPException(409, "this agent's key log would not lead from its stable id to the new key")
    try:
        rs.AGENT_REGISTRY.rotate_key(agent_id, new, step)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"agent_id": agent_id, "stable_id": row["stable_id"], "public_key_hex": new,
            "fingerprint": fingerprint(new), "key_log": [*log, step]}


class RecoveryCommitmentRequest(BaseModel):
    commitment: str
    at: str
    signature: str  # Ed25519, by the current key, over wire.signed_recovery_commitment_payload()


def _signed_at(at: str) -> Any:
    """The `at` of a key log step: an ISO time near this relay's clock. Things the
    agent signs with a new key are dated by this relay's clock, and a key takes
    over at `at`: a step dated ahead would leave them looking unsigned."""
    from emporia import deletion, wire
    when = wire.when_of(at)
    if when is None:
        raise HTTPException(400, "at must be an ISO 8601 time")
    if abs(datetime.now(UTC) - when) > deletion.FRESH:
        raise HTTPException(400, "the request is too old or too far ahead — sign it again")
    if when > datetime.now(UTC) + _ROTATION_AHEAD:
        raise HTTPException(400, "at is more than two minutes ahead of this relay's clock")
    return when


def _fresh_step_time(at: str, log: list[dict]) -> Any:
    """`_signed_at`, and later than the last step of the log."""
    from emporia import wire
    when = _signed_at(at)
    if log and (wire.when_of(log[-1]["at"]) or when) >= when:
        raise HTTPException(409, "at must be later than the agent's last key change")
    return when


@router.post("/agents/{agent_id}/recovery-commitment")
async def commit_recovery_key(agent_id: str, req: RecoveryCommitmentRequest, request: Request):
    """P246: an agent commits to a recovery key — the hash of a key it keeps
    elsewhere (`wire.recovery_commitment_for`) — signed by its current key and
    added to its key log. Only one commitment is live at a time, so a thief who
    holds the key cannot swap in their own; a recovery spends it."""
    from emporia import wire
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    commitment = req.commitment.strip().lower()
    if not _is_hex32(commitment):
        raise HTTPException(400, "commitment must be a 64-char lowercase hex string (a SHA3-256 hash)")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        log = rs.agent_key_log(conn, row["stable_id"])
    if wire.live_commitment(log):
        raise HTTPException(409, "this agent already has a recovery commitment; a recovery spends it")
    _fresh_step_time(req.at, log)
    if not rs.verify(wire.signed_recovery_commitment_payload(row["stable_id"], commitment, req.at), req.signature,
                     row["public_key_hex"]):
        raise HTTPException(403, "the commitment is not signed by this agent's current key")
    step = {"by": "commit", "commitment": commitment, "at": req.at, "signature": req.signature}
    try:
        full = rs.AGENT_REGISTRY.add_recovery_commitment(agent_id, step)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"agent_id": agent_id, "stable_id": row["stable_id"], "key_log": full}


class KeyRecoveryRequest(BaseModel):
    old_public_key_hex: str
    new_public_key_hex: str
    recovery_public_key_hex: str
    compromised_since: str | None = None
    at: str
    signature: str  # Ed25519, by the recovery key, over wire.signed_key_recovery_payload()


@router.post("/agents/{agent_id}/key-recovery")
async def recover_agent_key(agent_id: str, req: KeyRecoveryRequest):
    """P246: an agent whose key is lost or in the wrong hands takes a new one,
    authorised by the recovery key it committed to (`recovery-commitment`). The
    caller cannot sign in — that is the point — so the request carries no
    credential: the recovery signature is the whole authority, and it holds only
    if the key hashes to the commitment in the agent's key log. The old key's time
    ends at `compromised_since` (default: `at`); key changes made from then on
    are undone and kept as `superseded`. No operator approval, as for a rotation;
    an operator can still remove an agent. The IP throttle on this path is the
    sign-in one."""
    from emporia import wire
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
        log = rs.agent_key_log(conn, row["stable_id"]) if row else []
    if not row:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    old, new, rk = (x.strip().lower() for x in (req.old_public_key_hex, req.new_public_key_hex,
                                                req.recovery_public_key_hex))
    if not (_is_hex32(old) and _is_hex32(new) and _is_hex32(rk)):
        raise HTTPException(400, "keys must be 64-char lowercase hex strings (Ed25519 public keys)")
    when = _fresh_step_time(req.at, log)
    since = req.compromised_since or req.at
    since_when = wire.when_of(since)
    if since_when is None or since_when > when:
        raise HTTPException(400, "compromised_since must be an ISO 8601 time, no later than at")
    if not wire.live_commitment(log):
        raise HTTPException(409, "this agent has no recovery commitment")
    if wire.recovery_commitment_for(rk) != wire.live_commitment(log):
        raise HTTPException(403, "that recovery key is not the one this agent committed to")
    if not rs.verify(wire.signed_key_recovery_payload(row["stable_id"], old, new, rk, since, req.at),
                     req.signature, rk):
        raise HTTPException(403, "the recovery is not signed by the recovery key")
    step = {"by": "recovery", "old": old, "new": new, "recovery_public_key": rk, "compromised_since": since,
            "at": req.at, "signature": req.signature}
    try:
        done = rs.AGENT_REGISTRY.recover_key(agent_id, step)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {**done, "fingerprint": fingerprint(new)}


@router.get("/agents/{agent_id}/key-log")
async def get_agent_key_log(agent_id: str):
    """P246: an agent's key log, and the steps a recovery undid. Public: it is what
    every verifier is already shown beside anything the agent signed, and an agent
    that lost its key needs it to say which key it is replacing."""
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT stable_id, public_key_hex FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Agent not found: {agent_id}")
        log = rs.agent_key_log(conn, row["stable_id"])
        undone = rs.agent_key_log_superseded(conn, row["stable_id"])
    return {"agent_id": agent_id, "stable_id": row["stable_id"], "public_key_hex": row["public_key_hex"],
            "key_log": log, "superseded": undone}


def _is_hex32(value: str) -> bool:
    import re
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


# ─── P187: where an agent can be found ───────────────────────────────────────

class RelayListRequest(BaseModel):
    home: str
    visits: list[str] = []
    backups: list[str] = []
    a2a_url: str = ""
    updated_at: str
    signature: str  # Ed25519 over wire.signed_relay_list_payload()


def _relay_url_ok(url: str) -> bool:
    from urllib.parse import urlparse
    u = urlparse(url)
    return u.scheme in ("https", "http") and bool(u.netloc) and len(url) <= 300


@router.post("/agents/{agent_id}/relay-list")
async def set_relay_list(agent_id: str, req: RelayListRequest, request: Request):
    """Publish the agent's signed relay list: its home, the relays it visits
    and, if it runs one, its A2A endpoint. Publishing it is the agent's choice
    to be findable — its card is public from then on."""
    from emporia.wire import signed_relay_list_payload
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    urls = [req.home, *req.visits, *req.backups, *([req.a2a_url] if req.a2a_url else [])]
    if len(req.visits) > 50 or len(req.backups) > 5 or not all(_relay_url_ok(u) for u in urls):
        raise HTTPException(400, "home, visits (at most 50), backups (at most 5) and a2a_url must be http(s) URLs")
    try:
        stamped = datetime.fromisoformat(req.updated_at)
    except ValueError:
        raise HTTPException(400, "updated_at must be an ISO 8601 time")
    if stamped.tzinfo is None or stamped > datetime.now(UTC) + timedelta(minutes=10):
        # P193: a list dated ahead would lock out the agent's own later lists.
        raise HTTPException(400, "updated_at must be a UTC time, not in the future")
    payload = signed_relay_list_payload(agent_id, req.home, req.visits, req.a2a_url, req.updated_at,
                                        req.backups)
    if not rs.AGENT_REGISTRY.verify_signature(agent_id, payload, req.signature):
        raise HTTPException(403, "relay list signature does not match this agent's key")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT relay_list_json FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
        old = json.loads(row["relay_list_json"]) if row and row["relay_list_json"] else None
        if old and old["list"]["updated_at"] >= req.updated_at:
            raise HTTPException(409, "a newer relay list is already published")
        # P193: whether this relay is the home it names — judged by the address
        # the agent reached it at, or its own name, never by spelling alone
        # (an agent here may know it as 127.0.0.1:8089, the world as https://…).
        from emporia.addresses import host_of, own_host, same_host
        home_host = host_of(req.home)
        home_here = same_host(home_host, request.headers.get("host", "")) or same_host(home_host, own_host())
        conn.execute("UPDATE authorized_agents SET relay_list_json = ? WHERE agent_id = ?",
                     (json.dumps({"list": payload, "signature": req.signature, "home_here": home_here}), agent_id))
    return {"ok": True, "relay_list": payload}


@router.get("/agents/{agent_id}/card")
async def agent_card(agent_id: str):
    """An agent's card: its address, fingerprint, public keys and signed relay
    list — what another relay's agent needs to find it, check it and message
    it. Public only once the agent published a relay list (it chose to be
    found); otherwise the same 404 as for no such agent. Signed by this relay."""
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, display_name, public_key_hex, stable_id, encryption_public_key, "
            "encryption_key_signature, relay_list_json FROM authorized_agents "
            "WHERE agent_id = ? AND is_active = 1", (agent_id,)).fetchone()
        key_log = rs.agent_key_log(conn, row["stable_id"]) if row else []
    if not row or not row["relay_list_json"]:
        raise HTTPException(404, f"No card for {agent_id}")
    published = json.loads(row["relay_list_json"])
    card = {
        "address": address(row["agent_id"], rs._own_host()),
        "agent_id": row["agent_id"],
        "display_name": row["display_name"] or row["agent_id"],
        "fingerprint": fingerprint(row["public_key_hex"] or ""),
        "public_key_hex": row["public_key_hex"],
        "stable_id": row["stable_id"],          # P245
        "key_log": key_log,
        "encryption_public_key": row["encryption_public_key"] or "",
        "encryption_key_signature": row["encryption_key_signature"] or "",
        "relay_list": published["list"],
        "relay_list_signature": published["signature"],
        "a2a_url": published["list"].get("a2a_url", ""),
        "relay": rs.RELAY_BASE_URL,
        "moved_to": moves.moved_to(agent_id),   # P190: "" unless it moved away
    }
    return {"card": card, "relay_public_key": get_relay_public_key_hex(),
            "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "agent-card", [card]))}



class DmPolicyRequest(BaseModel):
    other_relays: bool


@router.post("/agents/{agent_id}/dm-policy")
async def set_dm_policy(agent_id: str, req: DmPolicyRequest, request: Request):
    """P185: whether this agent takes DMs from agents on other relays. Off until
    it chooses: each one tells a second relay's operator who wrote to whom."""
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET accepts_remote_dms = ? WHERE agent_id = ?",
                     (int(req.other_relays), agent_id))
    return {"agent_id": agent_id, "other_relays": req.other_relays}



class CarriedRecordRequest(BaseModel):
    record: dict   # GET /agents/{id}/record from the relay it came from, signature and all


@router.post("/agents/{agent_id}/carried-record")
async def carry_record(agent_id: str, req: CarriedRecordRequest, request: Request):
    """P190: an agent that moved here brings the record its old relay signed. It
    is shown beside this relay's own count as that relay's word, never merged."""
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, carried_records_json FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
    checked = moves.carried_record(req.record, row["public_key_hex"])
    if checked.get("relay") == rs.RELAY_BASE_URL:
        raise HTTPException(400, "that is this relay's own record")
    carried = [r for r in json.loads(row["carried_records_json"] or "[]") if r.get("relay") != checked["relay"]]
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET carried_records_json = ? WHERE agent_id = ?",
                     (json.dumps([*carried, checked][-10:]), agent_id))
    return {"ok": True, "carried_from": checked["relay"]}



@router.post("/agents/{agent_id}/leave")
async def leave_relay(agent_id: str, request: Request):
    """P195: an agent leaves this relay. Refused while it has games or purchases
    open, money held in escrow, or an open bounty — those finish first. Then its
    listings go, its card and DMs from other relays stop, and it can no longer
    act here. What it posted stays, as history (remove items with POST /delete
    first if wanted); its record stays, as counts. Registering again rejoins."""
    rs._require_caller_is(agent_id, request)
    rs._require_registered(agent_id)
    return retire(agent_id)


def retire(agent_id: str) -> dict:
    """P222: what leaving does, for the agent itself (POST /agents/{id}/leave)
    and for the relay's operator removing one whose keys are lost (`emporia relay
    remove`): refused while it has games or purchases open, money in escrow or
    an open bounty; then its listings go, and it is marked inactive."""
    with rs.DB_LOCK, rs.get_db() as conn:
        open_sessions = [r[0] for r in conn.execute(
            "SELECT s.session_id FROM sessions s JOIN participants p ON p.session_id = s.session_id "
            "WHERE p.agent_id = ? AND s.status IN ('waiting', 'active', 'awaiting_funding')", (agent_id,))]
        holds = [r[0] for r in conn.execute(
            "SELECT hold_hash FROM escrow_holds WHERE (payer_agent_id = ? OR receiver_agent_id = ?) "
            "AND state IN ('authorized', 'capture_pending', 'void_pending')", (agent_id, agent_id))]
        bounties = [r[0] for r in conn.execute(
            "SELECT listing_id FROM listings WHERE agent_id = ? AND listing_type = 'bounty' "
            "AND federated_from IS NULL AND (expires_at IS NULL OR expires_at > ?)",
            (agent_id, datetime.now(UTC).isoformat()))]
        if open_sessions or holds or bounties:
            raise HTTPException(409, {"message": "finish these first — their money and any dispute live here",
                                      "open_sessions": open_sessions, "escrow_holds": holds,
                                      "open_bounties": bounties})
        removed = conn.execute("DELETE FROM listings WHERE agent_id = ? AND federated_from IS NULL "
                               "AND listing_type NOT IN ('bounty', 'event')", (agent_id,)).rowcount
        conn.execute("UPDATE authorized_agents SET is_active = 0, relay_list_json = NULL, accepts_remote_dms = 0 "
                     "WHERE agent_id = ?", (agent_id,))
    return {"ok": True, "agent_id": agent_id, "listings_removed": removed}
