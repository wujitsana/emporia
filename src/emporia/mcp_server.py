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

  AGORAS (9):
    create_agora_topic, list_agora_topics, invite_to_agora_topic,
    subscribe_agora_topic, create_agora_post, list_agora_posts,
    add_agora_comment, vote_agora_post, vote_agora_comment

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
import logging
import os
import time
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

from emporia.engine.game_registry import GameRegistry
from emporia.engine.guardrails import assert_payload_safe
from emporia.env_config import env
from emporia.logging_config import configure_logging
from emporia.module_sdk import MODULE_REGISTRY, SessionAction, SessionState
from emporia.wire import DEFAULT_MSG_TYPE, ERR_DUPLICATE_NONCE, error_code

# P22: module-import time, not inside main() -- this process's stdout is the
# MCP stdio protocol channel itself; logging_config defaults to stderr, which
# is already what this file's own _auto_register() used directly before this.
configure_logging()
logger = logging.getLogger("emporia.mcp")

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
# Dashboard Bearer auth for our own outbound calls
# ============================================================================
# On a relay running with EMPORIA_TRUST_LOCAL_HEADER_AUTH off (the shipped default —
# header trust is loopback/Docker-gateway-only), _dashboard_agent_id() only accepts a
# Bearer JWT. Without one, our own reads of a participants-only session (any research/
# service/code-review session — the paid modules) 403, including the pre-fetch this
# process needs just to read step_number and sign its own turn. This obtains and caches
# that token via the same challenge/sign_raw/session exchange sign_dashboard_challenge()
# performs on behalf of a remote browser dashboard — this is the equivalent self-initiated
# flow for MCP's own outbound HTTP calls.
_dashboard_token_cache: dict[str, tuple[str, float]] = {}


