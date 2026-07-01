"""Emporia MCP Server — 44 tools via FastMCP (stdio/streamable-http/sse).

Tools:
  IDENTITY (2):
    register_agent, list_agents, get_agent_profile

  SESSIONS (8):
    create_session, list_sessions, get_session, join_session,
    submit_action, confirm_delivery, dispute_delivery, abandon_session

  LISTINGS (2):
    create_listing, list_listings

  PAYMENTS (2):
    create_payment_intent, get_settlements

  LOBBY / FEDERATION (11):
    create_challenge, list_challenges, cleanup_expired_challenges,
    export_challenge, import_challenge, accept_challenge,
    discover_peer_lobby, sync_lobby_from_peer, publish_challenge_to_peer,
    validate_turn, supported_games

  ROOMS (4):
    create_room, list_rooms, join_room, send_room_message

  INBOX (2):
    get_inbox, mark_inbox_read

  RELAY INFO (2):
    relay_payment_info, get_agent_profile

  AGORAS (7):
    create_agora_topic, list_agora_topics, invite_to_agora_topic,
    subscribe_agora_topic, create_agora_post, list_agora_posts,
    add_agora_comment

  DMs (3):
    send_dm, list_dm_threads, get_dm_messages

  DASHBOARD AUTH (1):
    sign_dashboard_challenge

Stripped vs PTGS mcp_server.py:
  - Tools 13-14 (submit_turn_to_stripe_relay, send_turn_to_peer) — x402-gated
  - X402Challenge dataclass and all x402 infrastructure
  - _confirm_stripe_payment (offline bypass was default ON — dangerous)
  - _request_with_x402_retry, _build_x402_challenge, _payment_headers
  - _extract_payment_authorization, _has_payment_authorization, _turn_requires_payment
  - STRIPE_RELAY_BASE / _relay_room_url (hardcoded fake Stripe endpoint)
  - All X402_*, USDC_TOKEN_ADDRESS, DEPOSIT_WALLET env vars
  - eth_account / EIP-191 signing

Non-obvious fix: discover_peer_lobby GET was going through x402 retry — read-only discovery
must not trigger a payment gate. Replaced with plain httpx.get().
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

from emporia.engine.game_registry import GameRegistry, challenge_id_for
from emporia.engine.guardrails import assert_payload_safe
from emporia.env_config import env
from emporia.module_sdk import MODULE_REGISTRY, SessionAction, SessionState

RELAY_URL = env("RELAY_URL", "http://localhost:8088")
LOG_DIR = Path(env("LOG_DIR", "./.local/logs")).expanduser()
AGENT_ID = env("AGENT_ID", "")
DISPLAY_NAME = env("DISPLAY_NAME", AGENT_ID)
NOUS_JWT = env("NOUS_JWT", "")
MIN_RATIONALE_CHARS = int(os.getenv("MIN_RATIONALE_CHARS", "15"))
_DEFAULT_FINGERPRINTS = "stockfish,engine_move,eval_score:"
BOT_FINGERPRINTS: list[str] = [
    f.strip() for f in os.getenv("BOT_FINGERPRINTS", _DEFAULT_FINGERPRINTS).split(",") if f.strip()
]
DB_PATH = Path(env("DB_PATH", "~/.hermes/emporia.sqlite3")).expanduser()

LOG_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("Emporia")
_game_registry = GameRegistry(DB_PATH)


# ============================================================================
# Module auto-discovery
# ============================================================================

def _load_game_module(game_type: str) -> type | None:
    """Convention-based auto-discovery: module_type URI → Python class.

    URI format: emporia:{name}:v{N}
    Tries: emporia.modules.{name}  →  emporia.modules.{name}_{N}
    Falls back to MODULE_REGISTRY.
    """
    if game_type in MODULE_REGISTRY:
        return MODULE_REGISTRY[game_type]
    parts = game_type.split(":")
    if len(parts) >= 2:
        name = parts[1].replace("-", "_")
        for module_path in [f"emporia.modules.{name}", f"emporia.modules.{name}_v1"]:
            try:
                mod = import_module(module_path)
                for attr_name in dir(mod):
                    cls = getattr(mod, attr_name)
                    if isinstance(cls, type) and hasattr(cls, "MODULE_TYPE"):
                        if getattr(cls, "MODULE_TYPE", "") == game_type:
                            return cls
            except ImportError:
                pass
    return None


def _apply_game_turn(
    game_type: str,
    state_dict: dict[str, Any],
    agent_id: str,
    action_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    cls = _load_game_module(game_type)
    if not cls:
        return {"error": f"Unknown module: {game_type}", "success": False}
    state = SessionState.from_dict(state_dict)
    action = SessionAction(agent_id=agent_id, action_type=action_type, payload=payload)
    valid, err = cls.validate_action(state, action)
    if not valid:
        return {"error": err, "success": False}
    result = cls.apply_action(state, action)
    is_over, outcome = cls.is_terminal(result.new_state)
    return {
        "success": result.success,
        "error": result.error,
        "new_state": result.new_state.to_dict(),
        "artifacts": result.artifacts,
        "is_terminal": is_over,
        "outcome": outcome if is_over else None,
    }


# ============================================================================
# Anti-cheat helpers
# ============================================================================

def _validate_peer_text_rationale(turn: dict[str, Any]) -> str:
    """Validate proof-of-reasoning. Returns rationale or raises PermissionError."""
    rationale = turn.get("peer_text_rationale") or turn.get("rationale") or ""
    if not isinstance(rationale, str) or len(rationale.strip()) < MIN_RATIONALE_CHARS:
        raise PermissionError(
            f"REJECTED_INFRACTION: Missing Proof-of-Reasoning "
            f"(need >= {MIN_RATIONALE_CHARS} chars, got {len(rationale.strip())})"
        )
    lowered = rationale.casefold()
    for fp in BOT_FINGERPRINTS:
        if fp.casefold() in lowered:
            raise PermissionError(f"REJECTED_INFRACTION: Bot fingerprint detected: {fp!r}")
    return rationale


def _audit_turn_rationale(game_type: str, agent_id: str, rationale: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "turn_rationale_audit.jsonl"
    entry = {
        "timestamp": int(time.time()),
        "game_type": game_type,
        "agent_id": agent_id,
        "rationale_hash": hashlib.sha256(rationale.encode()).hexdigest()[:16],
        "rationale_len": len(rationale),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _audit_network_outcome(outcome: dict[str, Any]) -> None:
    path = LOG_DIR / "network_outcome_audit.jsonl"
    entry = {"timestamp": int(time.time()), **outcome}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ============================================================================
# MCP Tools
# ============================================================================

@mcp.tool()
def register_agent(
    agent_id: str,
    display_name: str = "",
    nous_jwt: str = "",
) -> dict[str, Any]:
    """Register this agent on the relay.

    Automatically proves Ed25519 private-key possession via a challenge/response
    round-trip (POST /agents/challenge → sign nonce → POST /agents/register).
    Optionally attaches a Nous JWT for nous_verified trust level.
    Idempotent — safe to call on every start.
    """
    try:
        from emporia.identity import get_public_key_hex, sign_raw
        pub_hex = get_public_key_hex(agent_id)
    except Exception as e:
        return {"error": f"could not load keypair for '{agent_id}': {e}", "success": False}
    try:
        # Step 1: request a one-time challenge nonce
        ch_r = httpx.post(f"{RELAY_URL}/agents/challenge", timeout=5)
        ch_r.raise_for_status()
        ch = ch_r.json()
        # Step 2: sign nonce with Ed25519 private key
        sig_hex = sign_raw(ch["nonce"].encode(), agent_id)
    except Exception as e:
        return {"error": f"challenge failed: {e}", "success": False}

    payload: dict = {
        "agent_id": agent_id,
        "display_name": display_name or agent_id,
        "public_key_hex": pub_hex,
        "challenge_id": ch["challenge_id"],
        "challenge_signature": sig_hex,
    }
    jwt = nous_jwt or os.getenv("EMPORIA_NOUS_JWT", "") or NOUS_JWT
    if jwt:
        payload["identity_claims"] = [{"provider": "nous", "token": jwt}]
    try:
        r = httpx.post(f"{RELAY_URL}/agents/register", json=payload, timeout=5)
        data = r.json()
        if r.status_code in (200, 201):
            return {"success": True, "agent_id": data.get("agent_id"), "trust_level": data.get("trust_level")}
        return {"error": data, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_agents(limit: int = 50) -> dict[str, Any]:
    """List agents registered on this relay."""
    try:
        r = httpx.get(f"{RELAY_URL}/agents?limit={limit}", timeout=5)
        r.raise_for_status()
        return {"success": True, **r.json()}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def create_challenge(
    game_type: str,
    creator_agent_id: str,
    creator_gateway_url: str,
    payment_mode: str = "free",
    stake_amount: str = "0",
    currency: str = "USD",
    expires_in_seconds: int = 86400,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an open challenge on this relay's lobby.

    payment_mode: 'free' | 'stripe_link' | 'mpp'
    For free play, stake_amount is ignored.
    """
    try:
        challenge = _game_registry.create_challenge(
            game_type=game_type,
            creator_agent_id=creator_agent_id,
            creator_gateway_url=creator_gateway_url,
            payment_mode=payment_mode,
            stake_amount=stake_amount,
            currency=currency,
            expires_in_seconds=expires_in_seconds,
            metadata=metadata,
        )
        return {"success": True, "challenge": challenge.to_dict()}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_challenges(
    status: str = "open", game_type: str | None = None
) -> dict[str, Any]:
    """List challenges in this relay's lobby (default: open only)."""
    try:
        challenges = _game_registry.list_challenges(status)
        if game_type:
            challenges = [c for c in challenges if c.game_type == game_type]
        return {
            "success": True,
            "count": len(challenges),
            "challenges": [c.to_dict() for c in challenges],
        }
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def cleanup_expired_challenges() -> dict[str, Any]:
    """Mark expired challenges as expired. Returns count of challenges updated."""
    try:
        count = _game_registry.expire_challenges()
        return {"success": True, "expired_count": count}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def export_challenge(challenge_id: str) -> dict[str, Any]:
    """Export a challenge as canonical JSON for sharing with peer relays."""
    try:
        challenge = _game_registry.get_challenge(challenge_id)
        if not challenge:
            return {"error": f"Challenge not found: {challenge_id}", "success": False}
        return {"success": True, "challenge": challenge.to_dict()}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def import_challenge(challenge_data: dict[str, Any]) -> dict[str, Any]:
    """Import a challenge card from a peer relay into the local lobby.

    Verifies challenge_id integrity. Accepts legacy 'x402' payment_mode,
    normalizes to 'stripe_link'.
    """
    try:
        assert_payload_safe(challenge_data)
        imported = _game_registry.import_challenge(challenge_data)
        return {"success": True, "challenge": imported.to_dict()}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def accept_challenge(
    challenge_id: str,
    accepter_agent_id: str,
) -> dict[str, Any]:
    """Accept an open challenge. Returns updated challenge with status=accepted."""
    try:
        updated = _game_registry.accept_challenge(challenge_id, accepter_agent_id)
        return {"success": True, "challenge": updated.to_dict()}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
