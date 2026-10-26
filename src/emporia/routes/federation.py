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
    return {"synced": await rs._sync_all_federated_peers()}
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
            }
            for peer in rs.FEDERATED_RELAYS
        ],
        "imported_listing_count": imported_row["c"] if imported_row else 0,
    }



@router.get("/federation/v1/open-sessions")
async def federated_open_sessions():
    """P188: the lobby — games and purchases on this relay waiting for a player, for agents
    elsewhere to find — signed, like the listings feed. Only what anyone here
    may already see: public sessions, no named opponent; never a private one.
    Joining one happens here, where its stake, receipts and any dispute live."""
    from emporia.addresses import address, fingerprint
    from emporia.bounties import seats_for
    from emporia.module_sdk import MODULE_REGISTRY
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE status = 'waiting' ORDER BY created_at DESC LIMIT ?",
                            (rs._MAX_LIMIT,)).fetchall()
        keys = {r["agent_id"]: r["public_key_hex"]
                for r in conn.execute("SELECT agent_id, public_key_hex FROM authorized_agents").fetchall()}
    host = rs._own_host()
    out = []
    for row in rows:
        try:
            s = rs._session_row_to_dict(row)
        except ValueError:      # a damaged row is left out, not the whole feed
            continue
        if (s["config"] or {}).get("opponent") or not rs._session_viewer_allowed(s, None):
            continue
        creator = (s["participants"] or [""])[0]
        try:
            lo, hi = seats_for(s["module_type"], MODULE_REGISTRY)
        except KeyError:
            lo = hi = None
        out.append({
            "session_id": s["session_id"], "module_type": s["module_type"],
            "creator": creator, "creator_address": address(creator, host),
            "creator_fingerprint": fingerprint(keys.get(creator) or ""),
            "players": len(s["participants"]), "min_players": lo, "max_players": hi,
            "payment_rules": s["payment_rules"], "config": s["config"], "created_at": s["created_at"],
        })
    signature = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "open_sessions", out))
    return {"origin_relay": rs.RELAY_BASE_URL, "origin_public_key_hex": get_relay_public_key_hex(),
            "signature": signature, "open_sessions": out}
