"""Gaming lobby routes (P57).

Moved out of `relay_server` by scratchpad/extract_router.py. The only edits are
`@app` becoming `@router` and shared relay state resolving through `rs.` at call
time rather than being bound at import — see `emporia.routes` for why that
distinction is load-bearing for the suite's 43 patch sites.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from emporia import relay_server as rs
from emporia.engine.game_registry import ChallengeOwnershipError

router = APIRouter()


# ============================================================================
# Lobby (gaming challenge discovery)
# ============================================================================



@router.get("/gaming/lobby")
async def gaming_lobby_get(status: str = "open", game_type: str | None = None):
    challenges = [c.to_dict() for c in rs._game_registry.list_challenges(status)]
    if game_type:
        challenges = [c for c in challenges if c.get("game_type") == game_type]
    return {
        "ok": True,
        "brand": rs.BRAND,
        "count": len(challenges),
        "challenges": challenges,
    }


class GamingLobbyChallengeRequest(BaseModel):
    """Mirrors GameChallenge's fields (game_registry.py) for wire validation.

    creator_agent_id is a TARGET/attribution field, not the actor — this endpoint is
    peer-relay gossip (see below), so the agent a challenge is attributed to often isn't
    registered on *this* relay at all. The actor is the authenticated caller, checked
    separately below.

    N13-1 (R1): created_at is required, not optional — it was missing entirely until
    this fix, which meant req.model_dump() silently fed import_challenge() a payload
    missing one of the fields _recompute_challenge_id() hashes (N12-2), so every
    genuine challenge card (whose id was computed *with* a real created_at by
    create_challenge()) recomputed to a different id here and was rejected with a 422.
    A default (e.g. None) would have papered over that by making a mismatched-content
    payload verify against itself rather than surfacing the missing field.
    """
    challenge_id: str
    game_type: str
    creator_agent_id: str
    creator_gateway_url: str
    created_at: str
    payment_mode: str = "free"
    stake_amount: str = "0"
    currency: str = "USD"
    status: str = "open"
    expires_at: str | None = None
    origin_relay: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/gaming/lobby")
async def gaming_lobby_post(req: GamingLobbyChallengeRequest, request: Request):
    """Import a challenge card into this relay's lobby.

    N3-2 (N3 review R1): this endpoint used to take a raw `dict[str, Any]` body with zero
    caller authentication — a raw body has no field names for the identity-as-proof guard
    to introspect, which is exactly how it stayed unauthenticated through the whole N2
    pass. Any anonymous caller could publish a challenge attributed to any agent, or
    overwrite an existing challenge's status/metadata by reusing its challenge_id
    (`ON CONFLICT DO UPDATE` in GameRegistry._upsert has no ownership check).

    This is peer-relay gossip (mcp_server.py's publish_challenge_to_peer posts here), not
    an individual agent acting on their own behalf — creator_agent_id typically names an
    agent registered on the *origin* relay, not this one, so requiring
    caller == creator_agent_id would make legitimate cross-relay gossip permanently
    impossible. What this fix closes: (1) the caller must be a real, registered agent on
    *this* relay — no more fully anonymous writes; (2) a challenge_id collision with an
    existing row under a *different* creator_agent_id is rejected outright, closing the
    overwrite-any-existing-challenge attack completely, independent of who's calling.

    What this does NOT close: verifying that a claimed creator_agent_id genuinely belongs
    to a legitimate peer relay's agent — that requires relay-to-relay trust (mirroring the
    Ed25519 batch-signing already built for GET /gaming/v1/federate/listings), which is
    task #8's job, still deferred. Until task #8 lands, publish_challenge_to_peer's
    cross-relay flow has no credential path to a peer it isn't registered on and will get
    401'd calling a relay that (correctly) now requires authentication — expected fallout,
    not silently left insecure to keep it working.

    N12-3 (R3): the ownership check that used to live inline here (a second copy of the
    same rule, next to GameRegistry's own unenforced ON CONFLICT) now lives in
    GameRegistry.import_challenge() itself, shared with the other three importers. This
    handler just maps ChallengeOwnershipError to the same 403 it always returned.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    payload = req.model_dump()
    try:
        await rs._assert_payload_safe_counted(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        imported = rs._game_registry.import_challenge(payload)
        return {"ok": True, "challenge": imported.to_dict()}
    except ChallengeOwnershipError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(422, str(e))

