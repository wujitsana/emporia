"""Federation routes (P58).

Extracted by definition name, not by line range — see
scratchpad/extract_router.py's main_defs(). Shared helpers stay in
`relay_server`; only the handlers move.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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

    The batch is signed with this relay's own Ed25519 key (persisted, distinct from any agent's key —
    see identity.generate_or_load_relay_keypair()) so a pulling peer can verify the batch
    actually came from here unaltered, rather than trusting the HTTP response wholesale.
    """
    # P240: only listings their agent signed. Each carries that signature, the
    # nonce and stable id it was made with, and the agent's key, so a relay that
    # pulls it checks the agent's word, not just this relay's. A listing this
    # relay wrote itself (a bounty, an event's) has no signature and stays here.
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE (expires_at IS NULL OR expires_at > ?) "
            "AND origin_relay = ? AND signature IS NOT NULL "
            f"ORDER BY created_at DESC LIMIT {rs._MAX_LIMIT}",
            (datetime.now(UTC).isoformat(), rs.RELAY_BASE_URL),
        ).fetchall()
    listings = [rs.public_listing(r) for r in rows]
    for x in listings:
        x.pop("federated_from", None)
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



def _lobby_items() -> list[dict]:
    """The lobby's items: each a game its creator offered (P241), whose offer
    still matches its terms here — so this relay serves only what the creator
    signed, and each item carries what a reader needs to check that."""
    from emporia.addresses import address, fingerprint
    from emporia.bounties import seats_for
    from emporia.identity import verify
    from emporia.module_sdk import MODULE_REGISTRY
    from emporia.wire import open_session_verdict
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE status = 'waiting' AND session_id IN "
                            "(SELECT session_id FROM session_offers) ORDER BY created_at DESC LIMIT ?",
                            (rs._MAX_LIMIT,)).fetchall()
        offers = {r["session_id"]: r for r in conn.execute("SELECT * FROM session_offers").fetchall()}
    host = rs._own_host()
    out = []
    for row in rows:
        try:
            s = rs._session_row_to_dict(row)
        except ValueError:      # a damaged row is left out, not the whole feed
            continue
        if (s["config"] or {}).get("opponent") or not rs._session_viewer_allowed(s, None):
            continue
        offer = offers[s["session_id"]]
        creator = (s["participants"] or [""])[0]
        try:
            lo, hi = seats_for(s["module_type"], MODULE_REGISTRY)
        except KeyError:
            lo = hi = None
        item = {
            "session_id": s["session_id"], "module_type": s["module_type"],
            "creator": creator, "creator_address": address(creator, host),
            "creator_fingerprint": fingerprint(offer["public_key_hex"]),
            "creator_public_key_hex": offer["public_key_hex"], "creator_stable_id": offer["stable_id"],
            "offer_signature": offer["signature"],
            "players": len(s["participants"]), "min_players": lo, "max_players": hi,
            "payment_rules": s["payment_rules"], "config": s["config"], "created_at": s["created_at"],
        }
        if not open_session_verdict(item, rs.RELAY_BASE_URL, verify):
            out.append(item)
    return out


@router.get("/federation/v1/open-sessions")
async def federated_open_sessions():
    """P188: the lobby — games and purchases on this relay waiting for a player, for agents
    elsewhere to find — signed, like the listings feed. Only what anyone here
    may already see: public sessions, no named opponent; never a private one.
    Joining one happens here, where its stake, receipts and any dispute live.
    P241: only games their creator offered, each with the creator's signature."""
    out = _lobby_items()
    signature = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "open_sessions", out))
    return {"origin_relay": rs.RELAY_BASE_URL, "origin_public_key_hex": get_relay_public_key_hex(),
            "signature": signature, "open_sessions": out}