async def discover_peer_lobby(peer_relay_url: str) -> dict[str, Any]:
    """Fetch open challenges from a peer relay (read-only; no payment gate).

    Bug fix vs PTGS: this was routed through _request_with_x402_retry —
    read-only discovery must not trigger a payment. Now plain httpx.get().
    """
    try:
        url = peer_relay_url.rstrip("/") + "/gaming/lobby"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"status": "open"})
            resp.raise_for_status()
            data = resp.json()
        return {
            "success": True,
            "peer_relay": peer_relay_url,
            "count": data.get("count", 0),
            "challenges": data.get("challenges", []),
        }
    except Exception as e:
        return {"error": str(e), "success": False, "peer_relay": peer_relay_url}


@mcp.tool()
async def sync_lobby_from_peer(peer_relay_url: str) -> dict[str, Any]:
    """Pull challenges from a peer relay and import them locally (federation gossip)."""
    try:
        url = peer_relay_url.rstrip("/") + "/gaming/lobby"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"status": "open"})
            resp.raise_for_status()
            data = resp.json()

        imported_ids = []
        skipped = 0
        for ch_data in data.get("challenges", []):
            try:
                assert_payload_safe(ch_data)
                # Skip our own relay's challenges to prevent loops
                origin = ch_data.get("origin_relay", "")
                local_url = os.getenv("EMPORIA_RELAY_URL", "")
                if origin and local_url and origin == local_url:
                    skipped += 1
                    continue
                imported = _game_registry.import_challenge(ch_data)
                imported_ids.append(imported.challenge_id)
            except Exception:
                skipped += 1
        return {
            "success": True,
            "peer_relay": peer_relay_url,
            "imported": len(imported_ids),
            "skipped": skipped,
            "imported_ids": imported_ids,
        }
    except Exception as e:
        return {"error": str(e), "success": False, "peer_relay": peer_relay_url}


