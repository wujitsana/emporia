"""Federation routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from emporia import relay_server as rs
from emporia.identity import (
    federation_signable_bytes,
    get_relay_public_key_hex,
    sign_raw_relay,
)

router = APIRouter()


@router.get("/gaming/v1/federate/listings")
async def federated_listings_out():
    """Serve local listings for federation pull by peer relays.

    Signed with this relay's own Ed25519 key (persisted, distinct from any agent's key —
    see identity.generate_or_load_relay_keypair()) so a pulling peer can verify the batch
    actually came from here unaltered, rather than trusting the HTTP response wholesale.
    """
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE (expires_at IS NULL OR expires_at > ?) "
            f"AND origin_relay = ? ORDER BY created_at DESC LIMIT {rs._MAX_LIMIT}",
            (datetime.now(UTC).isoformat(), rs.RELAY_BASE_URL),
        ).fetchall()
    listings = [rs.public_listing(r) for r in rows]
    # P184: each seller's identity key, inside the signature, so a relay that
    # pulls this can tell the seller from an agent there with the same name.
    if listings:
        ids = sorted({x["agent_id"] for x in listings})
        with rs.DB_LOCK, rs.get_db() as conn:
            keys = {r["agent_id"]: r["public_key_hex"] for r in conn.execute(
                f"SELECT agent_id, public_key_hex FROM authorized_agents "
                f"WHERE agent_id IN ({','.join('?' * len(ids))})", ids).fetchall()}
        for x in listings:
            x["agent_public_key_hex"] = keys.get(x["agent_id"])
    signature = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "listings", listings))
    return {
        "origin_relay": rs.RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
        "listings": listings,
    }
@router.get("/gaming/v1/federate/lobby")
async def federated_lobby_out():
    """Serve local open lobby challenges for federation pull by peer relays — the
    signed, machine-to-machine counterpart to the human/dashboard-facing
    GET /gaming/lobby (unchanged; still public by design, see _DELIBERATELY_PUBLIC).

    N11-5 (task #8, R5): lobby gossip used to be push-shaped
    (mcp_server.publish_challenge_to_peer POSTing to a peer's /gaming/lobby), which
    can never authenticate — the calling agent is by definition not registered on the
    peer it's pushing to, and a relay-scoped Bearer JWT doesn't cross relays by design.
    Inverted to the same signed-pull model GET /gaming/v1/federate/listings already
    uses: signed with this relay's own persistent Ed25519 key (see
    identity.generate_or_load_relay_keypair()), so a pulling peer can verify the batch
    actually came from here.

    N12-4 (R2): this used to diverge from federated_listings_out in two ways it
    shouldn't have. (a) No origin filter — this relay would sign and re-serve
    challenges it had merely imported from another peer, so the signature attested
    transport while a pulling peer read it as authorship; own-origin-only was the
    user's explicit choice for this feed (no multi-hop relay model). `origin_relay=None`
    is the actual "genuinely local, never imported" invariant for challenges — unlike
    listings, nothing stamps a challenge's origin_relay to this relay's own
    RELAY_BASE_URL at creation, and import_challenge() now stamps every peer-pulled
    challenge with the verified peer_url regardless of what it claims about itself, so
    None only ever means "created here." (b) No LIMIT, unlike listings' 200 — an
    unbounded response on a public unauthenticated endpoint whose size an attacker
    grows by creating challenges (same class as N4-5, N7-3).
    """
    challenges = [
        c.to_dict()
        for c in rs._game_registry.list_challenges("open", origin_relay=None, limit=rs._MAX_LIMIT)
    ]
    signature = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "challenges", challenges))
    return {
        "origin_relay": rs.RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
        "challenges": challenges,
    }
@router.post("/gaming/v1/federate/sync")
async def trigger_federation_sync(request: Request):
    """Manually trigger a federation pull from all configured peers.

    N4-5 (N4 review R7): had no `request` param at all — an anonymous caller could fan
    out an outbound HTTP pull to every configured peer on demand, free amplification and
    an on-demand federated-state refresh with no caller check whatsoever. This is an
    operator maintenance action, not an agent-facing one, so it requires the relay
    operator identity rather than just any authenticated caller.

    N5 review R5: on a default install EMPORIA_AGENT_ID is unset, so this — like the
    other two operator-only actions in this file — was unreachable by anyone,
    including whoever would become the operator by setting it, and said so with the
    same generic message a wrong-identity caller gets. See _require_operator.
    """
    caller = rs._dashboard_agent_id(request)
    rs._require_operator(caller, "Federation sync")
    results, lobby_results = await rs._sync_all_federated_peers()
    return {"synced": results, "synced_lobby": lobby_results}
@router.get("/federation/peers")
async def federation_peers():
    """Configured federation peers + last gossip-sync outcome, for the dashboard."""
    rs._ensure_federation_pins_loaded()
    with rs.DB_LOCK, rs.get_db() as conn:
        imported_row = conn.execute(
            "SELECT COUNT(*) AS c FROM listings WHERE origin_relay IS NOT NULL "
            "AND origin_relay != ?",
            (rs.RELAY_BASE_URL,),
        ).fetchone()
    return {
        "relay_url": rs.RELAY_BASE_URL,
        "standalone": not rs.FEDERATED_RELAYS,
        "peers": [
            {
                "url": peer,
                "pinned_public_key_hex": rs._FEDERATION_PEER_PUBKEYS.get(peer),
                **rs._FEDERATION_LAST_SYNC.get(peer, {"ok": None, "imported": 0}),
                "lobby_sync": rs._FEDERATION_LOBBY_LAST_SYNC.get(peer, {"ok": None, "imported": 0}),
            }
            for peer in rs.FEDERATED_RELAYS
        ],
        "imported_listing_count": imported_row["c"] if imported_row else 0,
    }