def _advert_feed(kind: str) -> list[dict]:
    """P247: the rooms or topics their owners advertised whose advert still
    matches what is stored here — so a changed or deleted one drops out of the
    feed on its own. Each item names its origin relay and carries the owner's
    key, stable id and signature for the reader to check, and where to join."""
    from emporia import rooms as rooms_db
    from emporia.addresses import address, fingerprint
    from emporia.identity import verify
    from emporia.wire import ROOM_ADVERT_FIELDS, TOPIC_ADVERT_FIELDS, room_advert_verdict, topic_advert_verdict
    host = rs._own_host()
    out: list[dict] = []
    with rs.DB_LOCK, rs.get_db() as conn:
        if kind == "rooms":
            adverts = conn.execute("SELECT * FROM room_adverts ORDER BY advertised_at DESC LIMIT ?",
                                   (rs._MAX_LIMIT,)).fetchall()
        else:
            adverts = conn.execute(
                "SELECT a.*, t.slug, t.name, t.description, t.gate_type, t.entry_fee_cents, t.visibility, t.creator_id "
                "FROM topic_adverts a "
                "JOIN agora_topics t ON t.topic_id = a.topic_id WHERE t.is_deleted = 0 "
                "ORDER BY a.advertised_at DESC LIMIT ?", (rs._MAX_LIMIT,)).fetchall()
    for a in adverts:
        owner = {"owner_public_key_hex": a["public_key_hex"], "owner_stable_id": a["stable_id"],
                 "advert_signature": a["signature"], "origin_relay": rs.RELAY_BASE_URL}
        if kind == "rooms":
            room = rooms_db.get_room(a["room_id"], db_path=rs.DATABASE_PATH)
            if not room or room.room_type == "private" or room.gate_type == "invite":
                continue
            creator = room.creator_id
            item = {"room_id": room.room_id, **{f: getattr(room, f) for f in ROOM_ADVERT_FIELDS},
                    "join_url": f"{rs.RELAY_BASE_URL}/rooms/{room.room_id}/join", "created_at": room.created_at,
                    **owner}
            verdict = room_advert_verdict(item, rs.RELAY_BASE_URL, verify)
        else:
            if a["visibility"] != "public":
                continue
            creator = a["creator_id"]
            item = {"slug": a["slug"], **{f: a[f] for f in TOPIC_ADVERT_FIELDS},
                    "join_url": f"{rs.RELAY_BASE_URL}/agoras/topics/{a['slug']}", **owner}
            verdict = topic_advert_verdict(item, rs.RELAY_BASE_URL, verify)
        if not verdict:
            out.append({**item, "owner_address": address(creator, host),
                        "owner_fingerprint": fingerprint(a["public_key_hex"])})
    return out


@router.get("/federation/v1/rooms")
async def federated_rooms():
    """P247: rooms here that their creator advertised, for agents elsewhere to
    find — signed, like the lobby. A reader joins by visiting this relay with
    the same key (`visit_relay`); the room, its members and its money stay here."""
    out = _advert_feed("rooms")
    signature = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "rooms", out))
    return {"origin_relay": rs.RELAY_BASE_URL, "origin_public_key_hex": get_relay_public_key_hex(),
            "signature": signature, "rooms": out}


@router.get("/federation/v1/topics")
async def federated_topics():
    """P247: public Agora topics here that their owner advertised — as the rooms feed."""
    out = _advert_feed("topics")
    signature = sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "topics", out))
    return {"origin_relay": rs.RELAY_BASE_URL, "origin_public_key_hex": get_relay_public_key_hex(),
            "signature": signature, "topics": out}


# ─── P185: DMs between linked relays (emporia.federation_dm) ─────────────────

@router.get("/federation/v1/agents/{agent_id}")
async def federated_agent(agent_id: str, request: Request):
    """For a linked relay only, and only for an agent that takes DMs from other
    relays: its address and public keys, signed by this relay. Every other case
    is the same 404, so a peer cannot use it to list who is here."""
    from emporia import federation_dm as fd
    await fd.verified_peer(request, b"")
    from emporia import moves
    moves.gone(agent_id)
    record = fd.agent_record(agent_id) if fd.accepts(agent_id) else None
    if not record:
        raise HTTPException(404, "not found")
    return fd.signed_record(record)


class FederatedDM(BaseModel):
    from_address: str
    to_agent: str
    thread_id: str
    content: str
    msg_type: str
    nonce: str
    signature: str
    sender: dict          # the sender's address and public keys, as its relay vouches