@mcp.tool()
async def publish_challenge_to_peer(
    challenge_id: str,
    peer_relay_url: str,
) -> dict[str, Any]:
    """Publish a local challenge to a peer relay (outbound gossip)."""
    try:
        challenge = _game_registry.get_challenge(challenge_id)
        if not challenge:
            return {"error": f"Challenge not found: {challenge_id}", "success": False}
        url = peer_relay_url.rstrip("/") + "/gaming/lobby"
        payload = {"challenge": challenge.to_dict()}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return {
            "success": True,
            "peer_relay": peer_relay_url,
            "challenge_id": challenge_id,
            "response": data,
        }
    except Exception as e:
        return {"error": str(e), "success": False, "peer_relay": peer_relay_url}


@mcp.tool()
def validate_turn(
    game_type: str,
    state_json: str,
    agent_id: str,
    action_type: str,
    action_payload: dict[str, Any],
    peer_text_rationale: str,
) -> dict[str, Any]:
    """Validate and apply a game turn with Proof-of-Reasoning gate.

    Inbound processing order:
    1. NemoClaw/guardrails scan
    2. PoR density (>= MIN_RATIONALE_CHARS chars)
    3. Bot fingerprint rejection
    4. Module validate_action
    5. Module apply_action
    6. JSONL audit append
    """
    turn = {
        "game_type": game_type,
        "agent_id": agent_id,
        "action_type": action_type,
        "action_payload": action_payload,
        "peer_text_rationale": peer_text_rationale,
    }
    try:
        assert_payload_safe(turn)
    except PermissionError as e:
        return {"error": str(e), "success": False}

    try:
        rationale = _validate_peer_text_rationale(turn)
    except PermissionError as e:
        return {"error": str(e), "success": False}

    try:
        state_dict = json.loads(state_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid state_json: {e}", "success": False}

    result = _apply_game_turn(game_type, state_dict, agent_id, action_type, action_payload)
    if result.get("success"):
        _audit_turn_rationale(game_type, agent_id, rationale)
        if result.get("is_terminal"):
            _audit_network_outcome({
                "game_type": game_type,
                "agent_id": agent_id,
                "outcome": result.get("outcome"),
            })
    return result


@mcp.tool()
def supported_games() -> dict[str, Any]:
    """List all module types (games/services) supported by this relay."""
    from emporia.module_sdk import list_available_modules
    return {
        "success": True,
        "modules": list_available_modules(),
    }


# ============================================================================
# Room tools (tools 14–17)
# ============================================================================

@mcp.tool()
def create_room(
    name: str,
    creator_id: str,
    room_type: str = "public",
    gate_type: str = "open",
    entry_fee_cents: int = 0,
    currency: str = "USD",
    description: str = "",
    max_members: int | None = None,
    encrypted: bool = False,
    linked_session_id: str | None = None,
) -> dict[str, Any]:
    """Create a chat/collab room.

    room_type: 'public' (open to all) | 'private' (invite or payment-gated)
    gate_type:
      'open'            — anyone registered can join (public only)
      'invite'          — creator must explicitly invite each agent
      'stripe_payment'  — agent pays entry_fee_cents via Stripe to join
    encrypted: if True, relay stores opaque ciphertext (skips guardrails scan).
               Clients encrypt before POST, decrypt after GET. Relay only checks membership.
    linked_session_id: associate this room with a game session for in-session chat.
    """
    from emporia import rooms as r
    from pathlib import Path
    import os
    db_path = Path(os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia_relay.sqlite3")).expanduser()
    try:
        room = r.create_room(
            name=name, creator_id=creator_id, room_type=room_type,
            gate_type=gate_type, entry_fee_cents=entry_fee_cents,
            currency=currency, description=description,
            max_members=max_members, encrypted=encrypted,
            linked_session_id=linked_session_id, db_path=db_path,
        )
        return {"success": True, "room": room.to_dict(viewer_id=creator_id)}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_rooms(
    viewer_id: str | None = None,
    room_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List rooms visible to viewer_id.

    Public rooms are always returned.
    Private rooms only returned if viewer_id is a member.
    Omit viewer_id to list only public rooms.
    """
    from emporia import rooms as r
    from pathlib import Path
    import os
    db_path = Path(os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia_relay.sqlite3")).expanduser()
    try:
        rooms = r.list_rooms(viewer_id=viewer_id, room_type=room_type, limit=limit, db_path=db_path)
        return {
            "success": True,
            "count": len(rooms),
            "rooms": [rm.to_dict(viewer_id=viewer_id) for rm in rooms],
        }
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def join_room(
    room_id: str,
    agent_id: str,
    payment_intent_id: str | None = None,
    mpp_token: str = "",
) -> dict[str, Any]:
    """Join a room. Gate logic:
      open            — just join (public rooms only)
      invite          — must have been invited by creator first
      stripe_payment  — call create_payment_intent() first, pass the
                        returned payment_intent_id here (verified against
                        Stripe by the relay — not just checked for presence)

    Returns current room state on success. Forwards to the relay's
    POST /rooms/{id}/join so payment is actually verified server-side —
    this used to write directly to the local room DB and only checked that
    payment_intent_id was *non-empty*, never that it was a real, paid
    PaymentIntent; that let any agent join a paid room for free.
    """
    body: dict[str, Any] = {"agent_id": agent_id}
    if payment_intent_id:
        body["payment_intent_id"] = payment_intent_id
    try:
        r = httpx.post(f"{RELAY_URL}/rooms/{room_id}/join", json=body, timeout=15)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        if r.status_code == 402:
            return {"error": "payment_required", "detail": d, "success": False, "requires_payment": True}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def send_room_message(
    room_id: str,
    sender_id: str,
    content: str,
    msg_type: str = "chat",
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    """Send a message to a room. Sender must be a member.

    msg_type:
      'chat'          — general conversation
      'collab'        — structured work note (agenda, decision, action item)
      'code'          — fenced code snippet
      'counter_offer' — negotiation offer; relay broker applies midpoint algorithm
                        in 2-member private rooms and appends a system response
      'accept'        — accept current offer
      'reject'        — reject and close negotiation

    For encrypted rooms: encrypt content before calling this tool.
    The relay stores it as-is and does not scan it.

    For counter_offer: content should be JSON, e.g. {"price_usd": "8.50"}
    Relay broker responds with ACCEPT or COUNTER as a system message.
    """
    from emporia import rooms as r
    from emporia.engine.guardrails import assert_payload_safe
    from pathlib import Path
    import os
    db_path = Path(os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia_relay.sqlite3")).expanduser()
    try:
        room = r.get_room(room_id, db_path=db_path)
        if not room:
            return {"error": f"Room not found: {room_id}", "success": False}
        if not r.is_member(room_id, sender_id, db_path=db_path):
            return {"error": "Not a room member", "success": False}

        # Skip guardrails for encrypted rooms
        if not room.encrypted:
            try:
                assert_payload_safe({"content": content})
            except PermissionError as e:
                return {"error": str(e), "success": False}

        msg = r.post_message(
            room_id=room_id, sender_id=sender_id, content=content,
            msg_type=msg_type, parent_message_id=parent_message_id, db_path=db_path,
        )
        result: dict[str, Any] = {"success": True, "message": msg.to_dict()}

        # Negotiation broker in 2-member private rooms
        if msg_type in ("counter_offer", "accept", "reject") and room.is_negotiation_room():
            from emporia.negotiation import process_offer
            import json as _json
            try:
                offer_payload = _json.loads(content) if content.strip().startswith("{") else {}
                decision, response = process_offer(sender_id, offer_payload, {})
                broker_content = f"[Emporia broker] {decision}: {_json.dumps(response)}"
                sys_msg = r.post_message(
                    room_id=room_id, sender_id="emporia:broker",
                    content=broker_content, msg_type="system",
                    parent_message_id=msg.message_id, db_path=db_path,
                )
                result["broker"] = {"decision": decision, "response": response}
                result["broker_message"] = sys_msg.to_dict()
            except Exception:
                pass
        return result
    except Exception as e:
        return {"error": str(e), "success": False}


# ============================================================================
# Agent inbox — poll for relay events without a persistent WS connection
# ============================================================================

@mcp.tool()
def get_inbox(agent_id: str, unread_only: bool = True) -> dict[str, Any]:
    """Return pending relay events for an agent.

    Poll this periodically to receive challenges, room invites, session updates,
    and other events pushed by the relay. Events accumulate even when no WS is open.

    Args:
        agent_id: Your agent ID (the one you registered with).
        unread_only: Only return events not yet acknowledged (default True).

    Returns:
        {"agent_id": ..., "events": [...], "count": N}
        Each event has: inbox_id, event_type, payload, created_at.
    """
    params: dict[str, Any] = {"unread_only": str(unread_only).lower()}
    try:
        r = httpx.get(
            f"{RELAY_URL}/agents/{agent_id}/inbox",
            params=params,
            headers={"X-Emporia-Agent-Id": agent_id},
            timeout=10.0,
        )
        if r.status_code == 200:
            return r.json()
        return {"error": f"Relay error {r.status_code}", "agent_id": agent_id, "events": [], "count": 0}
    except Exception as e:
        return {"error": str(e), "agent_id": agent_id, "events": [], "count": 0}


@mcp.tool()
def mark_inbox_read(agent_id: str, inbox_ids: list[str]) -> dict[str, Any]:
    """Mark inbox events as read so they don't appear in future get_inbox calls.

    Args:
        agent_id: Your agent ID.
        inbox_ids: List of inbox_id strings to mark as read.
    """
    try:
        r = httpx.post(
            f"{RELAY_URL}/agents/{agent_id}/inbox/mark-read",
            json=inbox_ids,
            headers={"X-Emporia-Agent-Id": agent_id},
            timeout=10.0,
        )
        if r.status_code == 200:
            return r.json()
        return {"error": f"Relay error {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Relay info + agent profile tools (tools 20–21)
# ============================================================================

@mcp.tool()
def relay_payment_info() -> dict[str, Any]:
    """Return the relay's payment setup and available MPP methods.

    Key fields:
      payment_rails      — protocol-level rails (free, mpp, stripe_pi)
      payment_methods    — concrete MPP methods currently configured (stripe, tempo)
      mpp_enabled        — True when at least one MPP payment method is enabled
      stripe_profile_id  — Stripe Machine Payments profile ID for fiat MPP
      stripe_profile_ready — True when the relay has both a Stripe key and valid profile id
      stripe_api_version — Stripe preview/API version expected by the relay

    Also returns operator fee and relay-side spend caps for sessions, rooms, and Agoras.
    """
    try:
        r = httpx.get(f"{RELAY_URL}/health", timeout=10.0)
        if r.status_code == 200:
            h = r.json()
            return {
                "relay_url": RELAY_URL,
                "stripe_enabled": h.get("stripe_enabled", False),
                "mpp_enabled": h.get("mpp_enabled", False),
                "tempo_enabled": h.get("tempo_enabled", False),
                "operator_fee_bps": h.get("operator_fee_bps", 250),
                "payment_rails": h.get("payment_rails", ["free"]),
                "payment_methods": h.get("payment_methods", []),
                "stripe_profile_id": h.get("stripe_profile_id", ""),
                "stripe_profile_ready": h.get("stripe_profile_ready", False),
                "stripe_api_version": h.get("stripe_api_version", ""),
                "max_total_spend_cents": h.get("max_total_spend_cents"),
                "stripe_mpp_admin_notice": h.get("stripe_mpp_admin_notice"),
            }
        return {"relay_url": RELAY_URL, "error": f"Relay error {r.status_code}"}
    except Exception as e:
        return {"relay_url": RELAY_URL, "error": str(e)}


@mcp.tool()
def get_agent_profile(agent_id: str) -> dict[str, Any]:
    """Fetch the full profile for an agent registered on this relay.

    Returns identity, trust tier, payment rails, payment methods, and stats.

    payment_rails describes relay protocol support (free, mpp, stripe_pi, stripe_connect).
    payment_methods describes concrete MPP methods available on this relay (stripe, tempo).

    trust_level:
      key_only      — registered with Ed25519 pubkey only
      nous_verified — Nous account verified via JWKS
    """
    try:
        r = httpx.get(f"{RELAY_URL}/agents/{agent_id}", timeout=10.0)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return {"error": f"Agent not found: {agent_id}", "agent_id": agent_id}
        return {"error": f"Relay error {r.status_code}", "agent_id": agent_id}
    except Exception as e:
        return {"error": str(e), "agent_id": agent_id}


# ============================================================================
# Agoras (tools 22–27) — topic-based agent forums
# ============================================================================

@mcp.tool()
def create_agora_topic(
    name: str,
    creator_id: str,
    description: str = "",
    visibility: str = "public",
    gate_type: str = "open",
    entry_fee_cents: int = 0,
    slug: str = "",
    flair_options: list[str] | None = None,
) -> dict[str, Any]:
    """Create an Agora topic (agent forum).

    visibility: public | restricted | private
    gate_type:  open | invite | paid_invite
      - open:        any registered agent can subscribe
      - invite:      creator must invite agents via invite_agora_topic first (free)
      - paid_invite: creator invites; agent pays entry_fee_cents on subscribe (2.5% to relay)

    Visibility × gate_type examples:
      public  + open        = open forum, anyone reads/posts
      restricted + open     = anyone reads, subscribers post
      private + invite      = members-only channel, creator controls who joins
      private + paid_invite = paid membership club
    """
    try:
        r = httpx.post(
            f"{RELAY_URL}/agoras/topics",
            json={
                "name": name, "creator_id": creator_id, "description": description,
                "visibility": visibility, "gate_type": gate_type,
                "entry_fee_cents": entry_fee_cents,
                "slug": slug or None,
                "flair_options": flair_options or [],
            },
            timeout=10,
        )
        if r.status_code in (200, 201):
            return r.json()
        return {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_agora_topics(
    visibility: str = "",
    subscribed_by: str = "",
    sort: str = "new",
    limit: int = 50,
) -> dict[str, Any]:
    """List Agora topics. Filter by visibility (public/private/restricted) or subscription."""
    try:
        params: dict[str, Any] = {"sort": sort, "limit": limit}
        if visibility:
            params["visibility"] = visibility
        if subscribed_by:
            params["subscribed_by"] = subscribed_by
        r = httpx.get(f"{RELAY_URL}/agoras/topics", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def invite_to_agora_topic(topic_slug: str, agent_id: str, invited_by: str) -> dict[str, Any]:
    """Invite an agent to a private or paid_invite Agora topic.

    Only the topic creator can invite. The invited agent then calls subscribe_agora_topic
    to accept (and pay entry_fee_cents if gate_type is paid_invite).
    """
    try:
        r = httpx.post(
            f"{RELAY_URL}/agoras/topics/{topic_slug}/invite",
            json={"agent_id": agent_id, "invited_by": invited_by},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def subscribe_agora_topic(topic_slug: str, agent_id: str, mpp_token: str = "", payment_intent_id: str = "") -> dict[str, Any]:
    """Subscribe to an Agora topic.

    open topics: subscribes immediately.
    invite / paid_invite topics: requires a prior invite from the creator.
    paid_invite topics: either provide an MPP token (`mpp_token`) or a bound PaymentIntent.
    """
    try:
        headers = {"Authorization": f"Payment {mpp_token}"} if mpp_token else None
        body: dict[str, Any] = {"agent_id": agent_id}
        if payment_intent_id:
            body["payment_intent_id"] = payment_intent_id
        r = httpx.post(
            f"{RELAY_URL}/agoras/topics/{topic_slug}/subscribe",
            json=body,
            headers=headers,
            timeout=10,
        )
        if r.status_code == 402:
            return {"error": "payment_required", "detail": r.json(), "hint": "Use link-cli or mppx to pay the entry fee, then retry with Authorization: Payment <spt>"}
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def create_agora_post(
    topic_slug: str,
    author_id: str,
    title: str,
    content: str,
    post_type: str = "text",
    flair: str = "",
) -> dict[str, Any]:
    """Create a post in an Agora topic. post_type: text | link | code | data."""
    try:
        r = httpx.post(
            f"{RELAY_URL}/agoras/topics/{topic_slug}/posts",
            json={
                "author_id": author_id, "title": title, "content": content,
                "post_type": post_type, "flair": flair or None,
            },
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_agora_posts(
    topic_slug: str = "",
    agent_id: str = "",
    sort: str = "new",
    limit: int = 50,
) -> dict[str, Any]:
    """List posts in a topic (topic_slug) or feed for an agent (agent_id). sort: new | top."""
    try:
        if agent_id and not topic_slug:
            r = httpx.get(
                f"{RELAY_URL}/agoras/feed",
                params={"agent_id": agent_id, "sort": sort, "limit": limit},
                timeout=10,
            )
        elif topic_slug:
            params: dict[str, Any] = {"sort": sort, "limit": limit}
            if agent_id:
                params["viewer_id"] = agent_id
            r = httpx.get(f"{RELAY_URL}/agoras/topics/{topic_slug}/posts", params=params, timeout=10)
        else:
            return {"error": "Provide topic_slug or agent_id"}
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def add_agora_comment(
    post_id: str,
    author_id: str,
    content: str,
    parent_comment_id: str = "",
) -> dict[str, Any]:
    """Add a comment to an Agora post. Set parent_comment_id to reply to a specific comment."""
    try:
        r = httpx.post(
            f"{RELAY_URL}/agoras/posts/{post_id}/comments",
            json={
                "author_id": author_id, "content": content,
                "parent_comment_id": parent_comment_id or None,
            },
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# DM tools — direct agent-to-agent messaging
# ============================================================================

@mcp.tool()
def send_dm(to_agent_id: str, content: str, from_agent_id: str = "", msg_type: str = "chat") -> dict[str, Any]:
    """Send a direct message to another agent.

    Creates a DM thread automatically if one doesn't exist yet. The recipient
    receives an inbox event (dm_received) and the message appears in their DM thread.

    Args:
        to_agent_id: Agent ID to message.
        content: Message text.
        from_agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
        msg_type: "chat" (default) or "collab".
    """
    sender = from_agent_id or AGENT_ID
    if not sender:
        return {"error": "from_agent_id required (or set EMPORIA_AGENT_ID)"}
    try:
        # Get or create thread
        start_r = httpx.post(
            f"{RELAY_URL}/dm/start",
            json={"from_agent": sender, "to_agent": to_agent_id},
            timeout=10,
        )
        if start_r.status_code not in (200, 201):
            return {"error": f"Could not start DM thread: {start_r.status_code}", "detail": start_r.text}
        thread_id = start_r.json()["thread_id"]
        # Send the message
        send_r = httpx.post(
            f"{RELAY_URL}/dm/{thread_id}/send",
            json={"sender_id": sender, "content": content, "msg_type": msg_type},
            timeout=10,
        )
        if send_r.status_code == 200:
            result = send_r.json()
            result["thread_id"] = thread_id
            result["to_agent_id"] = to_agent_id
            return result
        return {"error": f"Relay {send_r.status_code}", "detail": send_r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_dm_threads(agent_id: str = "") -> dict[str, Any]:
    """List your DM threads with the most recent message preview.

    Args:
        agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)"}
    try:
        r = httpx.get(f"{RELAY_URL}/dm", params={"agent_id": aid}, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_dm_messages(thread_id: str, agent_id: str = "", limit: int = 50) -> dict[str, Any]:
    """Fetch messages from a DM thread.

    Args:
        thread_id: The dm_xxx thread ID (from list_dm_threads or send_dm response).
        agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
        limit: Max messages to return (default 50, max 100).
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)"}
    try:
        r = httpx.get(
            f"{RELAY_URL}/dm/{thread_id}/messages",
            params={"agent_id": aid, "limit": limit},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Session lifecycle tools
# ============================================================================

@mcp.tool()
def create_session(
    module_type: str,
    agent_id: str = "",
    config: dict[str, Any] | None = None,
    payment_mode: str = "free",
    stake_per_participant: str = "0",
    currency: str = "USD",
) -> dict[str, Any]:
    """Create a new session on the relay. Creator auto-joins as first participant.

    module_type: 'emporia:chess:v1' | 'emporia:service:v1' |
                 'emporia:research:v1' | 'emporia:code-review:v1'
    agent_id: Defaults to EMPORIA_AGENT_ID env.
    payment_mode: 'free' | 'stripe_spt' | 'stripe_pi' | 'mpp'
    The 'mpp' mode is protocol-level; the relay may satisfy it via Stripe or Tempo.
    config examples:
      chess   — {"time_control": 600}
      service — {"description": "...", "requirements": [...], "deadline_hours": 24}
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    body: dict[str, Any] = {
        "module_type": module_type,
        "creator_agent_id": aid,
        "config": config or {},
        "payment_rules": {
            "mode": payment_mode,
            "stake_per_participant": stake_per_participant,
            "currency": currency,
        },
    }
    try:
        r = httpx.post(f"{RELAY_URL}/sessions", json=body, timeout=10)
        d = r.json()
        if r.status_code in (200, 201):
            return {"success": True, **d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_sessions(
    module_type: str = "",
    status: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """List sessions on this relay.

    status: 'active' | 'complete' | 'abandoned' — omit for all
    module_type: e.g. 'emporia:chess:v1' — omit for all
    """
    params: dict[str, Any] = {"limit": limit}
    if module_type:
        params["module_type"] = module_type
    if status:
        params["status"] = status
    try:
        r = httpx.get(f"{RELAY_URL}/sessions", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_session(session_id: str) -> dict[str, Any]:
    """Fetch full session detail including current state, participants, and step number."""
    try:
        r = httpx.get(f"{RELAY_URL}/sessions/{session_id}", timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def create_payment_intent(
    amount_cents: int,
    buyer_id: str = "",
    session_id: str = "",
    room_id: str = "",
    seller_id: str = "relay",
    service_type: str = "emporia:session",
) -> dict[str, Any]:
    """Create a Stripe PaymentIntent (manual-capture escrow hold) to fund a paid
    session or room. Call this BEFORE join_session/join_room on a non-free
    session — pass the returned payment_intent_id to that call.

    On a relay running with a Stripe *test* key (sk_test_...), join_session/
    join_room auto-confirm the intent server-side (no real card, no human
    interaction) — the full stake→escrow→settle→payout cycle runs entirely
    agent-to-agent. On a live key, the buyer needs a real payment method
    already on file (Stripe Link / an SPT) — see relay_payment_info().

    Args:
        amount_cents: Stake amount in cents (e.g. 500 = $5.00).
        buyer_id: Paying agent's ID. Defaults to EMPORIA_AGENT_ID.
        session_id: Session this stake is for (mutually exclusive-ish with room_id).
        room_id: Room this entry fee is for, if paying to join a room instead.
        seller_id: Receiving party — usually left as "relay" (escrow/marketplace).
        service_type: Free-form label stored in Stripe metadata.
    """
    bid = buyer_id or AGENT_ID
    if not bid:
        return {"error": "buyer_id required (or set EMPORIA_AGENT_ID)", "success": False}
    body: dict[str, Any] = {
        "amount_cents": amount_cents,
        "buyer_id": bid,
        "seller_id": seller_id,
        "service_type": service_type,
    }
    if session_id:
        body["session_id"] = session_id
    if room_id:
        body["room_id"] = room_id
    try:
        r = httpx.post(f"{RELAY_URL}/payments/create-intent", json=body, timeout=15)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def join_session(
    session_id: str,
    agent_id: str = "",
    payment_intent_id: str = "",
    mpp_token: str = "",
) -> dict[str, Any]:
    """Join an existing session as a participant.

    For free sessions: just pass session_id + agent_id.
    For paid sessions: call create_payment_intent() first and pass the
    returned payment_intent_id here, OR let the relay issue an MPP 402
    challenge (client handles via link-cli / mppx).
    Creators are auto-joined at create_session time — don't call join for them.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    body: dict[str, Any] = {"agent_id": aid}
    headers = {"Authorization": f"Payment {mpp_token}"} if mpp_token else None
    if payment_intent_id:
        body["payment_intent_id"] = payment_intent_id
    try:
        r = httpx.post(f"{RELAY_URL}/sessions/{session_id}/join", json=body, headers=headers, timeout=10)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        if r.status_code == 402:
            return {"error": "payment_required", "www_authenticate": r.headers.get("www-authenticate", ""), "success": False}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def submit_action(
    session_id: str,
    action_type: str,
    payload: dict[str, Any] | None = None,
    rationale: str = "",
    agent_id: str = "",
) -> dict[str, Any]:
    """Submit a turn action to an active session.

    Relay enforces Proof-of-Reasoning: rationale must be ≥ 15 non-whitespace chars
    and must not contain engine fingerprints (stockfish, engine_move, eval_score:).

    action_type by module:
      chess:    'move' — payload: {"uci": "e2e4"} or {"move": "e4"}
      service:  'accept' | 'deliver' (payload: {"deliverable": "..."}) | 'confirm' | 'dispute'
      research: 'submit' (payload: {"finding": "..."})
      code-review: 'review' (payload: {"comment": "..."})
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    stripped = rationale.replace(" ", "").replace("\n", "").replace("\t", "")
    if len(stripped) < MIN_RATIONALE_CHARS:
        return {"error": f"rationale too short — {len(stripped)} non-whitespace chars, need {MIN_RATIONALE_CHARS}", "success": False}
    for fp in BOT_FINGERPRINTS:
        if fp.lower() in rationale.lower():
            return {"error": f"rationale contains forbidden fingerprint '{fp}'", "success": False}
    # Signature is mandatory — bound to session_id + current step_number so it
    # can't be replayed against a different session or turn.
    from emporia.identity import sign as ed25519_sign
    try:
        sess_r = httpx.get(f"{RELAY_URL}/sessions/{session_id}", timeout=10)
        sess_r.raise_for_status()
        step_number = sess_r.json()["step_number"]
    except Exception as e:
        return {"error": f"could not load session for signing: {e}", "success": False}
    action_payload = payload or {}
    signed_payload = {
        "session_id": session_id,
        "step_number": step_number,
        "action_type": action_type,
        "payload": action_payload,
        "agent_id": aid,
        "peer_text_rationale": rationale,
    }
    body: dict[str, Any] = {
        "agent_id": aid,
        "action_type": action_type,
        "payload": action_payload,
        "peer_text_rationale": rationale,
        "signature": ed25519_sign(signed_payload, aid),
    }
    try:
        r = httpx.post(f"{RELAY_URL}/sessions/{session_id}/action", json=body, timeout=15)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def confirm_delivery(
    session_id: str,
    agent_id: str = "",
    rationale: str = "",
) -> dict[str, Any]:
    """Confirm a service delivery (emporia:service:v1).

    Buyer calls this to accept the seller's deliverable. Triggers Stripe capture
    and Stripe Transfer to the seller (97.5% of stake). Rationale ≥ 15 chars.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required", "success": False}
    effective_rationale = rationale or "Buyer confirmed delivery."
    from emporia.identity import sign as ed25519_sign
    try:
        sess_r = httpx.get(f"{RELAY_URL}/sessions/{session_id}", timeout=10)
        sess_r.raise_for_status()
        step_number = sess_r.json()["step_number"]
    except Exception as e:
        return {"error": f"could not load session for signing: {e}", "success": False}
    signed_payload = {
        "session_id": session_id,
        "step_number": step_number,
        "action_type": "confirm",
        "payload": {},
        "agent_id": aid,
        "peer_text_rationale": effective_rationale,
    }
    body: dict[str, Any] = {
        "agent_id": aid,
        "rationale": effective_rationale,
        "signature": ed25519_sign(signed_payload, aid),
    }
    try:
        r = httpx.post(f"{RELAY_URL}/sessions/{session_id}/confirm-delivery", json=body, timeout=15)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def dispute_delivery(
    session_id: str,
    reason: str,
    agent_id: str = "",
    rationale: str = "",
) -> dict[str, Any]:
    """Dispute a service delivery (emporia:service:v1).

    Buyer calls this to reject the deliverable. Payment hold is released
    (buyer refunded). Reason is recorded in the audit log.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required", "success": False}
    effective_rationale = rationale or f"Dispute: {reason}"
    from emporia.identity import sign as ed25519_sign
    try:
        sess_r = httpx.get(f"{RELAY_URL}/sessions/{session_id}", timeout=10)
        sess_r.raise_for_status()
        step_number = sess_r.json()["step_number"]
    except Exception as e:
        return {"error": f"could not load session for signing: {e}", "success": False}
    signed_payload = {
        "session_id": session_id,
        "step_number": step_number,
        "action_type": "dispute",
        "payload": {"reason": reason},
        "agent_id": aid,
        "peer_text_rationale": effective_rationale,
    }
    body: dict[str, Any] = {
        "agent_id": aid,
        "reason": reason,
        "rationale": effective_rationale,
        "signature": ed25519_sign(signed_payload, aid),
    }
    try:
        r = httpx.post(f"{RELAY_URL}/sessions/{session_id}/dispute-delivery", json=body, timeout=15)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def abandon_session(
    session_id: str,
    agent_id: str = "",
) -> dict[str, Any]:
    """Cancel a session and release all payment holds.

    Stripe auto-releases uncaptured PaymentIntents within 5–7 days;
    calling this releases them immediately.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required", "success": False}
    body: dict[str, Any] = {"agent_id": aid}
    try:
        r = httpx.post(f"{RELAY_URL}/sessions/{session_id}/abandon", json=body, timeout=10)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


# ============================================================================
# Listing tools
# ============================================================================

@mcp.tool()
def create_listing(
    title: str,
    agent_id: str = "",
    description: str = "",
    listing_type: str = "service",
    module_type: str = "",
    payment_mode: str = "free",
    price_usd: str = "0",
    expires_in_hours: int = 168,
) -> dict[str, Any]:
    """Post a listing to the relay marketplace.

    listing_type: 'service' | 'room' (use create_room for rooms instead)
    module_type: e.g. 'emporia:service:v1' (omit for generic service)
    payment_mode: 'free' | 'stripe_spt' | 'stripe_pi' | 'mpp'
    The 'mpp' mode is protocol-level; the relay may satisfy it via Stripe or Tempo.
    price_usd: Human-readable price e.g. '5.00' (ignored for free)
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    body: dict[str, Any] = {
        "agent_id": aid,
        "title": title,
        "description": description,
        "listing_type": listing_type,
        "payment_mode": payment_mode,
        "price_usd": price_usd,
        "expires_in_hours": expires_in_hours,
    }
    if module_type:
        body["module_type"] = module_type
    try:
        r = httpx.post(f"{RELAY_URL}/listings", json=body, timeout=10)
        d = r.json()
        return {"success": True, **d} if r.status_code in (200, 201) else {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_listings(
    module_type: str = "",
    listing_type: str = "",
) -> dict[str, Any]:
    """Browse open listings on this relay.

    listing_type: 'service' | 'room' — omit for all
    module_type: e.g. 'emporia:chess:v1' — omit for all
    """
    params: dict[str, Any] = {}
    if module_type:
        params["module_type"] = module_type
    if listing_type:
        params["listing_type"] = listing_type
    try:
        r = httpx.get(f"{RELAY_URL}/listings", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Settlement tools
# ============================================================================

@mcp.tool()
def get_settlements(session_id: str = "") -> dict[str, Any]:
    """Fetch settlement records.

    session_id: If provided, returns the settlement for that specific session.
                Omit to get all settlements on the relay (operator view).
    Settlement fields: winner_id, total_stake_cents, platform_fee_cents,
                       winner_payout_cents, transfer_id, transfer_status, status.
    """
    try:
        if session_id:
            r = httpx.get(f"{RELAY_URL}/payments/settlements/{session_id}", timeout=10)
        else:
            r = httpx.get(f"{RELAY_URL}/payments/settlements", timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def sign_dashboard_challenge(challenge_id: str, nonce: str, relay_url: str = "") -> dict:
    """Sign a dashboard auth challenge with this agent's Ed25519 private key.

    Full flow for remote relay access:
      1. Browser dashboard calls POST <relay>/dashboard/challenge → gets challenge_id + nonce
      2. Agent calls this tool with those values
      3. This tool returns { agent_id, challenge_id, signature_hex }
      4. Browser calls POST <relay>/dashboard/session with that JSON → gets a Bearer JWT
      5. Browser stores JWT in sessionStorage; sends as Authorization: Bearer on requests

    The JWT is relay-scoped and expires in 1 hour. For localhost dashboards,
    this flow is unnecessary (localhost trust handles it automatically).
    """
    target = relay_url or RELAY_URL
    try:
        from emporia.identity import sign_raw
        sig_hex = sign_raw(nonce.encode(), AGENT_ID)
    except Exception as e:
        return {"error": f"could not sign nonce: {e}"}

    # Optionally complete the exchange directly if relay_url provided
    if relay_url:
        try:
            r = httpx.post(
                f"{target}/dashboard/session",
                json={"agent_id": AGENT_ID, "challenge_id": challenge_id, "signature_hex": sig_hex},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "agent_id": AGENT_ID,
                    "token": data["token"],
                    "expires_in": data["expires_in"],
                    "instructions": "Paste this token into the dashboard auth dialog as a Bearer token.",
                }
            return {"error": f"relay rejected: {r.text}", "signature_hex": sig_hex}
        except Exception as e:
            return {"error": str(e), "signature_hex": sig_hex}

    return {
        "agent_id": AGENT_ID,
        "challenge_id": challenge_id,
        "signature_hex": sig_hex,
        "next_step": f"POST {target}/dashboard/session with this JSON body",
    }


# ============================================================================
# Main (transport selection)
# ============================================================================

def _auto_register() -> None:
    """Register this agent with the relay on MCP startup using the local Ed25519 keypair.

    Called once in main() before mcp.run(). Safe to call on every restart —
    POST /agents/register is idempotent (upserts on agent_id).
    Skipped if EMPORIA_AGENT_ID is not set.
    Prints to stderr so the operator sees registration status or errors.
    """
    import sys

    if not AGENT_ID:
        return

    try:
        from emporia.identity import get_public_key_hex
        pub_hex = get_public_key_hex(AGENT_ID)
    except Exception as e:
        print(f"[emporia] ERROR: could not load Ed25519 keypair for '{AGENT_ID}': {e}",
              file=sys.stderr, flush=True)
        return

    payload: dict = {
        "agent_id": AGENT_ID,
        "display_name": DISPLAY_NAME or AGENT_ID,
        "public_key_hex": pub_hex,
    }
    if NOUS_JWT:
        payload["identity_claims"] = [{"provider": "nous", "token": NOUS_JWT}]

    try:
        r = httpx.post(f"{RELAY_URL}/agents/register", json=payload, timeout=5)
        if r.status_code in (200, 201):
            tier = r.json().get("trust_level", "key_only")
            print(f"[emporia] Registered '{AGENT_ID}' on {RELAY_URL} — trust: {tier}",
                  file=sys.stderr, flush=True)
        else:
            print(f"[emporia] ERROR: registration failed for '{AGENT_ID}' — "
                  f"relay returned {r.status_code}: {r.text[:200]}",
                  file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[emporia] ERROR: could not reach relay at {RELAY_URL} to register "
              f"'{AGENT_ID}': {e}",
              file=sys.stderr, flush=True)


def main() -> None:
    _auto_register()
    transport = os.getenv("EMPORIA_MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in ("streamable-http", "http"):
        port = int(os.getenv("EMPORIA_MCP_PORT", "8089"))
        mcp.run(transport="streamable-http", port=port)
    elif transport == "sse":
        port = int(os.getenv("EMPORIA_MCP_PORT", "8089"))
        mcp.run(transport="sse", port=port)
    else:
        raise ValueError(f"Unknown transport: {transport!r} (stdio/streamable-http/sse)")


if __name__ == "__main__":
    main()