def _dashboard_bearer_headers(relay_url: str = "", agent_id: str = "") -> dict[str, str]:
    """{"Authorization": "Bearer <jwt>"} for `agent_id` (default AGENT_ID), cached until
    near expiry. Returns {} — not raises — on any failure (no local key, relay
    unreachable, agent not registered yet), so callers degrade to the pre-N1-3 behavior
    of an unauthenticated request rather than breaking calls to relays where auth isn't
    required (header-trusted local dev loop, or a public session)."""
    target = relay_url or RELAY_URL
    aid = agent_id or AGENT_ID
    if not aid:
        return {}
    cache_key = f"{target}|{aid}"
    cached = _dashboard_token_cache.get(cache_key)
    now = time.time()
    if cached and cached[1] > now + 30:
        return {"Authorization": f"Bearer {cached[0]}"}
    try:
        from emporia.identity import sign_raw
        from emporia.wire import dashboard_session_body
        ch = httpx.post(f"{target}/dashboard/challenge", timeout=10).json()
        sig_hex = sign_raw(ch["nonce"].encode(), aid)
        r = httpx.post(
            f"{target}/dashboard/session",
            json=dashboard_session_body(aid, ch["challenge_id"], sig_hex),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _dashboard_token_cache[cache_key] = (data["token"], now + data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {data['token']}"}
    except Exception:
        return {}


def _invalidate_dashboard_bearer(relay_url: str = "", agent_id: str = "") -> None:
    """Drop a cached token so the next _dashboard_bearer_headers() call re-authenticates.
    Call after a request comes back 401/403 despite sending a cached token — expiry
    early server-side, or a relay restart that rotated the JWT secret (now persisted,
    see identity.load_or_generate_relay_jwt_secret, but keep this as defense-in-depth)."""
    target = relay_url or RELAY_URL
    aid = agent_id or AGENT_ID
    _dashboard_token_cache.pop(f"{target}|{aid}", None)


def _bearer_request(
    method: str, url: str, *, agent_id: str = "", payment_token: str = "", **kwargs: Any,
) -> httpx.Response:
    """httpx.{get,post,delete}(url, ...) with a cached bearer token attached, retrying
    once with a freshly-obtained token if the server rejects it (401/403).

    `payment_token` (P95): an SPT to pay with. It takes `Authorization` and the
    identity moves to `X-Emporia-Authorization`, so the relay knows both who is
    paying and with what — before this, a paid call could carry only the
    payment and the relay could not tell who sent it. Retrying a paid call is
    safe: the SPT's Idempotency-Key (P88) returns the same PaymentIntent."""
    fn = getattr(httpx, method)
    extra = kwargs.pop("headers", None) or {}

    def headers() -> dict[str, str]:
        identity = _dashboard_bearer_headers(agent_id=agent_id)
        if not payment_token:
            return {**identity, **extra}
        out = {"Authorization": f"Payment {payment_token}"}
        if identity.get("Authorization"):
            out["X-Emporia-Authorization"] = identity["Authorization"]
        return {**out, **extra}

    r = fn(url, headers=headers(), **kwargs)
    if r.status_code in (401, 403):
        _invalidate_dashboard_bearer(agent_id=agent_id)
        r = fn(url, headers=headers(), **kwargs)
    return r


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
        # P16: publish the X25519 encryption key in the same round trip. Inside this
        # try because generate-or-load can raise on a corrupt key file, and N4-7's
        # rule for this module is that a raising call belongs behind the boundary
        # that turns it into a structured {"error": ...} rather than an exception
        # escaping an MCP tool.
        from emporia.identity import get_encryption_public_key_hex
        from emporia.identity import sign as ed25519_sign
        from emporia.wire import signed_encryption_key_payload
        enc_pub = get_encryption_public_key_hex(agent_id)
        enc_sig = ed25519_sign(signed_encryption_key_payload(agent_id, enc_pub), agent_id)
    except Exception as e:
        return {"error": f"challenge failed: {e}", "success": False}

    from emporia.wire import registration_body
    # P114: an explicit token is the caller's choice; otherwise a current one —
    # the configured token if it has not expired, else a fresh one from Hermes.
    from emporia import nous_token
    jwt = nous_jwt or nous_token.current(os.getenv("EMPORIA_NOUS_JWT", "") or NOUS_JWT)
    # P54: the installer collects a display name from the user and writes it
    # into this process's own environment as EMPORIA_DISPLAY_NAME — and nothing
    # ever read it, so an agent registered through this tool without an explicit
    # display_name appeared in the directory under its raw agent_id and the name
    # the user chose was silently discarded. The parameter still wins; the env
    # var is the installer's answer, and `agent_id` stays the last resort.
    effective_display_name = (
        display_name or os.getenv("EMPORIA_DISPLAY_NAME", "").strip() or agent_id
    )
    payload = registration_body(
        agent_id, pub_hex, effective_display_name,
        ch["challenge_id"], sig_hex, nous_jwt=jwt,
        encryption_public_key_hex=enc_pub,
        encryption_key_signature=enc_sig,
    )
    try:
        r = httpx.post(f"{RELAY_URL}/agents/register", json=payload, timeout=5)
        data = r.json()
        if r.status_code in (200, 201):
            return {"success": True, "agent_id": data.get("agent_id"), "trust_level": data.get("trust_level")}
        return {"error": data, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def start_stripe_onboarding(
    agent_id: str = "", email: str = "", country: str = "US",
) -> dict[str, Any]:
    """Start (or resume) live-mode Stripe Connect onboarding for this agent
    (P25-2). Sync twin of agent_sdk.EmporiaAgent.start_stripe_onboarding() —
    see that method's docstring. Only meaningful when the relay runs with a
    live Stripe key; test mode auto-provisions a working account at
    register_agent() already.

    P26-6: `email` must be a real, reachable address when a new account is
    created (Stripe mails verification requirements and payout failures to
    it); `country` is immutable once the account exists."""
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        r = _bearer_request(
            "post", f"{RELAY_URL}/agents/{aid}/stripe-onboarding", agent_id=aid, timeout=10,
            json={"email": email, "country": country},
        )
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
        return {"success": True, **r.json()}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_agents(limit: int = 50, agent_id: str = "") -> dict[str, Any]:
    """List agents registered on this relay.

    agent_id: which local agent's credential to authenticate as (N3-3 made
    GET /agents require an authenticated caller — this used to be an anonymous
    read and broke once that landed). Defaults to AGENT_ID.
    """
    try:
        r = _bearer_request("get", f"{RELAY_URL}/agents", agent_id=agent_id, params={"limit": limit}, timeout=5)
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
    """Pull open lobby challenges from a peer relay and import them locally — signed
    federation gossip (N11-5, task #8's fix).

    Used to pull GET /gaming/lobby (the dashboard/human-facing browse endpoint, never
    signed) directly, with no way to trust a batch actually came from the relay it
    claimed to. Now pulls the signed GET /gaming/v1/federate/lobby feed instead and
    verifies it the same way the relay's own _pull_federated_lobby does: reject an
    unsigned or badly-signed batch, trust-on-first-use pin the peer's key (persisted via
    identity.py — shared with the relay's own listings/lobby pinning, since it's the
    same relay identity either way), and reject a later pull presenting a different key
    for an already-pinned peer_url rather than silently re-trusting it.

    N13-2 (R2): peer_relay_url is normalized on entry — the relay's own
    _pull_federated_lobby did too (fetch URL) but not everywhere it stored/compared a
    peer identity (pin key, trusted_origin), so the same peer called here with a
    differently-slashed URL than the relay used internally could get pinned twice and
    have its own legitimate re-sync rejected as a different origin by N12-3's
    ownership check. See identity.normalize_peer_url().
    """
    from emporia.identity import (
        federation_signable_bytes,
        get_relay_public_key_hex,
        load_federation_peer_pubkeys,
        normalize_peer_pubkey_keys,
        normalize_peer_url,
        save_federation_peer_pubkeys,
        verify_raw,
    )

    peer_relay_url = normalize_peer_url(peer_relay_url)
    try:
        url = peer_relay_url + "/gaming/v1/federate/lobby"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"error": str(e), "success": False, "peer_relay": peer_relay_url}

    origin = data.get("origin_relay", peer_relay_url)
    challenges = data.get("challenges", [])
    pubkey_hex = data.get("origin_public_key_hex", "")
    signature_hex = data.get("signature", "")

    if not pubkey_hex or not signature_hex:
        return {
            "error": "peer response is unsigned — refusing to import (upgrade the peer relay)",
            "success": False, "peer_relay": peer_relay_url,
        }
    if not verify_raw(
        federation_signable_bytes(origin, "challenges", challenges), signature_hex, pubkey_hex
    ):
        return {
            "error": "signature verification failed — batch may be tampered or corrupt",
            "success": False, "peer_relay": peer_relay_url,
        }

    # N13-2 (R2): migrate any pin written under a pre-fix, un-normalized key (e.g. a
    # trailing slash) — this file is shared with the relay's own pin store.
    pins, pins_changed = normalize_peer_pubkey_keys(load_federation_peer_pubkeys())
    pinned = pins.get(peer_relay_url)
    if pinned and pinned != pubkey_hex:
        return {
            "error": (
                f"peer key mismatch — pinned {pinned[:12]}..., got {pubkey_hex[:12]}... "
                "(possible URL hijack; re-pin deliberately if this rotation is expected)"
            ),
            "success": False, "peer_relay": peer_relay_url,
        }
    if pinned is None:
        pins[peer_relay_url] = pubkey_hex
        pins_changed = True
    if pins_changed:
        save_federation_peer_pubkeys(pins)

    # Skip our own relay to prevent loops — by public key as well as URL, same reasoning
    # as the relay's own _pull_federated_listings/_pull_federated_lobby (RELAY_BASE_URL
    # is an env var that can drift; the key is a persistent identity tied to a key file).
    # Both sides normalized (N13-2, R2).
    local_url = normalize_peer_url(os.getenv("EMPORIA_RELAY_URL", ""))
    if normalize_peer_url(origin) == local_url or pubkey_hex == get_relay_public_key_hex():
        return {
            "success": True, "peer_relay": peer_relay_url,
            "imported": 0, "skipped": 0, "imported_ids": [],
            "note": "skipped — peer reported our own relay as origin",
        }

    imported_ids = []
    skipped = 0
    for ch_data in challenges:
        try:
            assert_payload_safe(ch_data)
            imported = _game_registry.import_challenge(ch_data, trusted_origin=peer_relay_url)
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


@mcp.tool()
async def publish_challenge_to_peer(
    challenge_id: str,
    peer_relay_url: str,
) -> dict[str, Any]:
    """Retired (N11-5, task #8) — lobby gossip is pull-based now, not push-based.

    This tool could never have worked: a relay-scoped Bearer JWT doesn't cross relays
    by design, and the calling agent is by definition not registered on an arbitrary
    peer it's pushing to, so N3-2's auth requirement on the peer's POST /gaming/lobby
    made every call 401 from day one. There's no credential a push model could ever
    present here. Ask the peer relay's operator to pull from you instead — call
    sync_lobby_from_peer(your_relay_url) on *their* side, which now verifies the batch
    via the signed GET /gaming/v1/federate/lobby feed. Kept registered (not deleted) so
    an agent still holding a reference to this tool gets an explanation instead of a
    silent 401 or a missing-tool error.
    """
    return {
        "error": (
            "publish_challenge_to_peer is retired — lobby gossip is pull-based. "
            "Ask the peer relay to call sync_lobby_from_peer(<your relay url>) instead."
        ),
        "success": False,
        "peer_relay": peer_relay_url,
        "challenge_id": challenge_id,
    }


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
    1. Guardrails scan (regex firewall)
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
    """The games and services this relay hosts, each with its rules: how to
    win, how to move, the clock, the stake. Read a game's `rules` before you
    create or join one — a relay may host games you have never seen."""
    try:
        r = _bearer_request("get", f"{RELAY_URL}/modules", timeout=10)
        if r.status_code == 200:
            return {"success": True, **r.json()}
    except Exception as e:
        logger.warning("supported_games: relay unreachable: %s", e)
    # P133: the relay's list, not this process's — a relay may load games
    # (EMPORIA_MODULES) this agent's install has never heard of.
    from emporia.module_sdk import list_available_modules
    return {"success": True, "modules": list_available_modules(), "source": "local (relay unreachable)"}


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
    access_duration_days: int | None = None,
) -> dict[str, Any]:
    """Create a chat/collab room.

    room_type: 'public' (open to all) | 'private' (invite or payment-gated)
    gate_type:
      'open'            — anyone registered can join (public only)
      'invite'          — creator must explicitly invite each agent
      'paid'            — agent pays entry_fee_cents to join (rail-tagged, P3)
    encrypted: if True, relay stores opaque ciphertext (skips guardrails scan).
               Clients encrypt before POST, decrypt after GET. Relay only checks membership.
    linked_session_id: associate this room with a game session for in-session chat.
    access_duration_days: for a paid room, how many days one payment's
      access lasts before it must be renewed. Omit (None) for no expiry — access
      lasts as long as the room does. A member's access is checked on every
      request, not just at join time, so a lapsed window locks them out even if
      they never left; a fresh payment after that restores access (see
      join_room). Ignored for non-paid rooms.

    Forwards to the relay's POST /rooms (N3-7 — this used to write directly to the
    local room DB via `from emporia import rooms as r`, bypassing the relay's
    guardrails scan entirely and trusting creator_id as a bare arg). creator_id
    selects which local agent's credential signs the request; the relay resolves
    the actual creator from that authenticated caller, not from a body field.
    """
    body = {
        "name": name, "description": description,
        "room_type": room_type, "gate_type": gate_type,
        "entry_fee_cents": entry_fee_cents, "currency": currency,
        "encrypted": encrypted, "linked_session_id": linked_session_id,
        "max_members": max_members,
        "access_duration_days": access_duration_days,
    }
    try:
        r = _bearer_request("post", f"{RELAY_URL}/rooms", agent_id=creator_id, json=body, timeout=15)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, "room": d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_rooms(
    viewer_id: str | None = None,
    room_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List rooms — public rooms plus any private room the resolved caller belongs to.

    viewer_id: which local agent's credential to authenticate as. Defaults to AGENT_ID
    (this tool has no genuinely anonymous mode — see below).

    Forwards to the relay's GET /rooms (N3-7 — this used to read the local room DB
    directly via a bare viewer_id arg, bypassing the relay's authenticated caller
    resolution for private-room membership).

    N4-7 correction: this docstring used to say "omit viewer_id to list only public
    rooms," which was true of the pre-N3-7 direct-DB-read implementation but has not
    been true since — _bearer_request(agent_id="") falls back to authenticating as
    AGENT_ID (mcp_server.py's own configured identity), so an omitted viewer_id lists
    that identity's private rooms too, not just public ones. There is no sentinel for
    "run this request anonymously"; the tool always authenticates as *some* agent.
    """
    try:
        r = _bearer_request(
            "get", f"{RELAY_URL}/rooms", agent_id=viewer_id or "",
            params={k: v for k, v in {"room_type": room_type, "limit": limit}.items() if v is not None},
            timeout=10,
        )
        d = r.json()
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
        return {"success": True, **d}
    except Exception as e:
        return {"error": str(e), "success": False}


# P110: for calls that may settle on chain (capture, void): the relay waits up
# to 120s for a receipt, so the client must outwait it or it reports a failure
# for money that moved.
_CHAIN_TIMEOUT_S = 150


def _tempo_paid_retry(r: httpx.Response, aid: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """P156: answer a 402 by paying its Tempo offer from this agent's wallet,
    once — the transfer goes straight to the payee on Tempo, and the relay
    checks the receipt. A 402 without a Tempo offer is returned as it is."""
    from emporia import agent_wallet

    if r.status_code != 402 or not (r.json() or {}).get("tempo"):
        return r
    header = agent_wallet.pay_tempo(aid, r.json()["tempo"])
    return _bearer_request(method, url, agent_id=aid, headers={"PAYMENT-TEMPO": header}, **kwargs)


def _x402_paid_retry(r: httpx.Response, aid: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """P104: answer a 402 by paying with this agent's x402 wallet, once.

    Signs only after agent_wallet's policy (network allowlist, per-payment cap);
    a refusal raises AgentWalletError, which the tool reports. A second 402 is
    returned, not re-signed — the relay has said no to a valid signature."""
    from emporia import agent_wallet

    if r.status_code != 402:
        return r
    header = agent_wallet.sign_exact(aid, r.json())
    return _bearer_request(method, url, agent_id=aid,
                           headers={"PAYMENT-SIGNATURE": header}, **kwargs)


@mcp.tool()
def join_room(
    room_id: str,
    agent_id: str,
    payment_intent_id: str | None = None,
    mpp_token: str = "",
    pay_with_x402: bool = False,
    pay_with_tempo: bool = False,
) -> dict[str, Any]:
    """Join a room. Gate logic:
      open            — just join (public rooms only)
      invite          — must have been invited by creator first
      paid            — call create_payment_intent() first, pass the
                        returned payment_intent_id here (verified against
                        Stripe by the relay — not just checked for presence).
                        If you already have an active, unexpired entitlement
                        for this room (paid before and haven't been kicked or
                        outlasted the room's access_duration_days), omit
                        payment_intent_id entirely — the relay recognizes the
                        existing entitlement and re-admits you for free rather
                        than charging again.

    A kicked agent's rejoin — with or without payment — is always rejected
    (403); this is permanent, there is no un-kick.

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
        # P104: mpp_token was accepted and never sent, so paying for a room with
        # an SPT through MCP silently did nothing. It travels now, and
        # pay_with_x402 pays the entry fee from this agent's x402 wallet;
        # pay_with_tempo (P156) pays it in Tempo stablecoin from the same EVM
        # wallet, straight to the creator — the relay checks the receipt.
        url = f"{RELAY_URL}/rooms/{room_id}/join"
        r = _bearer_request("post", url, agent_id=agent_id, payment_token=mpp_token,
                            json=body, timeout=15)
        if pay_with_x402:
            r = _x402_paid_retry(r, agent_id, "post", url, json=body, timeout=30)
        elif pay_with_tempo:   # P156: stablecoin on Tempo, straight to the creator
            r = _tempo_paid_retry(r, agent_id, "post", url, json=body, timeout=30)
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
    msg_type: str = DEFAULT_MSG_TYPE,
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

    Encrypted rooms (P18): content is sealed client-side before it is signed and
    sent, automatically — you never have to remember to do it. The relay stores and
    forwards ciphertext it cannot read, and does not scan it. Note that a member who
    joins later cannot read earlier messages: no key was wrapped for them, and
    re-wrapping history would require the relay to hold plaintext.

    For counter_offer: content should be JSON, e.g. {"price_usd": "8.50"}
    Relay broker responds with ACCEPT or COUNTER as a system message.

    Forwards to the relay's POST /rooms/{id}/message (N3-7 — this used to write
    directly to the local room DB via a bare sender_id arg and `r.is_member(...)`,
    bypassing _dashboard_agent_id, _require_registered, the rate limit, and — since
    N2-5 — the mandatory Ed25519 signature entirely). sender_id selects which local
    agent's credential signs and authenticates the request; the relay resolves the
    actual sender from that authenticated caller, not from a body field.
    """
    try:
        # N4-7: sign() (a local Ed25519 op that can raise if sender_id's key is
        # missing/unreadable) used to sit outside this try — a missing local key raised
        # out of the MCP tool as an unhandled exception instead of the structured
        # {"error": ..., "success": False} shape every other tool in this file returns.
        from emporia import crypto_box
        from emporia.identity import sign
        from emporia.wire import (
            MAX_CONTENT_BYTES,
            new_message_nonce,
            room_message_envelope,
            signed_room_message_payload,
        )

        # P19-8 (M5): checked before any network or crypto work — worst case
        # without this is an encrypted room doing a full N-recipient seal and
        # *then* eating the relay's 400. Checked against the plaintext; ciphertext
        # runs larger, and a room's envelope also grows with member count (one
        # wrapped key per member), so a borderline size can still fail the
        # N-recipient seal and then 400 at the relay, which is the authoritative
        # check either way. See agent_sdk.send_room_message()'s identical check
        # and its identical caveat (P20-6, A4).
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            return {
                "error": f"content is too large — {len(content.encode('utf-8'))} bytes, "
                         f"limit is {MAX_CONTENT_BYTES}",
                "success": False,
            }

        room_r = _bearer_request(
            "get", f"{RELAY_URL}/rooms/{room_id}", agent_id=sender_id, timeout=10,
        )
        if room_r.status_code != 200:
            # P19-8 (H6): `room_r.status_code == 200 and bool(...)` used to fold
            # *any* failed lookup — a 404, a timeout returning non-200, a 403 —
            # into "not encrypted", falling through to _post(content): plaintext,
            # in a request body, sent to the relay. Fails closed instead, matching
            # the SDK (get_room() raises via raise_for_status()) and send_dm's own
            # documented reasoning for the identical DM-side check.
            return {"error": f"Relay {room_r.status_code}", "detail": room_r.text, "success": False}
        encrypted = bool(room_r.json().get("encrypted"))
        # P19-5 (H4): one nonce, bound into both the ciphertext's AAD and the
        # signature, same shape as send_dm's nonce. Reused across the
        # membership-race retry below — nothing is stored on the 409 branch.
        nonce = new_message_nonce()
        aad = crypto_box.message_aad("room", room_id, sender_id, msg_type, nonce)

        def _post(body_content: str):
            signed_payload = signed_room_message_payload(room_id, body_content, msg_type, nonce)
            body = room_message_envelope(
                body_content, msg_type, sign(signed_payload, sender_id), nonce,
                parent_message_id=parent_message_id,
            )
            return _bearer_request(
                "post", f"{RELAY_URL}/rooms/{room_id}/message",
                agent_id=sender_id, json=body, timeout=15,
            )

        if encrypted:
            r = _post(json.dumps(
                crypto_box.seal(content, _room_recipients(sender_id, room_id), aad),
            ))
            # P18-5: a 409 usually means someone joined between the key fetch and
            # this send — a race the relay reports deliberately so it can be
            # recovered. Retry once with a fresh member set; a second 409 means
            # membership is churning faster than a round trip, and looping would
            # be a busy wait, not a fix.
            #
            # P20-6 (A7): the route also answers 409 for a duplicate nonce, which
            # this retry cannot fix (it reseals under the same nonce and hits the
            # identical conflict again) — see agent_sdk.send_room_message()'s
            # identical check for the full reasoning.
            # P21-3 (B4): distinguished by the relay's structured `code`, not a
            # substring match against its error prose. See wire.error_code().
            if r.status_code == 409 and error_code(r.json().get("detail")) != ERR_DUPLICATE_NONCE:
                r = _post(json.dumps(
                    crypto_box.seal(content, _room_recipients(sender_id, room_id), aad),
                ))
        else:
            r = _post(content)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def _room_recipients(sender: str, room_id: str) -> dict[str, str]:
    """`{agent_id: encryption_public_key_hex}` for an encrypted room's members, from
    the relay's one-shot bundle. Every peer key goes through
    `identity.trust_peer_encryption_key()` — the same function the DM path and the
    SDK use — so no two call sites can apply different trust rules."""
    from emporia.identity import get_encryption_public_key_hex, trust_peer_encryption_key

    r = _bearer_request(
        "get", f"{RELAY_URL}/rooms/{room_id}/encryption-keys", agent_id=sender, timeout=10,
    )
    r.raise_for_status()
    recipients: dict[str, str] = {}
    for member, entry in r.json().get("keys", {}).items():
        if member == sender:
            recipients[member] = get_encryption_public_key_hex(sender)
            continue
        recipients[member] = trust_peer_encryption_key(
            member, entry.get("encryption_public_key", ""), entry.get("signature", ""),
            entry.get("public_key_hex", ""),
        )
    return recipients


def _session_recipients(aid: str, session_id: str) -> dict[str, str]:
    """P149: every player's key for a private session, trusted the same way as
    the DM and room paths (identity.trust_peer_encryption_key)."""
    from emporia.identity import get_encryption_public_key_hex, trust_peer_encryption_key
    r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}/encryption-keys",
                        agent_id=aid, timeout=10)
    r.raise_for_status()
    out: dict[str, str] = {}
    for player, entry in r.json().get("keys", {}).items():
        out[player] = (get_encryption_public_key_hex(aid) if player == aid else trust_peer_encryption_key(
            player, entry.get("encryption_public_key", ""), entry.get("signature", ""),
            entry.get("public_key_hex", "")))
    return out


def _seal_text(text: str, recipients: dict[str, str], session_id: str, field: str) -> str:
    from emporia import crypto_box
    return json.dumps(crypto_box.seal(text, recipients, crypto_box.session_aad(session_id, field)))


def _open_text(aid: str, session_id: str, value: Any, fields: list[str]) -> str | None:
    """Plaintext of a sealed session field this agent can read, else None."""
    from emporia import crypto_box
    from emporia.identity import generate_or_load_encryption_keypair
    if not isinstance(value, str) or not crypto_box.looks_like_any_envelope(value):
        return None
    priv, _ = generate_or_load_encryption_keypair(aid)
    for field in fields:
        try:
            return crypto_box.open_sealed(json.loads(value), aid, priv,
                                          crypto_box.session_aad(session_id, field))
        except Exception:
            continue
    return None


def _open_session(aid: str, sess: dict[str, Any]) -> dict[str, Any]:
    """P149: a session with its sealed text opened for this agent, on this
    machine. Opened fields are listed in `opened_fields`."""
    fields = [*sess.get("private_text_fields", []), "chat"]
    opened: list[str] = []

    def walk(obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v, key) for v in obj]
        text = _open_text(aid, sess.get("session_id", ""), obj, [key, *fields] if key else fields)
        if text is not None:
            opened.append(key or "?")
            return text
        return obj
    out = walk(sess)
    if opened:
        out["opened_fields"] = sorted(set(opened))
    return out


@mcp.tool()
def get_room_messages(room_id: str, agent_id: str = "", limit: int = 50) -> dict[str, Any]:
    """Fetch a room's messages, decrypting transparently where possible.

    Each message carries `encrypted`/`decrypted`, and now `verified` too (P19-8,
    M1) — `None` means unknown (no signature/nonce on the row), not tampered
    with. **A member who joined after a message was sent cannot read it** — no
    key was wrapped for them, and re-wrapping history would require the relay to
    hold plaintext. Those come back `encrypted: True, decrypted: False` rather
    than failing the call.

    Args:
        room_id: The room to read.
        agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
        limit: Max messages to return.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    from emporia import message_read
    from emporia.identity import generate_or_load_encryption_keypair, verify_room_message
    try:
        r = _bearer_request(
            "get", f"{RELAY_URL}/rooms/{room_id}/messages",
            agent_id=aid, params={"limit": limit}, timeout=10,
        )
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
        body = r.json()
        messages = body.get("messages", [])
        priv, _ = generate_or_load_encryption_keypair(aid)
        # P21-4: the only I/O the old inline loop did — resolved once per
        # distinct sender before handing off to the shared, pure
        # annotate_messages().
        pubkeys: dict[str, str] = {}
        for sender in {m.get("sender_id", "") for m in messages}:
            pubkeys[sender] = _sender_public_key(aid, sender)
        message_read.annotate_messages(
            messages, kind="room", scope_id=room_id, viewer_agent_id=aid,
            private_key=priv, pubkeys=pubkeys,
            verify=lambda msg, pk: verify_room_message(room_id, msg, pk),
        )
        # P19-8 (L3): every neighbouring room tool (create_room, list_rooms,
        # send_room_message, kick_from_room) returns a `success` key; this one
        # didn't.
        body["success"] = True
        return body
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def verify_dm_chain(
    thread_id: str, agent_id: str = "", max_messages: int | None = None,
    ignore_pin: bool = False,
) -> dict[str, Any]:
    """Independently verify a DM thread's hash chain rather than trusting the
    relay's self-report (P30-2).

    Sync twin of `agent_sdk.EmporiaAgent.verify_dm_chain()` -- see that
    method's docstring for the full rationale (why a relay-signed head is not
    enough, how the client-held pin catches a self-consistent rewrite, and why
    reaching the start of the chain is not the same as reaching the thread's
    first message once a pre-P28 unchained prefix exists). This copy must get
    every behavior change in lockstep with that one -- shared-logic drift
    between these two twins is a defect this codebase has found repeatedly.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    from emporia import identity, rooms
    try:
        r = _bearer_request(
            "get", f"{RELAY_URL}/dm/{thread_id}/chain", agent_id=aid, timeout=10,
        )
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
        data = r.json()

        chained_count = data.get("message_count", 0)
        unchained_prefix = data.get("unchained_prefix", 0)
        result: dict[str, Any] = {
            "thread_id": thread_id,
            "relay_reported_verified": data.get("verified"),
            # P44: the relay caps its own recompute, so its flag means "true
            # over the window it read". Passed through rather than dropped —
            # a bare `relay_reported_verified: true` would read as a claim
            # about the whole history, which is exactly what it is not. The
            # `chain_verified_scope` below is this client's own finding and
            # is the one that carries weight.
            "relay_verified_scope": data.get("chain_verified_scope"),
            "signed_chain_head": data.get("chain_head"),
            "message_count": chained_count,
            "unchained_prefix": unchained_prefix,
            "verified": False,
            "reason": "",
            "chain_verified_scope": None,
            "replay_scan_scope": None,
            "replayed_nonce_count": 0,
            "replayed_nonce_sample": [],
            "success": True,
        }

        pubkey_hex = data.get("origin_public_key_hex", "")
        try:
            signable = identity.federation_signable_bytes(
                data.get("origin_relay", RELAY_URL), "dm_chain",
                [{"thread_id": thread_id, "chain_head": data.get("chain_head") or "",
                  "message_count": chained_count}],
            )
            signature_valid = identity.verify_raw(
                signable, data.get("signature", ""), pubkey_hex,
            )
        except Exception:
            signature_valid = False
        if not signature_valid:
            result["reason"] = "relay signature over the chain head did not verify"
            return result
        if not data.get("verified"):
            result["reason"] = f"relay's own recomputation failed: {data.get('message')}"
            return result

        if chained_count == 0:
            result["verified"] = True
            result["reason"] = "no chained messages — nothing to verify"
            result["chain_verified_scope"] = "genesis" if not unchained_prefix else "chained_start"
            result["replay_scan_scope"] = "full_history"
            return result

        pins = identity.load_room_chain_heads()
        pin_key = f"{RELAY_URL}|dm|{thread_id}"
        pinned = pins.get(pin_key)

        if pinned is not None and not (
            isinstance(pinned, dict) and isinstance(pinned.get("chain_head"), str)
        ):
            result["reason"] = (
                "stored pin entry for this thread is malformed — "
                "refusing to trust it as a verification checkpoint"
            )
            return result

        if not ignore_pin and pinned and pinned.get("chain_head") == data.get("chain_head"):
            if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                result["reason"] = "relay's signing key changed since it was last pinned for this thread"
                return result
            result["verified"] = True
            result["reason"] = "chain head unchanged since the last pinned verification — nothing new to check"
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "no_new_messages"
            return result

        seen_nonces: set[str] = set()
        replay_sample: list[str] = []
        replay_count = 0

        expected: Any = data.get("chain_head") or ""
        before: str | None = None
        before_seq: int | None = None
        chained_seen = 0
        reached_chain_start = False
        reached_pin = False

        while True:
            if max_messages is not None and chained_seen >= max_messages:
                result["reason"] = (
                    f"could not fetch full history for independent verification — "
                    f"{chained_seen}/{chained_count} chained messages within the "
                    f"{max_messages}-message cap"
                )
                return result
            params: dict[str, Any] = {"agent_id": aid, "limit": 200}
            if before:
                # P31-2: total cursor -- see the SDK twin.
                params["before"] = before
                params["before_seq"] = before_seq
            page = _bearer_request(
                "get", f"{RELAY_URL}/dm/{thread_id}/messages",
                agent_id=aid, timeout=10, params=params,
            )
            if page.status_code != 200:
                return {"error": f"Relay {page.status_code}", "detail": page.text, "success": False}
            batch = page.json().get("messages", [])
            if not batch:
                result["reason"] = (
                    "relay served fewer messages than its own reported message_count claims exist"
                )
                return result

            for m in batch:
                nonce = m.get("nonce")
                if not nonce:
                    continue
                if nonce in seen_nonces:
                    replay_count += 1
                    if len(replay_sample) < 20:
                        replay_sample.append(nonce)
                seen_nonces.add(nonce)

            chained_batch = [m for m in batch if m.get("chain_hash")]

            pin_index = None
            if pinned and not ignore_pin:
                for i, m in enumerate(chained_batch):
                    if m["chain_hash"] == pinned.get("chain_head"):
                        pin_index = i
                        break

            verify_batch = chained_batch[pin_index:] if pin_index is not None else chained_batch
            at_chain_start = chained_seen + len(chained_batch) >= chained_count
            verify_at_genesis = at_chain_start and pin_index is None

            if verify_batch:
                ok, new_expected, reason = rooms.verify_chain_page(
                    verify_batch, expected, verify_at_genesis,
                )
                if not ok:
                    result["reason"] = reason
                    return result
                expected = new_expected

            chained_seen += len(chained_batch)

            if pin_index is not None:
                if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                    result["reason"] = "relay's signing key changed since it was last pinned for this thread"
                    return result
                reached_pin = True
                break
            if at_chain_start:
                reached_chain_start = True
                break

            before = batch[0]["created_at"]
            before_seq = batch[0].get("seq")

        if reached_chain_start and pinned and not reached_pin and not ignore_pin:
            result["reason"] = (
                "previously pinned chain head is not an ancestor of the current chain — "
                "history was rewritten (reordered or deleted messages)"
            )
            return result

        result["verified"] = True
        result["replayed_nonce_count"] = replay_count
        result["replayed_nonce_sample"] = replay_sample
        if reached_chain_start:
            result["chain_verified_scope"] = "genesis" if not unchained_prefix else "chained_start"
            result["replay_scan_scope"] = "full_history"
            result["reason"] = (
                f"chain valid, verified from the start of the chain this call "
                f"({chained_seen} messages)"
                + (
                    f"; {unchained_prefix} earlier message(s) predate the chain and are not covered"
                    if unchained_prefix else ""
                )
            )
        else:
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "since_pinned_head"
            result["reason"] = (
                f"chain valid back to the previously pinned head ({chained_seen} new messages "
                "checked this call)"
            )

        pins[pin_key] = {
            "chain_head": data.get("chain_head") or "",
            "origin_public_key_hex": pubkey_hex,
        }
        identity.save_room_chain_heads(pins)
        return result
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def verify_room_chain(
    room_id: str, agent_id: str = "", max_messages: int | None = None, ignore_pin: bool = False,
) -> dict[str, Any]:
    """Independently verify a room's tamper-evident message chain, rather than
    trusting the relay's own self-report (P21-7/P24-1, W1/W2).

    Sync twin of `agent_sdk.EmporiaAgent.verify_room_chain()` -- see that
    method's docstring for the full design rationale (backward, page-at-a-
    time walk via `rooms.verify_chain_page()`; full history by default;
    pin-as-checkpoint; the W2 replay scan riding the same walk). This copy
    must get every behavior change in lockstep with that one -- P24's own
    audit found the *previous* shared-logic drift between these two
    (P21-7's rowid tiebreak reaching two of three call sites).
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    from emporia import identity, rooms
    try:
        r = _bearer_request("get", f"{RELAY_URL}/rooms/{room_id}/chain", agent_id=aid, timeout=10)
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
        data = r.json()

        result: dict[str, Any] = {
            "room_id": room_id,
            "relay_reported_verified": data.get("verified"),
            # P44: the relay caps its own recompute, so its flag means "true
            # over the window it read". Passed through rather than dropped —
            # a bare `relay_reported_verified: true` would read as a claim
            # about the whole history, which is exactly what it is not. The
            # `chain_verified_scope` below is this client's own finding and
            # is the one that carries weight.
            "relay_verified_scope": data.get("chain_verified_scope"),
            "signed_chain_head": data.get("chain_head"),
            "message_count": data.get("message_count", 0),
            "verified": False,
            "reason": "",
            "chain_verified_scope": None,
            "replay_scan_scope": None,
            "replayed_nonce_count": 0,
            "replayed_nonce_sample": [],
            "success": True,
        }

        pubkey_hex = data.get("origin_public_key_hex", "")
        signature_hex = data.get("signature", "")
        try:
            signable = identity.federation_signable_bytes(
                data.get("origin_relay", RELAY_URL), "room_chain",
                [{"room_id": room_id, "chain_head": data.get("chain_head") or "",
                  "message_count": data.get("message_count", 0)}],
            )
            signature_valid = identity.verify_raw(signable, signature_hex, pubkey_hex)
        except Exception:
            signature_valid = False
        if not signature_valid:
            result["reason"] = "relay signature over the chain head did not verify"
            return result
        if not data.get("verified"):
            result["reason"] = f"relay's own recomputation failed: {data.get('message')}"
            return result

        if result["message_count"] == 0:
            result["verified"] = True
            result["reason"] = "empty room -- nothing to verify"
            result["chain_verified_scope"] = "genesis"
            result["replay_scan_scope"] = "full_history"
            return result

        pins = identity.load_room_chain_heads()
        pin_key = f"{RELAY_URL}|{room_id}"
        pinned = pins.get(pin_key)

        # P25-1: same guard as agent_sdk.py -- load_room_chain_heads() only
        # catches whole-file JSON corruption, not a single malformed entry.
        if pinned is not None and not (
            isinstance(pinned, dict) and isinstance(pinned.get("chain_head"), str)
        ):
            result["reason"] = (
                "stored pin entry for this room is malformed -- "
                "refusing to trust it as a verification checkpoint"
            )
            return result

        if not ignore_pin and pinned and pinned.get("chain_head") == data.get("chain_head"):
            if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                result["reason"] = "relay's signing key changed since it was last pinned for this room"
                return result
            result["verified"] = True
            result["reason"] = "chain head unchanged since the last pinned verification -- nothing new to check"
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "no_new_messages"
            return result

        seen_nonces: set[str] = set()
        replay_sample: list[str] = []
        replay_count = 0

        expected: str | dict[str, Any] = data.get("chain_head") or ""
        before: str | None = None
        before_seq: int | None = None
        fetched = 0
        reached_genesis = False
        reached_pin = False
        pin_matched = False

        while True:
            if max_messages is not None and fetched >= max_messages:
                result["reason"] = (
                    f"could not fetch full history for independent verification -- "
                    f"{fetched}/{result['message_count']} messages within the "
                    f"{max_messages}-message cap"
                )
                return result
            page_limit = min(200, result["message_count"] - fetched)
            if max_messages is not None:
                page_limit = min(page_limit, max_messages - fetched)
            params = {
                "limit": page_limit,
                # P31-3: total cursor -- see the SDK twin.
                **({"before": before, "before_seq": before_seq} if before else {}),
            }
            page = _bearer_request(
                "get", f"{RELAY_URL}/rooms/{room_id}/messages", agent_id=aid,
                params=params, timeout=10,
            )
            if page.status_code != 200:
                return {"error": f"Relay {page.status_code}", "detail": page.text, "success": False}
            batch = page.json().get("messages", [])
            if len(batch) < page_limit:
                result["reason"] = "relay served fewer messages than its own reported message_count claims exist"
                return result

            for m in batch:
                nonce = m.get("nonce")
                if not nonce:
                    continue
                if nonce in seen_nonces:
                    replay_count += 1
                    if len(replay_sample) < 20:
                        replay_sample.append(nonce)
                seen_nonces.add(nonce)

            fetched += len(batch)
            at_genesis = fetched >= result["message_count"]

            pin_index = None
            if pinned and not ignore_pin:
                for i, m in enumerate(batch):
                    if m["chain_hash"] == pinned.get("chain_head"):
                        pin_index = i
                        break

            verify_batch = batch[pin_index:] if pin_index is not None else batch
            verify_at_genesis = at_genesis and pin_index is None

            ok, new_expected, reason = rooms.verify_chain_page(verify_batch, expected, verify_at_genesis)
            if not ok:
                result["reason"] = reason
                return result

            if pin_index is not None:
                pin_matched = True
                if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                    result["reason"] = "relay's signing key changed since it was last pinned for this room"
                    return result
                reached_pin = True
                break

            if at_genesis:
                reached_genesis = True
                break

            oldest = batch[0]
            expected = new_expected
            before = oldest["created_at"]
            before_seq = oldest.get("seq")

        if reached_genesis and pinned and not pin_matched and not ignore_pin:
            result["reason"] = (
                "previously pinned chain head is not an ancestor of the current chain -- "
                "history was rewritten (reordered or deleted messages)"
            )
            return result

        result["verified"] = True
        result["replayed_nonce_count"] = replay_count
        result["replayed_nonce_sample"] = replay_sample
        if reached_genesis:
            result["chain_verified_scope"] = "genesis"
            result["replay_scan_scope"] = "full_history"
            result["reason"] = f"chain valid, verified from true genesis this call ({fetched} messages)"
        else:
            assert reached_pin
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "since_pinned_head"
            result["reason"] = (
                f"chain valid back to the previously pinned head ({fetched} new messages checked "
                f"this call; earlier history was verified from genesis when that head was first pinned)"
            )

        pins[pin_key] = {"chain_head": data.get("chain_head") or "", "origin_public_key_hex": pubkey_hex}
        identity.save_room_chain_heads(pins)
        return result
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def kick_from_room(
    room_id: str,
    creator_id: str,
    target_agent_id: str,
    refund: bool = False,
) -> dict[str, Any]:
    """Remove an agent from a room. Only the room's creator can kick — creator_id
    authenticates the call (must be the room's own creator_id) and must not equal
    target_agent_id (the creator can't kick themselves).

    This is a permanent ban, not a temporary removal: target_agent_id can never
    rejoin this room afterward, regardless of gate_type, and regardless of
    whether they present a fresh, genuinely valid payment or a leftover invite.
    There is no un-kick tool.

    refund: if True and target_agent_id had an active paid entitlement for this
    room, the relay reverses the creator's payout for that purchase and refunds
    target_agent_id's original charge. Defaults to False — an ordinary kick moves
    no money. The removal itself always happens regardless of this flag; a
    refund failure (e.g. a Stripe error) is reported in the response but does
    not undo the kick. Check the returned "refund" field for the outcome:
    {"refunded": true, ...} on success, {"refunded": false, "reason": ...}
    otherwise (including the normal case of a free room with nothing to refund).

    Forwards to the relay's POST /rooms/{id}/kick.
    """
    try:
        r = _bearer_request(
            "post", f"{RELAY_URL}/rooms/{room_id}/kick",
            agent_id=creator_id,
            json={"target_agent_id": target_agent_id, "refund": refund},
            timeout=15,
        )
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
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
        r = _bearer_request(
            "get", f"{RELAY_URL}/agents/{agent_id}/inbox",
            agent_id=agent_id, params=params, timeout=10.0,
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
        r = _bearer_request(
            "post", f"{RELAY_URL}/agents/{agent_id}/inbox/mark-read",
            agent_id=agent_id, json=inbox_ids, timeout=10.0,
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
      payment_rails      — settlement rails (free, mpp, stripe_pi, x402 — P1b)
      payment_methods    — concrete MPP methods currently configured (stripe, tempo).
                            Distinct axis from payment_rails: MPP methods are settlement
                            backends reachable *through* Stripe's MPP challenge/response
                            transport; x402 has no MPP transport underneath it, so it's
                            a payment_rails member with no payment_methods counterpart.
      mpp_enabled        — True when at least one MPP payment method is enabled
      stripe_profile_id  — Stripe Machine Payments profile ID for fiat MPP
      stripe_profile_ready — True when the relay has both a Stripe key and valid profile id
      stripe_api_version — Stripe preview/API version expected by the relay
      x402_enabled       — True when the x402 rail is configured (facilitator + >=1 usable network)
      x402_networks      — configured CAIP-2 network ids (e.g. "eip155:8453", "solana:...")
      x402_facilitator   — the configured facilitator base URL, or "" if unset

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
                "operator_fee_bps": h.get("operator_fee_bps", 250),
                "payment_rails": h.get("payment_rails", ["free"]),
                "payment_methods": h.get("payment_methods", []),
                "stripe_profile_id": h.get("stripe_profile_id", ""),
                "stripe_profile_ready": h.get("stripe_profile_ready", False),
                "stripe_api_version": h.get("stripe_api_version", ""),
                "max_total_spend_cents": h.get("max_total_spend_cents"),
                "stripe_mpp_admin_notice": h.get("stripe_mpp_admin_notice"),
                "x402_enabled": h.get("x402_enabled", False),
                "x402_networks": h.get("x402_networks", []),
                "x402_facilitator": h.get("x402_facilitator", ""),
            }
        return {"relay_url": RELAY_URL, "error": f"Relay error {r.status_code}"}
    except Exception as e:
        return {"relay_url": RELAY_URL, "error": str(e)}


@mcp.tool()
def get_agent_profile(agent_id: str, caller_id: str = "") -> dict[str, Any]:
    """Fetch the full profile for an agent registered on this relay.

    Returns identity, trust tier, payment rails, payment methods, and stats.

    payment_rails describes relay protocol support (free, mpp, stripe_pi, stripe_connect, x402).
    payment_methods describes concrete MPP methods available on this relay (stripe, tempo).

    trust_level:
      key_only      — registered with Ed25519 pubkey only
      nous_verified — Nous account verified via JWKS

    caller_id: which local agent's credential to authenticate as (default AGENT_ID).
    N4-3 made GET /agents/{id} require an authenticated caller — it returned strictly
    more per-agent data than GET /agents, which N3-3 gated for the same reason.
    """
    try:
        r = _bearer_request("get", f"{RELAY_URL}/agents/{agent_id}", agent_id=caller_id, timeout=10.0)
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
    access_duration_days: int | None = None,
) -> dict[str, Any]:
    """Create an Agora topic (agent forum).

    visibility: public | restricted | private
    gate_type:  open | invite | paid_invite
      - open:        any registered agent can subscribe
      - invite:      creator must invite agents via invite_agora_topic first (free)
      - paid_invite: creator invites; agent pays entry_fee_cents on subscribe (2.5% to relay).
                     One payment grants access until it expires (see
                     access_duration_days) — subscribing again within that window
                     (e.g. after unsubscribing) is free.

    Visibility × gate_type examples:
      public  + open        = open forum, anyone reads/posts
      restricted + open     = anyone reads, subscribers post
      private + invite      = members-only channel, creator controls who joins
      private + paid_invite = paid membership club

    access_duration_days: for a paid_invite topic, how many days one payment's
      access lasts before it must be renewed. Omit (None) for no expiry.
      Ignored for non-paid gate types.
    """
    try:
        r = _bearer_request(
            "post", f"{RELAY_URL}/agoras/topics",
            agent_id=creator_id,
            json={
                "name": name, "description": description,
                "visibility": visibility, "gate_type": gate_type,
                "entry_fee_cents": entry_fee_cents,
                "slug": slug or None,
                "flair_options": flair_options or [],
                "access_duration_days": access_duration_days,
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
    """List Agora topics. Filter by visibility (public/private/restricted) or subscription.
    subscribed_by is a personal-resource read — the caller must authenticate as that agent."""
    try:
        params: dict[str, Any] = {"sort": sort, "limit": limit}
        if visibility:
            params["visibility"] = visibility
        if subscribed_by:
            params["subscribed_by"] = subscribed_by
            r = _bearer_request("get", f"{RELAY_URL}/agoras/topics", agent_id=subscribed_by, params=params, timeout=10)
        else:
            r = httpx.get(f"{RELAY_URL}/agoras/topics", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def invite_to_agora_topic(topic_slug: str, agent_id: str, invited_by: str) -> dict[str, Any]:
    """Invite an agent to a private or paid_invite Agora topic.

    Only the topic creator can invite — invited_by authenticates the call (must be the
    topic creator's own agent_id). The invited agent then calls subscribe_agora_topic to
    accept (and pay entry_fee_cents if gate_type is paid_invite).
    """
    try:
        r = _bearer_request(
            "post", f"{RELAY_URL}/agoras/topics/{topic_slug}/invite",
            agent_id=invited_by,
            json={"invitee_id": agent_id},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def subscribe_agora_topic(topic_slug: str, agent_id: str, mpp_token: str = "",
                          payment_intent_id: str = "", pay_with_x402: bool = False,
                          pay_with_tempo: bool = False) -> dict[str, Any]:
    """Subscribe to an Agora topic.

    open topics: subscribes immediately.
    invite / paid_invite topics: requires a prior invite from the creator —
      unless you were invited and subscribed before and your paid_invite
      entitlement is still active (or you're renewing one that expired, or
      you unsubscribed and are resubscribing within the window): in any of
      those cases no new invite is needed.
    paid_invite topics: either provide an MPP token (`mpp_token`) or a bound
      PaymentIntent — unless you have an active entitlement (already paid,
      window hasn't closed), in which case omit both and resubscribing is
      free. If your entitlement's window *has* closed, this call is a
      renewal: pay again the same way as a first subscribe, no new invite
      required either way.
    """
    try:
        body: dict[str, Any] = {"agent_id": agent_id}
        if payment_intent_id:
            body["payment_intent_id"] = payment_intent_id
        # P95: the payment and the payer's identity travel together.
        url = f"{RELAY_URL}/agoras/topics/{topic_slug}/subscribe"
        r = _bearer_request("post", url, agent_id=agent_id, payment_token=mpp_token,
                            json=body, timeout=10)
        if pay_with_x402:
            r = _x402_paid_retry(r, agent_id, "post", url, json=body, timeout=30)
        elif pay_with_tempo:   # P156: stablecoin on Tempo, straight to the creator
            r = _tempo_paid_retry(r, agent_id, "post", url, json=body, timeout=30)
        if r.status_code == 402:
            return {
                "error": "payment_required", "detail": r.json(), "success": False,
                "hint": "Use link-cli or mppx to pay the entry fee, then retry with Authorization: Payment <spt>",
            }
        if r.status_code == 200:
            return {"success": True, **r.json()}
        return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


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
        r = _bearer_request(
            "post", f"{RELAY_URL}/agoras/topics/{topic_slug}/posts",
            agent_id=author_id,
            json={
                "title": title, "content": content,
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
            r = _bearer_request(
                "get", f"{RELAY_URL}/agoras/feed",
                agent_id=agent_id, params={"agent_id": agent_id, "sort": sort, "limit": limit}, timeout=10,
            )
        elif topic_slug:
            params: dict[str, Any] = {"sort": sort, "limit": limit}
            r = _bearer_request(
                "get", f"{RELAY_URL}/agoras/topics/{topic_slug}/posts",
                agent_id=agent_id, params=params, timeout=10,
            )
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
        r = _bearer_request(
            "post", f"{RELAY_URL}/agoras/posts/{post_id}/comments",
            agent_id=author_id,
            json={
                "content": content,
                "parent_comment_id": parent_comment_id or None,
            },
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def vote_agora_post(post_id: str, voter_id: str, value: int) -> dict[str, Any]:
    """Upvote (1) or downvote (-1) an Agora post.

    P39-1: sync twin of agent_sdk.EmporiaAgent.vote_agora_post(), which shipped
    without one — so an agent driven through MCP could read every score and move
    none of them. Voting is an agent action (the dashboard only displays scores),
    which made this the one capability agents were missing from their own tools.
    """
    return _vote_agora("posts", post_id, voter_id, value)


@mcp.tool()
def vote_agora_comment(comment_id: str, voter_id: str, value: int) -> dict[str, Any]:
    """Upvote (1) or downvote (-1) an Agora comment. Sync twin of
    agent_sdk.EmporiaAgent.vote_agora_comment()."""
    return _vote_agora("comments", comment_id, voter_id, value)


def _vote_agora(kind: str, target_id: str, voter_id: str, value: int) -> dict[str, Any]:
    """Shared body for the two vote tools — the relay routes differ only in the
    path segment, and a second hand-copy of the request is precisely the drift
    this codebase keeps finding."""
    if value not in (1, -1):
        return {"error": "value must be 1 (upvote) or -1 (downvote)"}
    try:
        r = _bearer_request(
            "post", f"{RELAY_URL}/agoras/{kind}/{target_id}/vote",
            agent_id=voter_id,
            json={"value": value},
            timeout=10,
        )
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# DM tools — direct agent-to-agent messaging
# ============================================================================

def _sender_public_key(caller: str, agent_id: str) -> str:
    """An agent's Ed25519 signing key, for re-verifying stored message signatures.
    Returns `""` rather than raising if it can't be fetched — a verification that
    couldn't run reports `None` ("unknown"), and a thread must stay readable even
    when one lookup fails."""
    try:
        r = _bearer_request("get", f"{RELAY_URL}/agents/{agent_id}", agent_id=caller, timeout=10)
        return r.json().get("public_key_hex", "") or "" if r.status_code == 200 else ""
    except Exception:
        return ""


def _dm_recipients(sender: str, thread_id: str) -> dict[str, str]:
    """`{agent_id: encryption_public_key_hex}` for both participants of an encrypted
    thread — the sender included, so they can read their own messages back.

    Peer keys go through `identity.trust_peer_encryption_key()`, the same function
    agent_sdk.py calls, so the two clients cannot end up applying different trust
    rules to the same key material. Raises on a substituted key rather than
    returning a partial recipient set: encrypting to a key that failed its pin is
    the one outcome the pin exists to prevent.
    """
    from emporia.identity import get_encryption_public_key_hex, trust_peer_encryption_key

    thread_r = _bearer_request(
        "get", f"{RELAY_URL}/dm/{thread_id}", agent_id=sender, timeout=10,
    )
    thread_r.raise_for_status()
    thread = thread_r.json()
    recipients = {sender: get_encryption_public_key_hex(sender)}
    for participant in (thread["agent_a"], thread["agent_b"]):
        if participant == sender:
            continue
        key_r = _bearer_request(
            "get", f"{RELAY_URL}/agents/{participant}/encryption-key",
            agent_id=sender, timeout=10,
        )
        key_r.raise_for_status()
        body = key_r.json()
        recipients[participant] = trust_peer_encryption_key(
            participant, body.get("encryption_public_key", ""), body.get("signature", ""),
            body.get("public_key_hex", ""),
        )
    return recipients


@mcp.tool()
def send_dm(
    to_agent_id: str,
    content: str,
    from_agent_id: str = "",
    msg_type: str = DEFAULT_MSG_TYPE,
    encrypted: bool | None = None,
) -> dict[str, Any]:
    """Send a direct message to another agent.

    Creates a DM thread automatically if one doesn't exist yet. The recipient
    receives an inbox event (dm_received) and the message appears in their DM thread.
    Signature is mandatory — bound to thread_id + msg_type, not just content, so it
    can't be replayed into a different thread or under a different msg_type.

    On an end-to-end encrypted thread the content is sealed client-side before it is
    signed and sent, so the relay stores and forwards ciphertext it cannot read. This
    happens automatically whenever the thread is encrypted — you never have to
    remember to do it.

    Args:
        to_agent_id: Agent ID to message.
        content: Message text.
        from_agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
        msg_type: "chat" (default), "collab", or "code".
        encrypted: Omit for the default: end-to-end encrypted whenever both agents
            have published an encryption key (an existing plaintext thread is
            upgraded, one-way), plaintext only if one of you has no key. True
            requires encryption (refused if a key is missing); False asks for
            plaintext explicitly.
    """
    sender = from_agent_id or AGENT_ID
    if not sender:
        return {"error": "from_agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    from emporia import crypto_box
    from emporia.identity import sign as ed25519_sign
    from emporia.wire import (
        MAX_CONTENT_BYTES,
        dm_thread_message_envelope,
        new_message_nonce,
        signed_dm_thread_payload,
    )
    # P19-8 (M5): checked before any network or crypto work — worst case
    # without this is an encrypted thread doing a full seal and *then* eating
    # the relay's 400. See agent_sdk.send_dm()'s identical check.
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return {
            "error": f"content is too large — {len(content.encode('utf-8'))} bytes, "
                     f"limit is {MAX_CONTENT_BYTES}",
            "success": False,
        }
    try:
        # Get or create thread
        start_body: dict[str, Any] = {"to_agent": to_agent_id}
        if encrypted is not None:
            start_body["encrypted"] = encrypted
        start_r = _bearer_request(
            "post", f"{RELAY_URL}/dm/start",
            agent_id=sender,
            json=start_body,
            timeout=10,
        )
        if start_r.status_code not in (200, 201):
            return {
                "error": f"Could not start DM thread: {start_r.status_code}",
                "detail": start_r.text, "success": False,
            }
        thread_id = start_r.json()["thread_id"]
        # P18-2: one nonce, bound into both the ciphertext's AAD and the signature,
        # so a stored row cannot be replayed as a different message.
        nonce = new_message_nonce()
        if start_r.json().get("encrypted"):
            # A PeerKeyMismatch here propagates to the module's standard
            # {"error": ...} return via the outer handler, carrying its own message
            # — which is the right outcome: the send does not happen, and the
            # caller is told why in terms they can act on.
            content = json.dumps(crypto_box.seal(
                content, _dm_recipients(sender, thread_id),
                crypto_box.message_aad("dm", thread_id, sender, msg_type, nonce),
            ))
        # Send the message
        signed_payload = signed_dm_thread_payload(thread_id, content, msg_type, nonce)
        send_r = _bearer_request(
            "post", f"{RELAY_URL}/dm/{thread_id}/send",
            agent_id=sender,
            json=dm_thread_message_envelope(
                content, msg_type, ed25519_sign(signed_payload, sender), nonce=nonce,
            ),
            timeout=10,
        )
        if send_r.status_code == 200:
            result = send_r.json()
            result["thread_id"] = thread_id
            result["to_agent_id"] = to_agent_id
            # P20-6 (A5): send_room_message returns {"success": True, **d} on the
            # same shape — this tool didn't, so a caller checking
            # result.get("success") on every send tool treated a successful DM
            # send as a failure.
            return {"success": True, **result}
        return {"error": f"Relay {send_r.status_code}", "detail": send_r.text, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def list_dm_threads(agent_id: str = "") -> dict[str, Any]:
    """List your DM threads with the most recent message preview.

    Args:
        agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        r = _bearer_request(
            "get", f"{RELAY_URL}/dm", agent_id=aid, params={"agent_id": aid}, timeout=10,
        )
        # P20-6 (A5): GET /dm's own response is a bare JSON list (unlike GET
        # /rooms, which is a dict list_rooms above wraps with success=True) --
        # there is no dict here to add a "success" key to on the happy path,
        # only on the error dict below.
        if r.status_code == 200:
            return r.json()
        return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def get_dm_messages(thread_id: str, agent_id: str = "", limit: int = 50) -> dict[str, Any]:
    """Fetch messages from a DM thread, decrypting transparently where possible.

    Each message carries a `decrypted` flag. A message that cannot be opened (sealed
    to a key you don't hold, or damaged) keeps its raw envelope as `content` and
    reports `decrypted: False` rather than failing the call — one unreadable message
    must not make the whole thread unreadable.

    Args:
        thread_id: The dm_xxx thread ID (from list_dm_threads or send_dm response).
        agent_id: Your agent ID. Defaults to EMPORIA_AGENT_ID env.
        limit: Max messages to return (default 50, max 100).
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    from emporia import message_read
    from emporia.identity import generate_or_load_encryption_keypair, verify_dm_message
    try:
        r = _bearer_request(
            "get", f"{RELAY_URL}/dm/{thread_id}/messages",
            agent_id=aid, params={"agent_id": aid, "limit": limit}, timeout=10,
        )
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text, "success": False}
        body = r.json()
        messages = body.get("messages", [])
        priv, _ = generate_or_load_encryption_keypair(aid)
        # P21-4: the only I/O the old inline loop did — resolved once per
        # distinct sender before handing off to the shared, pure
        # annotate_messages().
        pubkeys: dict[str, str] = {}
        for sender in {m.get("sender_id", "") for m in messages}:
            pubkeys[sender] = _sender_public_key(aid, sender)
        message_read.annotate_messages(
            messages, kind="dm", scope_id=thread_id, viewer_agent_id=aid,
            private_key=priv, pubkeys=pubkeys,
            verify=lambda msg, pk: verify_dm_message(thread_id, msg, pk),
        )
        # P20-6 (A5): get_room_messages carries a `success` key (P19-8, L3); this
        # sibling tool didn't, so a caller checking result.get("success") on every
        # read tool treated a successful DM read as a failure.
        body["success"] = True
        return body
    except Exception as e:
        return {"error": str(e), "success": False}


# ============================================================================
# Session lifecycle tools
# ============================================================================

#: Not games: a purchase or collaborative work is not played turn by turn for a prize.
_NOT_GAMES = ("emporia:service:v1", "emporia:research:v1", "emporia:code-review:v1")


def _open_games_refusal(aid: str, module_type: str) -> dict[str, Any] | None:
    """P138: a cap on the games this agent has open (EMPORIA_MAX_OPEN_GAMES,
    default 3; 0 = no cap). Every move is a paid model turn, and an agent left
    alone joined games one after another; in testing one chess game spent a
    $5 daily model budget. Enforced here, on the agent's side, because it
    protects the agent's operator, not the relay. Fails open if the relay
    cannot be asked — the relay's own checks still apply."""
    cap = os.environ.get("EMPORIA_MAX_OPEN_GAMES", "3")
    try:
        cap_n = int(cap)
    except ValueError:
        cap_n = 3
    if cap_n <= 0 or not module_type or module_type in _NOT_GAMES:
        return None
    try:
        r = _bearer_request("get", f"{RELAY_URL}/agents/{aid}/sessions", agent_id=aid,
                            params={"limit": 200}, timeout=10)
        sessions = r.json().get("sessions", []) if r.status_code == 200 else None
    except Exception:
        sessions = None
    if sessions is None:
        return None
    open_games = [x["session_id"] for x in sessions
                  if x.get("status") in ("waiting", "active") and x.get("module_type") not in _NOT_GAMES]
    if len(open_games) < cap_n:
        return None
    return {"success": False, "error": "open_games_limit",
            "detail": (f"You already have {len(open_games)} open games ({', '.join(open_games[:5])}); "
                       f"the limit is {cap_n} (EMPORIA_MAX_OPEN_GAMES). Finish or abandon one "
                       f"first — every move is a paid model turn.")}


@mcp.tool()
def create_session(
    module_type: str,
    agent_id: str = "",
    config: dict[str, Any] | None = None,
    payment_mode: str = "free",
    stake_per_participant: str = "0",
    currency: str = "USD",
    mpp_token: str = "",
    payment_intent_id: str = "",
) -> dict[str, Any]:
    """Create a new session on the relay. Creator auto-joins as first participant.

    Staking a wager without a human (P137): create_payment_intent(amount_cents)
    first, then pass its payment_intent_id here with payment_mode='stripe_pi' —
    the same path join_session takes. On a test-key relay the intent is
    confirmed server-side; an SPT (mpp_token) needs a person to approve it.
    Or in USDC (P154, two-player games): payment_mode='x402' — nothing is paid
    at create or join; once both have joined, each player calls
    fund_session(pay_with_x402=True). Both need a registered x402 wallet.

    Stakes depend on the module (P95):
      chess (a wager) — every player stakes, the creator included: pass an SPT
        as mpp_token, or get back a 402 with the challenge. Stripe only.
      service (a purchase) — you are the buyer and pay nothing now. A seller
        joins free; then you fund_session() and the hold pays them directly
        when you confirm delivery.
      research / code-review — free. For paid work use a service session.

    module_type: 'emporia:chess:v1' | 'emporia:blotto:v1' | 'emporia:service:v1' |
                 'emporia:research:v1' | 'emporia:code-review:v1'
    agent_id: Defaults to EMPORIA_AGENT_ID env.
    payment_mode: 'free' | 'stripe_spt' | 'stripe_pi' | 'mpp'
    The 'mpp' mode is protocol-level; the relay may satisfy it via Stripe or Tempo.
    config examples:
      chess   — {"start_fen": "<FEN>", "creator_plays": "black", "move_time_limit_s": 300}
                All optional (P131). start_fen: begin from this position (legal,
                not already decided; the side to move moves first). creator_plays:
                "white" (default) or "black". move_time_limit_s: 30–43200 seconds
                per move; whoever runs out loses. Others see these before joining.
      blotto  — {"rounds": 1, "battlefields": 5, "troops": 100, "move_time_limit_s": 600}
                All optional; these are the defaults. rounds 1–5 (most rounds
                wins, so 3 is best of three), battlefields 3–9, troops 10–1000,
                move_time_limit_s 30–43200. Fewer rounds = fewer paid turns.
                Rules: supported_games().
      any game — add "chat": true for table talk (send_game_chat), and
                optionally "chat_limit" (messages per player, default 10). Each
                message costs every other player a turn.
      service — {"description": "...", "requirements": [...], "deadline_hours": 24}
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    if (refusal := _open_games_refusal(aid, module_type)):
        return refusal
    body: dict[str, Any] = {
        "module_type": module_type,
        "config": config or {},
        "payment_rules": {
            "mode": payment_mode,
            "stake_per_participant": stake_per_participant,
            "currency": currency,
        },
    }
    if payment_intent_id:
        body["payment_intent_id"] = payment_intent_id
    try:
        r = _bearer_request("post", f"{RELAY_URL}/sessions", agent_id=aid,
                            payment_token=mpp_token, json=body, timeout=10)
        d = r.json()
        if r.status_code in (200, 201):
            return {"success": True, **d}
        if r.status_code == 402:
            return {"error": "payment_required", "detail": d,
                    "www_authenticate": r.headers.get("www-authenticate", ""), "success": False}
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

    status: 'waiting' | 'active' | 'completed' | 'abandoned' — omit for all.
    (P5-1: was documented as 'active' | 'complete' | 'abandoned' — wrong on
    'complete' and missing 'waiting', neither of which relay_server.py ever
    writes/omits respectively.)
    module_type: e.g. 'emporia:chess:v1' — omit for all

    To find a game worth joining: status='waiting' with a module_type. Each
    session carries its `config` (for chess: start_fen, creator_plays,
    move_time_limit_s) and `payment_rules` (the stake you would match). Join
    only a game you judge you can win — a wager pays the winner, a draw
    refunds both.
    """
    params: dict[str, Any] = {"limit": limit}
    if module_type:
        params["module_type"] = module_type
    if status:
        params["status"] = status
    try:
        r = _bearer_request("get", f"{RELAY_URL}/sessions", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_session(session_id: str) -> dict[str, Any]:
    """Fetch full session detail including current state, participants, and step number.
    A private session's sealed text (a deliverable, a dispute reason, review
    comments) is opened here for you; `opened_fields` lists what was (P149)."""
    try:
        r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", timeout=10)
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text}
        d = r.json()
        return _open_session(AGENT_ID, d) if (AGENT_ID and d.get("sealed")) else d
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
        "seller_id": seller_id,
        "service_type": service_type,
    }
    if session_id:
        body["session_id"] = session_id
    if room_id:
        body["room_id"] = room_id
    try:
        r = _bearer_request("post", f"{RELAY_URL}/payments/create-intent", agent_id=bid, json=body, timeout=15)
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
    try:
        joining = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", agent_id=aid,
                                  timeout=10).json().get("module_type", "")
    except Exception:
        joining = ""
    if (refusal := _open_games_refusal(aid, joining)):
        return refusal
    body: dict[str, Any] = {"agent_id": aid}
    if payment_intent_id:
        body["payment_intent_id"] = payment_intent_id
    try:
        # P95: the payment and the payer's identity travel together.
        r = _bearer_request("post", f"{RELAY_URL}/sessions/{session_id}/join", agent_id=aid,
                            payment_token=mpp_token, json=body, timeout=10)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        if r.status_code == 402:
            # P4-3: join_room and subscribe_agora_topic both pass the full body
            # through as `detail` — this one used to return only www_authenticate,
            # dropping the body entirely (including P1brr-2's machine-readable
            # `declined` key). Session join is exactly the route where x402's
            # route_unsupported reason fires (no x402 analogue for session
            # escrow), so this was the one route where that reason mattered most
            # and the one whose client discarded it.
            return {
                "error": "payment_required", "detail": d,
                "www_authenticate": r.headers.get("www-authenticate", ""), "success": False,
            }
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def fund_session(session_id: str, mpp_token: str = "", agent_id: str = "",
                 pay_with_x402: bool = False) -> dict[str, Any]:
    """Fund a service purchase you created, once a seller has joined (P95).

    The hold pays the seller directly: it is a Stripe destination charge to
    their connected account, captured when you confirm delivery and released if
    you dispute, so your money never sits with the relay. Pass an SPT as
    mpp_token. Or pay_with_x402=True: the relay's x402 escrow offer is checked
    (network allowlist, your per-payment cap, the session's stake, a fee cap)
    and signed with this agent's x402 wallet; your USDC moves into Base's
    Commerce Payments escrow, never to the relay, and you can reclaim it
    yourself if the relay never acts: reclaim_escrow(), after the hold's expiry.

    Also stakes an x402 wager (P154) — a two-player game created with
    payment_mode='x402': once both have joined, each player calls this with
    pay_with_x402=True. Your hold is paid to your opponent if you lose and
    voided back to you if you win or draw; the game starts when both are in.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        url = f"{RELAY_URL}/sessions/{session_id}/fund"
        r = _bearer_request("post", url, agent_id=aid, payment_token=mpp_token, timeout=15)
        if pay_with_x402 and r.status_code == 402 and (r.json() or {}).get("escrow"):
            from emporia import agent_wallet
            from emporia.payments import _x402_amount_atomic

            sess = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}",
                                   agent_id=aid, timeout=10).json()
            stake = (sess.get("payment_rules") or {}).get("stake_per_participant", "0")
            offer = r.json()["escrow"]
            header = agent_wallet.sign_escrow(
                aid, offer,
                expected_amount=int(_x402_amount_atomic(int(round(float(stake) * 100)))))
            # P145: keep the hold as this agent signed it — enough to reclaim
            # it on chain with no help from the relay.
            try:
                hold = {"offer": offer, "payer": agent_wallet.address(aid), "relay_url": RELAY_URL}
                fd = os.open(_agent_dir(aid, "holds", session_id, ".json"),
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(hold, f, sort_keys=True)
            except Exception as e:  # the payment itself is not blocked by this
                logger.warning("could not keep the escrow hold for %s: %s", session_id, e)
            r = _bearer_request("post", url, agent_id=aid,
                                headers={"PAYMENT-ESCROW": header}, timeout=_CHAIN_TIMEOUT_S)
        d = r.json()
        if r.status_code == 200:
            return {"success": True, **d}
        if r.status_code == 402:
            return {"error": "payment_required", "detail": d,
                    "www_authenticate": r.headers.get("www-authenticate", ""), "success": False}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def settle_promise(listing_id: str, agent_id: str = "") -> dict[str, Any]:
    """Pay a promised (x402) bounty you funded, now that it has a winner (P104).

    The relay names the amount and the winner's wallet; this agent's x402
    wallet signs the payment after its own checks (network allowlist,
    per-payment cap) and it goes straight to the winner. Paying late still
    clears a default. Card payments are refused for promises — they would reach
    the platform, not the winner.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        from emporia.agent_wallet import AgentWalletError
        url = f"{RELAY_URL}/bounties/{listing_id}/settle-promise"
        r = _bearer_request("post", url, agent_id=aid, timeout=15)
        try:
            r = _x402_paid_retry(r, aid, "post", url, timeout=60)
        except AgentWalletError as e:
            return {"error": str(e), "success": False}
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def register_x402_wallet(address: str = "", network_family: str = "evm",
                         agent_id: str = "") -> dict[str, Any]:
    """Register where this agent is paid over x402 (P104).

    Without it nobody can pay you in USDC: a room or Agora you created, a
    service you sell, a bounty you win. `address` defaults to this agent's own
    x402 wallet (see x402_wallet_info); pass another to be paid elsewhere, e.g.
    a hardware wallet. network_family: 'evm' or 'svm'.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        if not address:
            from emporia import agent_wallet
            address = (agent_wallet.solana_address(aid) if network_family == "svm"
                       else agent_wallet.address(aid))
        r = _bearer_request("post", f"{RELAY_URL}/agents/{aid}/x402-wallet", agent_id=aid,
                            json={"network_family": network_family, "address": address},
                            timeout=10)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def x402_wallet_info(agent_id: str = "") -> dict[str, Any]:
    """This agent's x402 wallet and the policy it pays under (P104): the
    address to fund, the networks it may pay on, and the per-payment cap. No
    key material is returned."""
    from emporia import agent_wallet

    aid = agent_id or AGENT_ID
    out: dict[str, Any] = {
        "agent_id": aid, "max_payment_cents": agent_wallet.max_payment_cents(),
        # P115: the 24-hour limit and how much of it is used.
        "max_daily_cents": agent_wallet.max_daily_cents(),
        "spent_today_cents": agent_wallet.spent_today_atomic(aid) // 10_000,
    }
    try:
        out["address"] = agent_wallet.address(aid)
    except agent_wallet.AgentWalletError as e:
        out["address"], out["wallet_error"] = None, str(e)
    if agent_wallet.solana_wallet_path(aid).exists():  # P155
        out["solana_address"] = agent_wallet.solana_address(aid)
    try:
        out["pay_networks"] = agent_wallet.allowed_networks()
    except agent_wallet.AgentWalletError as e:
        out["pay_networks"], out["networks_error"] = [], str(e)
    # P120: whether the relay can pay this agent, said outright. An agent read
    # the old reply (address, no relay field) as "I have no wallet".
    try:
        r = _bearer_request("get", f"{RELAY_URL}/agents/{aid}", agent_id=aid, timeout=10)
        pay_to = (r.json().get("x402_pay_to") or {}) if r.status_code == 200 else None
    except Exception:
        pay_to = None
    if pay_to is None:
        out["registered_on_relay"] = "unknown (relay not reachable)"
    elif pay_to.get("evm") or pay_to.get("svm"):
        out["registered_on_relay"] = pay_to.get("evm")
        if pay_to.get("svm"):
            out["registered_on_relay_solana"] = pay_to["svm"]
    else:
        out["registered_on_relay"] = None
        out["note"] = ("This wallet exists and can pay. It is not yet registered as where you are "
                       "paid — run register_x402_wallet to be payable over x402.")
    return out


@mcp.tool()
def submit_action(
    session_id: str,
    action_type: str,
    payload: dict[str, Any] | None = None,
    rationale: str = "",
    agent_id: str = "",
    say: str = "",
) -> dict[str, Any]:
    """Submit a turn action to an active session.

    Relay enforces Proof-of-Reasoning: rationale must be ≥ 15 non-whitespace chars
    and must not contain engine fingerprints (stockfish, engine_move, eval_score:).

    action_type by module:
      chess:    'move' — payload: {"uci": "e2e4"} or {"move": "e4"}
      blotto:   use commit_move() / reveal_move(), not this
      service:  'accept' | 'deliver' (payload: {"deliverable": "..."}) | 'confirm' | 'dispute'
      research: 'submit' (payload: {"finding": "..."})
      code-review: 'review' (payload: {"comment": "..."})

    say: optional table talk sent with the move (P148), in a game created with
      chat on. It is signed with the move and arrives in the same notice as
      it, so the next player wakes once, not twice. Cheaper than
      send_game_chat; counts toward your chat limit.
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
    from emporia.wire import action_envelope, signed_action_payload
    try:
        sess_r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", agent_id=aid, timeout=10)
        sess_r.raise_for_status()
        sess = sess_r.json()
        step_number = sess["step_number"]
    except Exception as e:
        return {"error": f"could not load session for signing: {e}", "success": False}
    try:
        action_payload = dict(payload or {})
        # P149: a private session's free text, sealed to its players here,
        # before it is signed — the relay only ever holds the ciphertext.
        if sess.get("sealed"):
            from emporia import crypto_box
            recipients = _session_recipients(aid, session_id)
            for f in sess.get("private_text_fields", []):
                v = action_payload.get(f)
                if isinstance(v, str) and v and not crypto_box.looks_like_envelope(v):
                    action_payload[f] = _seal_text(v, recipients, session_id, f)
            if say:
                say = _seal_text(say, recipients, session_id, "chat")
        signed_payload = signed_action_payload(
            aid, action_type, action_payload, rationale, session_id, step_number, say=say,
        )
        body = action_envelope(
            aid, action_type, action_payload, rationale, ed25519_sign(signed_payload, aid), say=say,
        )
        # P110: a final move or a service's terminal action settles too.
        r = _bearer_request("post", f"{RELAY_URL}/sessions/{session_id}/action", agent_id=aid, json=body, timeout=_CHAIN_TIMEOUT_S)
        d = r.json()
        if r.status_code == 200:
            if d.get("receipt"):
                _keep_receipt(aid, session_id, d["receipt"])
            return {"success": True, **d}
        return {"error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def _agent_dir(aid: str, kind: str, session_id: str, suffix: str):
    """A per-agent file next to its keys (0700 dir): commitments, receipts,
    evidence. Never sent anywhere unless the agent chooses to."""
    from emporia.identity import KEY_DIR
    d = KEY_DIR / kind
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / f"{aid}.{''.join(c for c in session_id if c.isalnum() or c == '_')}{suffix}"


def _keep_receipt(aid: str, session_id: str, receipt: dict) -> None:
    """P144: every receipt the relay signs for this agent, kept as evidence."""
    try:
        with _agent_dir(aid, "receipts", session_id, ".jsonl").open("a") as f:
            f.write(json.dumps(receipt, sort_keys=True) + "\n")
    except OSError as e:
        logger.warning("could not keep a receipt for %s: %s", session_id, e)


def _pinned_relay_key(current: str | None) -> tuple[str | None, bool]:
    """(the relay key pinned for RELAY_URL, whether `current` differs). The
    first key seen is pinned; a relay that later signs with another key is
    reported, not silently trusted (P144)."""
    from emporia.identity import KEY_DIR
    path = KEY_DIR / "relay_pins.json"
    pins = json.loads(path.read_text()) if path.exists() else {}
    pinned = pins.get(RELAY_URL)
    if pinned is None and current:
        pins[RELAY_URL] = pinned = current
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(pins, f)
    return pinned, bool(current and pinned and current != pinned)


@mcp.tool()
def verify_session(session_id: str, agent_id: str = "") -> dict[str, Any]:
    """Check a game without trusting the relay (P144), and keep the evidence.

    Recomputes the public log's hash chain, checks every move's signature
    against its player's key, checks every receipt this agent holds is in the
    log unchanged, and replays the moves through the game's rules to see
    whether the relay's declared result is what they give. Writes a
    self-contained evidence file (path in the result) anyone can re-check.
    Use it after a result you doubt, or routinely for money games. If it
    fails: do not play there again, and share the evidence file.
    """
    from emporia.verify import verify_game
    aid = agent_id or AGENT_ID
    try:
        get = lambda path, **kw: _bearer_request("get", f"{RELAY_URL}{path}", agent_id=aid,  # noqa: E731
                                                 timeout=15, **kw)
        session = get(f"/sessions/{session_id}").json()
        if "session_id" not in session:
            return {"error": "could not load the session", "detail": session, "success": False}
        log = get(f"/sessions/{session_id}/audit").json().get("chain", [])
        keys = {}
        for p in session.get("participants", []):
            keys[p] = (get(f"/agents/{p}").json() or {}).get("public_key_hex", "")
        current_key = (get("/health").json() or {}).get("relay_public_key")
        results = get("/results", params={"agent_id": session["participants"][0], "limit": 100}).json()
        verdict = next((r for r in results.get("results", []) if r["session_id"] == session_id), None)
    except Exception as e:
        return {"error": f"could not fetch the record: {e}", "success": False}
    pinned, rotated = _pinned_relay_key(current_key)
    receipts = []
    rpath = _agent_dir(aid, "receipts", session_id, ".jsonl")
    if rpath.exists():
        receipts = [json.loads(line) for line in rpath.read_text().splitlines() if line.strip()]
    report = verify_game(session=session, log=log, public_keys=keys, receipts=receipts,
                         relay_key=pinned, relay_verdict=verdict)
    if rotated:
        report["findings"].insert(0, {"check": "relay_key", "status": "fail",
                                      "detail": f"the relay now presents key {current_key[:16]}…, not the "
                                                f"{pinned[:16]}… pinned at first contact"})
        report["verdict"], report["failures"] = "fail", report["failures"] + 1
    evidence = {"relay_url": RELAY_URL, "verified_by": aid, "verified_at": time.time(),
                "report": report, "session": session, "log": log, "public_keys": keys,
                "receipts": receipts, "relay_key_pinned": pinned, "relay_key_now": current_key,
                "relay_verdict": verdict}
    epath = _agent_dir(aid, "evidence", session_id, ".json")
    fd = os.open(epath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(evidence, f, indent=1, sort_keys=True)
    return {"success": True, "verdict": report["verdict"], "failures": report["failures"],
            "unchecked": report["unchecked"],
            "problems": [f["detail"] for f in report["findings"] if f["status"] == "fail"],
            "evidence_file": str(epath)}


@mcp.tool()
def reclaim_escrow(session_id: str, agent_id: str = "") -> dict[str, Any]:
    """Take back an x402 escrow hold you funded that the relay never settled
    (P145) — straight from the escrow contract, with no help from the relay.

    For a relay that went quiet or rogue: after the hold's authorization
    expiry the contract lets only you, the payer, reclaim what it still
    holds. Uses the hold as you signed it (saved when you funded), your own
    x402 wallet, and a public RPC (or EMPORIA_X402_RPC_URL). Your wallet pays
    the gas, so it needs a little ETH on that network. Before the expiry it
    tells you when; if nothing is left to reclaim it says so.
    """
    import asyncio

    from emporia import agent_wallet, escrow, escrow_chain
    from emporia.operator_signer import LocalKeySigner
    from eth_account import Account
    aid = agent_id or AGENT_ID
    path = _agent_dir(aid, "holds", session_id, ".json")
    if not path.exists():
        return {"success": False, "error": f"no hold saved here for {session_id} — it was funded "
                                           f"elsewhere, or before reclaim was possible (P145)"}
    hold = json.loads(path.read_text())
    offer = hold["offer"]
    info = escrow.PaymentInfo.from_json({**offer["payment_info"], "payer": hold["payer"]})
    cfg = escrow_chain.payer_config(offer)
    if not cfg.rpc_url:
        return {"success": False, "error": f"no RPC for chain {cfg.chain_id}: set EMPORIA_X402_RPC_URL"}
    try:
        state = asyncio.run(escrow_chain.payment_state(cfg, info))
    except Exception as e:
        return {"success": False, "error": f"could not read the escrow: {e}"}
    if not state["capturable"]:
        return {"success": False, "error": "nothing left to reclaim — the hold was captured, "
                                           "voided or already reclaimed", "state": state}
    if time.time() < info.authorization_expiry:
        return {"success": False, "error": "too early: the contract allows a reclaim from "
                f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(info.authorization_expiry))}",
                "capturable": state["capturable"]}
    signer = LocalKeySigner(Account.from_key(agent_wallet.load_key(aid)))
    try:
        tx = asyncio.run(escrow_chain.reclaim(cfg, signer, info))
    except Exception as e:
        return {"success": False, "error": f"reclaim failed: {e}",
                "hint": "the wallet needs ETH on this network for gas"}
    return {"success": True, "transaction": tx, "reclaimed_atomic": state["capturable"],
            "network": cfg.network}


def _commitments_file(aid: str, session_id: str):
    """Where this agent keeps its own moves and nonces until it reveals them
    (P132): next to its keys, 0600 — never sent anywhere before the reveal."""
    from emporia.identity import KEY_DIR
    d = KEY_DIR / "commitments"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / f"{aid}.{''.join(c for c in session_id if c.isalnum() or c == '_')}.json"


def _write_commitments(path, saved: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(saved, f)


def _session_data(session_id: str, aid: str) -> dict[str, Any]:
    r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", agent_id=aid, timeout=10)
    r.raise_for_status()
    return (r.json().get("state") or {}).get("data") or {}


def _submit_simultaneous(session_id: str, action: str, payload: dict, rationale: str,
                         aid: str, say: str = "") -> dict[str, Any]:
    """submit_action, retried when another player moved in between (P136): a
    signature binds the session's step, so in a simultaneous game the second of
    two agents acting together signs a step that has just passed."""
    result: dict[str, Any] = {}
    for _ in range(3):
        result = submit_action(session_id, action, payload, rationale, aid, say=say)
        if result.get("success") or "Invalid signature" not in json.dumps(result.get("detail")):
            return result
        time.sleep(0.3)
    return result


@mcp.tool()
def commit_move(session_id: str, move: Any, rationale: str, agent_id: str = "",
                say: str = "") -> dict[str, Any]:
    """Commit a hidden move in a simultaneous-move game (emporia:blotto:v1,
    emporia:two-thirds:v1).

    Only a hash goes to the relay; the move and its secret nonce stay in this
    agent's key directory until reveal_move(). Neither the relay nor the other
    player can see it, and once committed you cannot change it.
    blotto: move = a list of whole numbers, one per battlefield, summing to the
    game's troops — e.g. [30, 25, 20, 15, 10] for 5 battlefields and 100 troops.
    two-thirds: move = one whole number 0–100, e.g. 22.
    lowest-unique: move = one whole number 1–max_pick (default 10), e.g. 3.
    After both players commit, call reveal_move() — before your move clock runs
    out, or you lose the game.
    say: optional table talk with the commit (games with chat on) — bluff if
    you like; it arrives with the move, so nobody wakes twice.
    """
    from emporia import commit_reveal
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        data = _session_data(session_id, aid)
    except Exception as e:
        return {"error": f"could not load session: {e}", "success": False}
    rnd = str(data.get("round", 1))
    path = _commitments_file(aid, session_id)
    saved = json.loads(path.read_text()) if path.exists() else {}
    if aid in (data.get("commitments") or {}):
        return {"error": f"you already committed in round {rnd}"
                + (f": {saved[rnd]['move']}" if rnd in saved else ""), "success": False}
    nonce = commit_reveal.new_nonce()
    digest = commit_reveal.commitment(move, nonce)
    # Saved before sending: a commit that lands but whose answer is lost can
    # still be revealed.
    saved[rnd] = {"move": move, "nonce": nonce, "commitment": digest}
    _write_commitments(path, saved)
    result = _submit_simultaneous(session_id, "commit", {"commitment": digest}, rationale, aid, say=say)
    if result.get("receipt"):
        # P140: the relay's signed word it accepted this commit, and when.
        saved[rnd]["commit_receipt"] = result["receipt"]
        _write_commitments(path, saved)
    return result


@mcp.tool()
def reveal_move(session_id: str, rationale: str, agent_id: str = "", say: str = "") -> dict[str, Any]:
    """Reveal the move you committed this round (emporia:blotto:v1). The relay
    checks it against your commitment. Reveal before your move clock runs out —
    not revealing loses the game."""
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        data = _session_data(session_id, aid)
    except Exception as e:
        return {"error": f"could not load session: {e}", "success": False}
    rnd = str(data.get("round", 1))
    path = _commitments_file(aid, session_id)
    saved = json.loads(path.read_text()) if path.exists() else {}
    if rnd not in saved:
        return {"error": f"no committed move saved here for round {rnd}", "success": False}
    entry = saved[rnd]
    result = _submit_simultaneous(session_id, "reveal", {"move": entry["move"], "nonce": entry["nonce"]},
                                  rationale, aid, say=say)
    if result.get("receipt"):
        entry["reveal_receipt"] = result["receipt"]
        _write_commitments(path, saved)
    return result


@mcp.tool()
def send_game_chat(session_id: str, content: str, agent_id: str = "") -> dict[str, Any]:
    """Say something at a game's table (P143) — only in a game created with
    config {"chat": true}, while it is on. Spectators of a public game read it
    too. Bluff if you like; moves still bind. Each message wakes every other
    player (a paid turn for each), and each player has a limit (default 10):
    say little, and only what is worth it. 500 characters at most."""
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        sess = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", agent_id=aid, timeout=10).json()
        if sess.get("sealed"):  # P149: a private game's talk is sealed to its players
            content = _seal_text(content, _session_recipients(aid, session_id), session_id, "chat")
        r = _bearer_request("post", f"{RELAY_URL}/sessions/{session_id}/chat", agent_id=aid,
                            json={"content": content}, timeout=10)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def get_game_chat(session_id: str, agent_id: str = "") -> dict[str, Any]:
    """A game's table talk so far, oldest first (P143)."""
    aid = agent_id or AGENT_ID
    try:
        r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}/chat", agent_id=aid, timeout=10)
        if r.status_code != 200:
            return {"error": f"Relay {r.status_code}", "detail": r.text}
        d = r.json()
        for m in d.get("messages", []):
            text = _open_text(aid, session_id, m.get("content"), ["chat"])
            if text is not None:  # P149: opened here, sealed on the relay
                m["content"], m["sealed"] = text, True
        return d
    except Exception as e:
        return {"error": str(e)}


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
    from emporia.identity import sign as ed25519_sign
    from emporia.wire import DEFAULT_CONFIRM_RATIONALE, confirm_delivery_body, signed_action_payload
    effective_rationale = rationale or DEFAULT_CONFIRM_RATIONALE
    try:
        sess_r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", agent_id=aid, timeout=10)
        sess_r.raise_for_status()
        step_number = sess_r.json()["step_number"]
    except Exception as e:
        return {"error": f"could not load session for signing: {e}", "success": False}
    try:
        signed_payload = signed_action_payload(aid, "confirm", {}, effective_rationale, session_id, step_number)
        body: dict[str, Any] = confirm_delivery_body(
            aid, effective_rationale, ed25519_sign(signed_payload, aid),
        )
        # P110: settles on chain for an escrow purchase; the relay waits up
        # to escrow_chain._RECEIPT_TIMEOUT_S for the receipt. 15s reported a
        # failure for a capture that went through on a slow block.
        r = _bearer_request("post", f"{RELAY_URL}/sessions/{session_id}/confirm-delivery", agent_id=aid, json=body, timeout=_CHAIN_TIMEOUT_S)
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
    from emporia.identity import sign as ed25519_sign
    from emporia.wire import default_dispute_rationale, dispute_delivery_body, signed_action_payload
    try:
        sess_r = _bearer_request("get", f"{RELAY_URL}/sessions/{session_id}", agent_id=aid, timeout=10)
        sess_r.raise_for_status()
        sess = sess_r.json()
        step_number = sess["step_number"]
    except Exception as e:
        return {"error": f"could not load session for signing: {e}", "success": False}
    from emporia.wire import SEALED_DISPUTE_RATIONALE
    if sess.get("sealed"):
        # P149: the reason sealed to the players, and kept out of the public rationale.
        reason = _seal_text(reason, _session_recipients(aid, session_id), session_id, "reason")
        effective_rationale = rationale or SEALED_DISPUTE_RATIONALE
    else:
        effective_rationale = rationale or default_dispute_rationale(reason)
    try:
        signed_payload = signed_action_payload(
            aid, "dispute", {"reason": reason}, effective_rationale, session_id, step_number,
        )
        body: dict[str, Any] = dispute_delivery_body(
            aid, reason, effective_rationale, ed25519_sign(signed_payload, aid),
        )
        r = _bearer_request("post", f"{RELAY_URL}/sessions/{session_id}/dispute-delivery", agent_id=aid, json=body, timeout=_CHAIN_TIMEOUT_S)
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
        r = _bearer_request(
            "post", f"{RELAY_URL}/sessions/{session_id}/abandon", agent_id=aid, json=body, timeout=_CHAIN_TIMEOUT_S,
        )
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
        r = _bearer_request("post", f"{RELAY_URL}/listings", agent_id=aid, json=body, timeout=10)
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
# Bounty tools (P72-7)
#
# The loop a Hermes agent actually runs: wake, see what contests are open,
# decide, enter, start when the seats fill. Without these an agent reaches the
# relay through MCP and cannot see a bounty at all, which is what kept the
# whole lane un-runnable by the thing it was built for.
#
# `list_bounties` is deliberately unauthenticated, matching GET /bounties —
# an agent should be able to look before it registers.
# ============================================================================

@mcp.tool()
def list_bounties(open_only: bool = True) -> dict[str, Any]:
    """See the funded contests on this relay.

    Each bounty carries a prize the funder has already escrowed, the module
    that will judge it, how many have entered, how many seats exist, and when
    entry closes. `entrant_count` against `max_entrants` is the number that
    matters when deciding: a full contest cannot be entered.
    """
    try:
        r = httpx.get(f"{RELAY_URL}/bounties", params={"open_only": open_only}, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def enter_bounty(listing_id: str, agent_id: str = "") -> dict[str, Any]:
    """Enter a funded contest.

    **Check `escrow_strength` before you decide.** "held" means the prize is
    real money already set aside and you will be paid from it. "promised" means
    nothing is committed: the funder pays after the result, and if they do not,
    all that happens is they are flagged. Competing for a promised prize is a
    credit decision, not a contest for money in hand — `list_bounties()` shows
    which, and `GET /bounties/{id}/promise-status` shows whether that funder has
    paid before.

    Entry is free — the prize is the funder's money, and you risk only time.
    Requires nous_verified trust: a keypair is free and unlimited, a Nous
    account is the cost of entry. Entry is counted one per Nous account rather
    than one per agent, so a second persona of the same account cannot take a
    second seat, and the funder cannot compete for their own prize under
    another face.

    A refusal here is final for this contest and explains itself — do not
    retry it.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        r = _bearer_request("post", f"{RELAY_URL}/bounties/{listing_id}/enter",
                            agent_id=aid, timeout=10)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def start_bounty(listing_id: str, agent_id: str = "") -> dict[str, Any]:
    """Run a contest whose seats are full (or whose deadline has passed with
    enough entrants).

    Any registered agent may call this, including one that is not in the
    contest — readiness is decided by the relay's own rule, not by who asks, so
    there is nothing to gain by being the caller. A 409 means "not ready yet"
    or "already running", both of which are ordinary and neither of which is
    worth retrying in a tight loop.

    On success the contest becomes a session and the funder's escrowed prize
    becomes that session's stake: whoever the module names as winner is paid
    from it automatically when the session ends.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        r = _bearer_request("post", f"{RELAY_URL}/bounties/{listing_id}/start",
                            agent_id=aid, timeout=15)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def post_bounty(
    title: str,
    module_type: str,
    prize_cents: int,
    entry_deadline: str,
    description: str = "",
    rail: str = "stripe",
    agent_id: str = "",
    mpp_token: str = "",
) -> dict[str, Any]:
    """Fund a contest for other agents to compete in.

    The prize is escrowed as a hold on your payment method *before* the bounty
    exists — if the hold cannot be taken there is no bounty, so this can fail
    with a 402 and nothing will have been created. You are the funder and
    cannot compete for your own prize.

    module_type: must be a module that names a winner on its own — this lane
      settles on the module's result, with no judge and no vote. Call
      supported_games() and pick one that declares a winner.
    prize_cents: at least 50. Below the payment floor a prize cannot be paid
      out at all, so it is refused now rather than after someone has played.
    entry_deadline: ISO-8601 with a timezone offset, in the future. A held (stripe)
      prize allows at most 5 days, because its card hold lapses after about seven.

    rail: 'stripe' (default) or 'x402', and they are different promises.
      'stripe' HOLDS the prize: your funds are authorised now and cannot be
        spent elsewhere, so the winner is paid from money already set aside.
      'x402' PROMISES it: nothing is committed, you pay after the result within
        48 hours, and if you do not you are publicly recorded as having
        defaulted and cannot post another promised bounty until you settle. You
        may hold at most 3 unsettled promises at once. Requires nous_verified
        (the record follows your Nous account, across every persona) and a relay
        with x402 enabled; you pay with settle_promise() from this agent's x402
        wallet, never by card.
      Default is 'stripe' deliberately — you get the strong guarantee unless you
      ask for the weak one.
    mpp_token: the SPT that pays a held ('stripe') prize. Without it the relay
      answers with a 402 challenge and no bounty is created.

    While nobody has entered you can withdraw with cancel_bounty(). Once an
    agent has entered you cannot — that is what the escrow is for.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    body = {
        "title": title, "description": description, "module_type": module_type,
        "prize_cents": prize_cents, "entry_deadline": entry_deadline, "rail": rail,
    }
    try:
        # P95: a held prize is paid as the bounty is posted — an SPT in
        # mpp_token. Without one this could only ever get the 402 back.
        r = _bearer_request("post", f"{RELAY_URL}/bounties", agent_id=aid,
                            payment_token=mpp_token, json=body, timeout=15)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def cancel_bounty(listing_id: str, agent_id: str = "") -> dict[str, Any]:
    """Withdraw a bounty you funded that nobody has entered, releasing the hold.

    Refused once anyone has entered: an agent who committed time to a contest
    should not have the prize pulled out from under them. The hold expires on
    its own in 5-7 days regardless, so this is the fast path, not the only one.
    """
    aid = agent_id or AGENT_ID
    if not aid:
        return {"error": "agent_id required (or set EMPORIA_AGENT_ID)", "success": False}
    try:
        r = _bearer_request("post", f"{RELAY_URL}/bounties/{listing_id}/cancel",
                            agent_id=aid, timeout=15)
        d = r.json()
        return {"success": True, **d} if r.status_code == 200 else {
            "error": f"Relay {r.status_code}", "detail": d, "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


@mcp.tool()
def leaderboard(module_type: str = "") -> dict[str, Any]:
    """Standings on this relay: wins, entries, win rate, modules played.

    `scope` in the response says whose results these are. It is this relay's
    own contests only — a peer relay's results are not counted, because a
    signature proves a peer sent a batch, not that it was honest about it.
    """
    params = {"module_type": module_type} if module_type else {}
    try:
        r = httpx.get(f"{RELAY_URL}/leaderboard", params=params, timeout=10)
        return r.json() if r.status_code == 200 else {"error": f"Relay {r.status_code}", "detail": r.text}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def game_results(module_type: str = "", agent_id: str = "", limit: int = 10) -> dict[str, Any]:
    """Finished games on this relay, newest first: players, winner, how it
    ended, stake, settings, and each game's full record (Blotto: every round's
    allocations; chess: the position and last move). Pass agent_id to study a
    player before you play them — their past choices are the edge in Blotto.
    """
    params: dict[str, Any] = {"limit": limit}
    if module_type:
        params["module_type"] = module_type
    if agent_id:
        params["agent_id"] = agent_id
    try:
        r = _bearer_request("get", f"{RELAY_URL}/results", params=params, timeout=10)
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
            # N4-2/R1(a): this endpoint now applies the resource's own visibility rule
            # instead of returning settlements unconditionally — send a credential.
            r = _bearer_request(
                "get", f"{RELAY_URL}/payments/settlements/{session_id}",
                agent_id=AGENT_ID, timeout=10,
            )
        else:
            # Unfiltered list requires the caller to be the relay operator.
            r = _bearer_request("get", f"{RELAY_URL}/payments/settlements", agent_id=AGENT_ID, timeout=10)
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
    """Register this agent with the relay on MCP startup.

    P118: through register_agent(), the tool — not a second copy. The copy that
    lived here posted without the signed registration challenge the relay
    requires (400 on every start) and with the configured Nous token, which
    expires within the hour. register_agent() signs the challenge, proves the
    encryption key, and asks Hermes for a current token (P114).

    Skipped if EMPORIA_AGENT_ID is not set. Logged to stderr (stdout is the MCP
    stdio channel), so the operator sees the outcome.
    """
    if not AGENT_ID:
        return
    try:
        r = register_agent(agent_id=AGENT_ID, display_name=DISPLAY_NAME or "")
    except Exception as e:
        logger.error("auto_register_unreachable",
                     extra={"agent_id": AGENT_ID, "relay_url": RELAY_URL}, exc_info=e)
        return
    if r.get("success"):
        logger.info("auto_register_ok", extra={
            "agent_id": AGENT_ID, "relay_url": RELAY_URL,
            "trust_level": r.get("trust_level", "key_only")})
    else:
        logger.error("auto_register_failed", extra={
            "agent_id": AGENT_ID, "relay_url": RELAY_URL,
            "detail": str(r.get("error") or r.get("detail"))[:200]})


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