@router.post("/federation/v1/dm")
async def federated_dm_in(request: Request):
    """A sealed DM from an agent on a linked relay. Checked like one sent here:
    the relay that sent it is linked and signed the request; it speaks for its
    own agents only; the sender's key is the one pinned for its address and
    signed the message; the content is sealed to both and to no one else; the
    recipient takes DMs from other relays, or started this thread itself."""
    import sqlite3
    import uuid as _uuid

    from emporia import crypto_box
    from emporia import federation_dm as fd
    from emporia import rooms as rooms_db
    from emporia.routes.dm import _dm_last_chain_hash
    from emporia.wire import ERR_DUPLICATE_NONCE, signed_dm_thread_payload

    body = await request.body()
    peer = await fd.verified_peer(request, body)
    if not rs.AGENT_REGISTRY.check_rate_limit(f"peer:{peer}"):
        raise HTTPException(429, "too many messages from that relay")
    try:
        req = FederatedDM.model_validate_json(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    sender = fd.pin_remote(peer, req.sender)
    if sender["address"] != req.from_address:
        raise HTTPException(403, "the message's sender is not the agent its relay vouched for")
    if not rs.AGENT_REGISTRY.check_rate_limit(f"remote:{req.from_address}"):
        raise HTTPException(429, "too many messages from that agent")   # P193: one sender, not its whole relay
    if not rs.AGENT_REGISTRY.is_authorized(req.to_agent):
        raise HTTPException(404, "not found")
    from emporia import moves
    moves.gone(req.to_agent)
    me = fd.address(req.to_agent, fd.own_host())
    thread_id = fd.thread_id_for(me, req.from_address)
    if req.thread_id != thread_id:
        raise HTTPException(400, "thread id does not name these two agents")
    with rs.DB_LOCK, rs.get_db() as conn:
        thread = conn.execute("SELECT * FROM dm_threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if not fd.accepts(req.to_agent) and not (thread and thread["started_by_local"]):
        raise HTTPException(404, "not found")   # the same answer as no such agent
    rs._require_valid_msg_type(req.msg_type)
    rs._require_content_within_limit(req.content)
    if not rs._NONCE_RE.fullmatch(req.nonce):
        raise HTTPException(400, "nonce must be 32-64 hex characters")
    try:
        crypto_box.validate_envelope_for(req.content, {me, req.from_address})
    except ValueError as e:
        raise HTTPException(400, f"DMs between relays are sealed to both agents — {e}") from e
    if not rs.verify(signed_dm_thread_payload(thread_id, req.content, req.msg_type, req.nonce),
                     req.signature, sender["public_key_hex"]):
        raise HTTPException(403, "message signature does not match the sender's key")
    now = datetime.now(UTC).isoformat()
    message_id = f"dmm_{_uuid.uuid4().hex[:12]}"
    a, b = sorted((me, req.from_address))
    with rs.DB_LOCK, rs.get_db() as conn:
        if not thread:
            conn.execute(
                "INSERT INTO dm_threads (thread_id, agent_a, agent_b, created_at, last_message_at, encrypted, "
                "local_agent, remote_address, peer_url, started_by_local) VALUES (?,?,?,?,?,1,?,?,?,0)",
                (thread_id, a, b, now, now, req.to_agent, req.from_address, peer))
        try:
            chain_hash = rooms_db.compute_chain_hash(_dm_last_chain_hash(conn, thread_id), req.from_address,
                                                     req.content, now)
            conn.execute(
                "INSERT INTO dm_messages (message_id, thread_id, sender_id, content, msg_type, created_at, "
                "signature, nonce, chain_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (message_id, thread_id, req.from_address, req.content, req.msg_type, now, req.signature,
                 req.nonce, chain_hash))
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, {"code": ERR_DUPLICATE_NONCE, "message": "already delivered"}) from e
        conn.execute("UPDATE dm_threads SET last_message_at = ? WHERE thread_id = ?", (now, thread_id))
    await rs.broadcast_to_agent(req.to_agent, {
        "type": "dm_received", "thread_id": thread_id, "message_id": message_id,
        "sender_id": req.from_address, "content": req.content, "msg_type": req.msg_type,
        "encrypted": True, "created_at": now})
    return {"message_id": message_id, "created_at": now}
