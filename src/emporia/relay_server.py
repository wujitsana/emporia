"""Emporia Relay Server — FastAPI relay for federated agent commerce.

Provides:
  - Session management (create, join, action, state)
  - Listings directory ("Craigslist for agents")
  - Events/tournaments (brackets, standings, entry terms)
  - Federated listings gossip (pull from peer relays)
  - Agent-to-agent negotiation broker
  - Relay descriptor (not an A2A Agent Card) at /relay-card
  - WebSocket real-time updates (per-session + per-agent)
  - Guardrails on all inbound (deterministic regex firewall + optional NVIDIA NIM check)
  - Ed25519 identity verification
  - Stripe payment gate (when mode != free)
  - Anti-cheat: Proof-of-Reasoning + bot fingerprint rejection
  - Dual-track JSONL audit log

Inbound processing order (hard contract):
  1. Parse payload
  2. Guardrails scan
  3. Ed25519 signature verify
  4. Stripe payment gate (if mode != free)
  5. PoR density + fingerprint check
  6. Audit log append
  7. Module dispatch
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse


def _merge_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if not val:
                continue
            if not os.environ.get(key):
                os.environ[key] = val


def _load_profile_dotenv() -> None:
    """Load Hermes profile .env first, then emporia repo .env (setdefault semantics).

    When emporia lives under profiles/<name>/emporia, the repo-level .env must not
    shadow the profile — the old \"first .env wins\" walk stopped at emporia/.env.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").exists():
            _merge_dotenv(parent / ".env")
            break
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "emporia").is_dir():
            _merge_dotenv(parent / ".env")
            break


_load_profile_dotenv()

from emporia.engine.game_registry import ChallengeOwnershipError, GameRegistry
from emporia.engine import guardrails
from emporia.engine.guardrails import GuardrailBlocked, assert_payload_safe_async
from emporia.identity import (
    verify,
    verify_raw,
    get_relay_public_key_hex,
    sign_raw_relay,
    load_federation_peer_pubkeys,
    save_federation_peer_pubkeys,
    load_or_generate_relay_jwt_secret,
    build_relay_descriptor,
    federation_signable_bytes,
    normalize_peer_url,
    normalize_peer_pubkey_keys,
)
from emporia.module_sdk import (
    MODULE_REGISTRY,
    SessionAction,
    SessionState,
    get_interaction_module,
)
from emporia.session_audit import (
    get_public_log,
    log_private_event,
    log_public_receipt,
    open_dual_session,
    verify_chain,
)
from emporia import rooms as rooms_db
from emporia import entitlements

if TYPE_CHECKING:
    # Type-only — every real (runtime) reference to emporia.payments stays a
    # function-local import (L2, this file's own discipline): 75 test
    # @patch("emporia.payments.X") decorators only intercept because nothing
    # here imports these names at module scope. A TYPE_CHECKING import is
    # erased before runtime, so it can't affect that.
    from emporia.payments import PaymentRef

# Trigger module registration — import all bundled modules at startup
import emporia.modules.chess  # noqa: F401
import emporia.modules.code_review  # noqa: F401
import emporia.modules.research  # noqa: F401
import emporia.modules.service  # noqa: F401

# ============================================================================
# Configuration
# ============================================================================

BRAND = "Emporia"
RELAY_PORT = int(os.getenv("EMPORIA_RELAY_PORT", "8088"))
RELAY_BASE_URL = os.getenv("EMPORIA_RELAY_URL", f"http://localhost:{RELAY_PORT}")
RELAY_ID = os.getenv("EMPORIA_RELAY_ID", secrets.token_urlsafe(8))
# Set EMPORIA_REQUIRE_NOUS=1 to reject registrations without a verified Nous identity.
REQUIRE_NOUS = os.getenv("EMPORIA_REQUIRE_NOUS", "0").strip() == "1"
# Set EMPORIA_WRITE_REQUIRES_NOUS=1 to restrict key_only agents to read-only (GET) access.
WRITE_REQUIRES_NOUS = os.getenv("EMPORIA_WRITE_REQUIRES_NOUS", "0").strip() == "1"
# Set EMPORIA_REQUIRE_CHALLENGE=0 to allow registration without proof-of-key-possession
# (default mandatory — every first-party client already completes the challenge round-trip
# unconditionally, so this only weakens a direct REST caller's requirements; not recommended).
REQUIRE_CHALLENGE = os.getenv("EMPORIA_REQUIRE_CHALLENGE", "1").strip() == "1"
OPERATOR_FEE_BPS = int(os.getenv("OPERATOR_FEE_BPS", "250"))
MAX_TOTAL_SPEND_CENTS = int(os.getenv("EMPORIA_MAX_TOTAL_SPEND_CENTS", "0"))
TEMPO_ENABLED = os.getenv("EMPORIA_MPP_TEMPO_ENABLED", "0").strip() == "1"
LOG_DIR = Path(os.getenv("EMPORIA_LOG_DIR", "./.local/logs")).expanduser()
DATABASE_PATH = Path(os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia.sqlite3")).expanduser()

# Federated peers (comma-separated relay URLs; empty = standalone). Normalized
# (N13-2, R2) so these entries agree with the keys _pull_federated_listings/
# _pull_federated_lobby actually store under — see identity.normalize_peer_url().
FEDERATED_RELAYS: list[str] = [
    normalize_peer_url(u) for u in os.getenv("FEDERATED_RELAYS", "").split(",") if u.strip()
]

# Last federation sync outcome per peer, for the dashboard's Federation panel.
# {peer_url: {"ok": bool, "imported": int, "synced_at": iso8601}}
_FEDERATION_LAST_SYNC: dict[str, dict[str, Any]] = {}

# Same shape, for lobby-challenge gossip (N11-5) — kept separate from the listings
# sync record above since the two feeds are pulled and can fail independently.
_FEDERATION_LOBBY_LAST_SYNC: dict[str, dict[str, Any]] = {}

# Trust-on-first-use pin of each peer's Ed25519 public key, keyed by peer_url. The first
# successfully signature-verified pull from a peer pins its key; a later pull presenting a
# different key for the same peer_url is rejected rather than silently trusted (guards
# against a URL/DNS hijack swapping identities out from under an existing federation
# relationship). Persisted to disk (identity.save_federation_peer_pubkeys(), beside the
# relay's own key) so a restart doesn't reset trust-on-first-use back to
# trust-on-first-use-again — the pin is the entire point. Loaded lazily on first actual
# use rather than at module-import time: identity.KEY_DIR can be patched (e.g. by tests)
# after this module is imported, and a lazy load correctly picks up whatever KEY_DIR is
# live when federation code first runs, not whatever it was at import time.
_FEDERATION_PEER_PUBKEYS: dict[str, str] = {}
_FEDERATION_PEER_PUBKEYS_LOADED = False


def _ensure_federation_pins_loaded() -> None:
    global _FEDERATION_PEER_PUBKEYS_LOADED
    if not _FEDERATION_PEER_PUBKEYS_LOADED:
        # N13-2 (R2): migrate any pin written under a pre-fix, un-normalized key (e.g.
        # a trailing slash) so it's reachable under the same key _pull_federated_*
        # now always look up.
        migrated, changed = normalize_peer_pubkey_keys(load_federation_peer_pubkeys())
        _FEDERATION_PEER_PUBKEYS.update(migrated)
        if changed:
            save_federation_peer_pubkeys(_FEDERATION_PEER_PUBKEYS)
        _FEDERATION_PEER_PUBKEYS_LOADED = True


def _stripe_profile_id() -> str:
    return (os.getenv("STRIPE_PROFILE_ID", "") or "").strip()


def _stripe_profile_ready() -> bool:
    profile_id = _stripe_profile_id()
    return bool(os.getenv("STRIPE_SECRET_KEY")) and (
        profile_id.startswith("profile_") or profile_id.startswith("profile_test_")
    )


def _stripe_mpp_admin_notice() -> str | None:
    from emporia.stripe_profile_discovery import stripe_mpp_admin_notice

    sk = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    return stripe_mpp_admin_notice(sk, profile_ready=_stripe_profile_ready())

def _docker_gateway_ip() -> str | None:
    """Return this container's default-route gateway IP, if any.

    In a Docker deployment, browser traffic to the dashboard is proxied in by
    the host (e.g. Hermes's gateway process) and arrives at the relay with
    the container's bridge gateway as the source IP — not 127.0.0.1. That
    traffic never left the Docker host, so it's the correct "local" trust
    boundary for this container, same as 127.0.0.1 is for a bare-metal
    deployment. Determined from /proc/net/route (not guessed/hardcoded) so it
    tracks whatever bridge network this container actually has.
    """
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000":  # default route
                    import socket
                    import struct
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except Exception:
        pass
    return None


_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Rate-limit exemption is a much lower-stakes bypass than identity trust below — "don't
# throttle this IP" grants nothing an attacker can act on. Bare loopback is always exempt;
# an operator's own reverse-proxy/LB IPs (EMPORIA_TRUSTED_LOCAL_HOSTS) are an explicit opt-in
# independent of the header-auth flag below.
_RATE_LIMIT_EXEMPT_HOSTS: set[str] = set(_LOCALHOST_HOSTS)
_RATE_LIMIT_EXEMPT_HOSTS.update(
    h.strip() for h in os.getenv("EMPORIA_TRUSTED_LOCAL_HOSTS", "").split(",") if h.strip()
)

# Header-based identity trust (X-Emporia-Agent-Id on REST, ?agent_id= on WS) must never be
# inferred from a network address alone. Two ways that bites:
#   1. Docker's default userland-proxy rewrites a *published* port's external traffic to
#      arrive at the container with the bridge gateway as its source IP — indistinguishable
#      from genuinely co-located traffic.
#   2. A same-host TLS reverse proxy (nginx/Caddy in front of the relay on one VPS, the
#      standard production topology) makes every internet request arrive from 127.0.0.1 —
#      indistinguishable from a co-located dashboard/agent.
# So this set requires an explicit operator opt-in and, with the flag off, is EMPTY —
# header auth is fully dead regardless of network topology, including bare loopback. The
# single-operator co-located deployment (dashboard's agent + relay on one host, not exposed
# to untrusted traffic) sets the flag deliberately; a public relay never should.
_TRUST_LOCAL_HEADER_AUTH = os.getenv("EMPORIA_TRUST_LOCAL_HEADER_AUTH", "0").strip() == "1"
_HEADER_AUTH_TRUSTED_HOSTS: set[str] = set()
if _TRUST_LOCAL_HEADER_AUTH:
    _HEADER_AUTH_TRUSTED_HOSTS.update(_LOCALHOST_HOSTS)
    _docker_gw = _docker_gateway_ip()
    if _docker_gw:
        _HEADER_AUTH_TRUSTED_HOSTS.add(_docker_gw)
        _RATE_LIMIT_EXEMPT_HOSTS.add(_docker_gw)
    _HEADER_AUTH_TRUSTED_HOSTS.update(
        h.strip() for h in os.getenv("EMPORIA_TRUSTED_LOCAL_HOSTS", "").split(",") if h.strip()
    )

# ─── Dashboard JWT (HMAC-SHA256, stdlib only) ────────────────────────────────
import base64
import hashlib
import hmac as _hmac

_JWT_TTL_SECONDS = 3600                  # 1-hour sessions
_CHALLENGE_STORE: dict[str, tuple[str, float]] = {}   # challenge_id → (nonce, expires_at)
_PENDING_TOKENS: dict[str, str] = {}                  # challenge_id → jwt (consumed on poll)
_CHALLENGE_TTL = 300.0  # 5-minute window to complete sign


def _jwt_encode(payload: dict) -> str:
    import json as _json
    body = base64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = _hmac.new(load_or_generate_relay_jwt_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _jwt_decode(token: str) -> dict | None:
    import json as _json
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = _hmac.new(load_or_generate_relay_jwt_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expected):
        return None
    try:
        payload = _json.loads(base64.urlsafe_b64decode(body + "=="))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def _is_header_auth_trusted(request: Request) -> bool:
    """True when the TCP connection is from a peer allowed to claim identity via header.

    Always reads request.client.host (the real TCP peer), never forwarded headers — so
    X-Forwarded-For: 127.0.0.1 from a remote client is ignored. Checks
    _HEADER_AUTH_TRUSTED_HOSTS, not bare loopback: with EMPORIA_TRUST_LOCAL_HEADER_AUTH off
    (the default), this set is empty and every peer — including 127.0.0.1 — is untrusted for
    header auth. See that flag's definition for why bare loopback isn't safe to trust
    unconditionally (a same-host reverse proxy makes every request arrive from it).
    """
    host = request.client.host if request.client else ""
    return host in _HEADER_AUTH_TRUSTED_HOSTS


def _dashboard_agent_id(request: Request) -> str | None:
    """
    Resolve the requesting agent for dashboard REST calls.

    Trust model:
      - trusted host + X-Emporia-Agent-Id header → trusted (co-located, EMPORIA_TRUST_LOCAL_HEADER_AUTH=1)
      - Authorization: Bearer <jwt>                → verified via HMAC-SHA256 JWT
      - anything else                              → None (unauthenticated)
    """
    if _is_header_auth_trusted(request):
        header_id = request.headers.get("x-emporia-agent-id")
        if header_id:
            return header_id
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        payload = _jwt_decode(auth[7:])
        if payload and payload.get("relay_id") == RELAY_ID:
            return payload.get("agent_id") or None
    return None


def _ws_agent_id(ws: WebSocket) -> str | None:
    """
    Resolve the connecting agent's identity for a WebSocket upgrade.

    Same trust tiers as _dashboard_agent_id(), adapted for WS: browsers cannot set
    custom headers (Authorization, X-Emporia-Agent-Id) on a WS handshake, so both
    tiers are carried as query params instead:
      - trusted host + ?agent_id=<id>  → trusted (co-located, EMPORIA_TRUST_LOCAL_HEADER_AUTH=1)
      - ?token=<jwt>                    → verified via the same HMAC-SHA256 JWT used for
                                           dashboard REST auth
      - neither                         → None (unauthenticated)
    """
    host = ws.client.host if ws.client else ""
    if host in _HEADER_AUTH_TRUSTED_HOSTS:
        qp_agent = ws.query_params.get("agent_id")
        if qp_agent:
            return qp_agent
    token = ws.query_params.get("token")
    if token:
        payload = _jwt_decode(token)
        if payload and payload.get("relay_id") == RELAY_ID:
            return payload.get("agent_id") or None
    return None


def _relay_operator_id() -> str:
    """The configured relay operator's agent_id, or "" if none is configured (N6
    review O2: every "is this caller the operator?" check in this file re-read
    os.getenv("EMPORIA_AGENT_ID", "") independently — a dozen call sites as of this
    pass, up from the two the finding originally named, since N6-2's new redaction
    helpers each added their own. One canonical definition instead."""
    return os.getenv("EMPORIA_AGENT_ID", "")


def _require_caller_is(agent_id: str, request: Request) -> None:
    """Raise 403 unless the authenticated caller is `agent_id` or the relay operator.

    Guards agent-scoped writes (inbox, subscriptions) whose path carries a bare
    agent_id with no other proof the caller actually controls that identity.
    """
    caller = _dashboard_agent_id(request)
    owner = _relay_operator_id()
    if caller != agent_id and not (owner and caller == owner):
        raise HTTPException(403, f"Not authorized to act as '{agent_id}'")


def _require_caller_unless_mpp_payment(agent_id: str, request: Request) -> bool:
    """Like _require_caller_is, but skipped when the caller is presenting an MPP payment
    credential (Authorization: Payment <token>) rather than a Bearer JWT — the two share
    the Authorization header and can't both be sent, so caller identity can't be resolved
    from a payment-token request at all. In that case payment resource-binding is the only
    authorization (see _assert_payment_intent_matches), same as before this check existed.
    Tightening the payment path itself (binding a payment credential to a payer identity,
    not just a resource) is payment-integrity work, deferred to the x402 re-platform — see
    the plan's P1 note ("payment is resource-bound, not identity-bound").

    Returns whether `agent_id` was actually proven (N12-5, R5): callers use this to decide
    whether it's safe to apply _enforce_agent_rate_limit(agent_id) — before this, all three
    call sites threw the per-agent write budget at whatever agent_id the request body named,
    even on the MPP-payment branch where that name was never verified, letting a payment-
    token holder burn any registered agent's budget just by naming them.
    """
    from emporia.payments import extract_mpp_token
    if extract_mpp_token(request.headers.get("Authorization")):
        return False
    _require_caller_is(agent_id, request)
    return True


_MAX_LIMIT = 200


def _clamp_limit(limit: int, max_limit: int = _MAX_LIMIT) -> int:
    """Bound a caller-supplied `limit` query param to [1, max_limit] (N3-6). Several
    LIMIT-bearing endpoints already did this inline as `min(limit, 200)`; the N3 review
    (O2) named 8 more that took `limit: int` with no cap at all, and applying this
    uniformly turned up 4 further uncapped sites the review's own count had missed
    (GET /listings, GET /rooms, GET /rooms/{id}/messages, GET /dm/{id}/messages) — an
    uncapped limit is both a DoS lever (unbounded result set / row-scan) and, on
    GET /agents specifically, the mechanism that turned an N+1 per-row query loop into
    an unbounded one (O1). Also floors at 1 so a caller can't pass 0 or a negative number
    to `LIMIT` (SQLite treats a negative LIMIT as "no limit," which would silently defeat
    the whole point of this clamp).
    """
    return min(max(limit, 1), max_limit)


def _fetch_page(conn: Any, sql: str, params: tuple, limit: int) -> tuple[list, bool]:
    """Run `sql` (a SELECT whose final `?` placeholder is the LIMIT) requesting
    `limit + 1` rows, then slice back down to `limit` — returns (rows, has_more).
    One query, no COUNT(*) scan.

    N6 review R2: every list endpoint in this file returns `"count": len(rows)`,
    which is the already-capped page size — it can never exceed `limit`, so it can
    never actually signal truncation (N5-5 added `count` to GET /events with that
    stated goal; it doesn't achieve it). `has_more` is the real signal.
    """
    rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    has_more = len(rows) > limit
    return rows[:limit], has_more


def _session_visibility(session: dict[str, Any]) -> str:
    """Resolve whether a session's content ("public" or "participants"-only).

    Room-inheritance wins when a room links to this session (rooms.py's
    get_room_for_session): public room → public, private room → participants-only,
    regardless of the module's own default. No linked room → the module's
    DEFAULT_VISIBILITY (module_sdk.py) — chess defaults "public" for the
    spectator/replay use case; research/service/code-review default "participants"
    since the session's action payload and artifacts are the paid deliverable.
    """
    room = rooms_db.get_room_for_session(session["session_id"], db_path=DATABASE_PATH)
    if room:
        return "public" if room.room_type == "public" else "participants"
    try:
        module = get_interaction_module(session["module_type"])
    except ValueError:
        return "participants"  # unknown module type — fail closed, not open
    return module.DEFAULT_VISIBILITY


def _session_viewer_allowed(session: dict[str, Any], caller: str | None) -> bool:
    """True if `caller` may read this session's actions/state/audit — see
    _session_visibility(). The relay operator can always read any session."""
    if _session_visibility(session) == "public":
        return True
    if caller and caller in session.get("participants", []):
        return True
    owner = _relay_operator_id()
    return bool(owner and caller == owner)


# Anti-cheat configuration
MIN_RATIONALE_CHARS = int(os.getenv("MIN_RATIONALE_CHARS", "15"))
_DEFAULT_FINGERPRINTS = "stockfish,engine_move,eval_score:"
BOT_FINGERPRINTS: list[str] = [
    f.strip() for f in os.getenv("BOT_FINGERPRINTS", _DEFAULT_FINGERPRINTS).split(",") if f.strip()
]

# WebSocket connections
ws_connections: dict[str, set[WebSocket]] = defaultdict(set)

DB_LOCK = threading.RLock()


# ============================================================================
# Database
# ============================================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS authorized_agents (
                agent_id TEXT PRIMARY KEY,
                public_key_hex TEXT NOT NULL,
                display_name TEXT,
                registered_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                stripe_account_id TEXT,
                nous_user_id TEXT,
                trust_level TEXT NOT NULL DEFAULT 'key_only',
                identity_providers_json TEXT NOT NULL DEFAULT '[]',
                x402_pay_to_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                module_type TEXT NOT NULL,
                config_json TEXT NOT NULL,
                payment_rules_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                current_agent TEXT NOT NULL,
                step_number INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting',
                participants_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS participants (
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                stake_paid INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session_id, agent_id)
            );

            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                session_id TEXT,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                signature TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS listings (
                listing_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                listing_type TEXT NOT NULL DEFAULT 'service',
                agent_id TEXT NOT NULL,
                payment_mode TEXT NOT NULL DEFAULT 'free',
                price_usd TEXT NOT NULL DEFAULT '0',
                module_type TEXT,
                origin_relay TEXT,
                expires_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                module_type TEXT NOT NULL,
                organizer_id TEXT NOT NULL,
                payment_mode TEXT NOT NULL DEFAULT 'free',
                entry_fee_usd TEXT NOT NULL DEFAULT '0',
                status TEXT NOT NULL DEFAULT 'open',
                bracket_json TEXT NOT NULL DEFAULT '{}',
                participants_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                session_id TEXT,
                room_id TEXT,
                agent_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'usd',
                payment_intent_id TEXT NOT NULL,
                payment_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                rail TEXT NOT NULL DEFAULT 'stripe'
            );

            CREATE TABLE IF NOT EXISTS settlements (
                settlement_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                winner_id TEXT,
                total_stake_cents INTEGER NOT NULL DEFAULT 0,
                platform_fee_cents INTEGER NOT NULL DEFAULT 0,
                winner_payout_cents INTEGER NOT NULL DEFAULT 0,
                platform_fee_bps INTEGER NOT NULL DEFAULT 250,
                payment_intent_ids TEXT NOT NULL DEFAULT '[]',
                transfer_id TEXT,
                transfer_status TEXT NOT NULL DEFAULT 'pending_connect',
                status TEXT NOT NULL DEFAULT 'settled',
                created_at TEXT NOT NULL,
                payer_id TEXT,
                rail TEXT NOT NULL DEFAULT 'stripe'
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_sessions_module ON sessions(module_type);
            CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent);
            CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_agent);
            CREATE INDEX IF NOT EXISTS idx_listings_type ON listings(listing_type);
            CREATE INDEX IF NOT EXISTS idx_listings_agent ON listings(agent_id);
            CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
            CREATE INDEX IF NOT EXISTS idx_payments_session ON payments(session_id);
            CREATE INDEX IF NOT EXISTS idx_settlements_session ON settlements(session_id);
            CREATE INDEX IF NOT EXISTS idx_settlements_winner ON settlements(winner_id);
            CREATE INDEX IF NOT EXISTS idx_settlements_payer ON settlements(payer_id);
            -- N9 review O1: all three list_settlements branches ORDER BY created_at
            -- DESC; without this, SQLite scans the whole table and sorts the matching
            -- set in a temp b-tree before LIMIT applies (verified via EXPLAIN QUERY
            -- PLAN — see the commit message for before/after).
            CREATE INDEX IF NOT EXISTS idx_settlements_created ON settlements(created_at);
            CREATE INDEX IF NOT EXISTS idx_participants_agent ON participants(agent_id);
            CREATE INDEX IF NOT EXISTS idx_payments_agent ON payments(agent_id);
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_inbox (
                inbox_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbox_agent ON agent_inbox(agent_id, is_read)"
        )
        # Agoras schema (topic-based agent forums: public/private/restricted)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agora_topics (
                topic_id TEXT PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'public',
                gate_type TEXT NOT NULL DEFAULT 'open',
                entry_fee_cents INTEGER NOT NULL DEFAULT 0,
                creator_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                post_count INTEGER NOT NULL DEFAULT 0,
                subscriber_count INTEGER NOT NULL DEFAULT 0,
                flair_options TEXT NOT NULL DEFAULT '[]',
                access_duration_days INTEGER
            );
            CREATE TABLE IF NOT EXISTS agora_members (
                topic_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'subscriber',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (topic_id, agent_id)
            );
            CREATE TABLE IF NOT EXISTS agora_invites (
                topic_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                invited_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (topic_id, agent_id)
            );
            CREATE TABLE IF NOT EXISTS agora_posts (
                post_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                post_type TEXT NOT NULL DEFAULT 'text',
                flair TEXT,
                vote_score INTEGER NOT NULL DEFAULT 0,
                comment_count INTEGER NOT NULL DEFAULT 0,
                is_pinned INTEGER NOT NULL DEFAULT 0,
                is_locked INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agora_comments (
                comment_id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL,
                parent_comment_id TEXT,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL,
                vote_score INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agora_votes (
                voter_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_type TEXT NOT NULL,
                value INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (voter_id, target_id, target_type)
            );
            CREATE INDEX IF NOT EXISTS idx_agora_posts_topic ON agora_posts(topic_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agora_posts_author ON agora_posts(author_id);
            CREATE INDEX IF NOT EXISTS idx_agora_comments_post ON agora_comments(post_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agora_members_agent ON agora_members(agent_id);
            CREATE INDEX IF NOT EXISTS idx_agora_topics_visibility ON agora_topics(visibility, created_at);
            CREATE INDEX IF NOT EXISTS idx_inbox_created ON agent_inbox(is_read, created_at);
        """)
        # DM schema (direct agent-to-agent threads)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dm_threads (
                thread_id TEXT PRIMARY KEY,
                agent_a TEXT NOT NULL,
                agent_b TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_message_at TEXT NOT NULL,
                UNIQUE(agent_a, agent_b)
            );
            CREATE TABLE IF NOT EXISTS dm_messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT NOT NULL DEFAULT 'chat',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dm_threads_a ON dm_threads(agent_a, last_message_at);
            CREATE INDEX IF NOT EXISTS idx_dm_threads_b ON dm_threads(agent_b, last_message_at);
            CREATE INDEX IF NOT EXISTS idx_dm_messages_thread ON dm_messages(thread_id, created_at);

            CREATE TABLE IF NOT EXISTS reg_challenges (
                challenge_id TEXT PRIMARY KEY,
                nonce TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                key TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)
        # Schema migrations for existing DBs (idempotent ADD COLUMN)
        _migrate_authorized_agents(conn)
        _migrate_payments(conn)
        _migrate_settlements(conn)
        _migrate_settlement_dedupe_key(conn)
        _migrate_settlement_refund_columns(conn)
        # Rename aroga_* → agora_* if old names still exist (typo fix)
        _migrate_agora_tables(conn)
        # Rooms schema (separate DDL to keep clean)
        rooms_db.init_rooms_schema(conn)
        _migrate_rooms(conn)
        # Access entitlements (P2-3): what a payment actually buys — see
        # entitlements.py's module docstring
        entitlements.init_entitlements_schema(conn)
        _assert_not_pre_p3_schema(conn)


def _assert_not_pre_p3_schema(conn: Any) -> None:
    """P3: `rail` (payments/settlements/access_entitlements) and the
    `gate_type='paid'` rename (P3-1, was 'stripe_payment') both landed with
    **no** `ALTER TABLE`/migration — by decision, since the operator is
    starting from a fresh DB for this pass (see the P3 plan's "Migrations"
    section). `CREATE TABLE IF NOT EXISTS` is a no-op against a table that
    already exists, so an old-schema DB wouldn't get `rail` added, and worse:
    a leftover `rooms.gate_type='stripe_payment'` row would silently fall
    through every `gate_type == "paid"` check this pass introduced —
    `rooms._access_active()` would take the *not-paid* branch, skip the
    entitlement check entirely, and the room would quietly become free. Fail
    loud on startup instead of serving that.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(settlements)").fetchall()}
    if "rail" not in cols:
        raise RuntimeError(
            "This database predates the P3 rail-seam pass (settlements.rail is "
            "missing). No migration is provided for it — delete the database "
            "file and reseed."
        )
    legacy_gate = conn.execute(
        "SELECT 1 FROM rooms WHERE gate_type = 'stripe_payment' LIMIT 1"
    ).fetchone()
    if legacy_gate:
        raise RuntimeError(
            "This database has a room with the legacy gate_type='stripe_payment' "
            "(renamed to 'paid' in P3). No migration is provided for it — delete "
            "the database file and reseed."
        )
    # P1b-2: same fresh-DB posture, same reason — authorized_agents.x402_pay_to_json
    # landed with no ALTER TABLE either.
    agent_cols = {r[1] for r in conn.execute("PRAGMA table_info(authorized_agents)").fetchall()}
    if "x402_pay_to_json" not in agent_cols:
        raise RuntimeError(
            "This database predates the P1b x402 pass (authorized_agents."
            "x402_pay_to_json is missing). No migration is provided for it — "
            "delete the database file and reseed."
        )


def _migrate_agora_tables(conn: Any) -> None:
    existing_tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    renames = [
        ("aroga_topics",   "agora_topics"),
        ("aroga_members",  "agora_members"),
        ("aroga_posts",    "agora_posts"),
        ("aroga_comments", "agora_comments"),
        ("aroga_votes",    "agora_votes"),
    ]
    for old, new in renames:
        if old in existing_tables and new not in existing_tables:
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    # Add gate_type / entry_fee_cents to agora_topics if missing
    topic_cols = {r[1] for r in conn.execute("PRAGMA table_info(agora_topics)").fetchall()}
    if "gate_type" not in topic_cols:
        conn.execute("ALTER TABLE agora_topics ADD COLUMN gate_type TEXT NOT NULL DEFAULT 'open'")
    if "entry_fee_cents" not in topic_cols:
        conn.execute("ALTER TABLE agora_topics ADD COLUMN entry_fee_cents INTEGER NOT NULL DEFAULT 0")
    # P2-3: how long one payment's access lasts — NULL (the correct default for
    # every pre-existing row) means no expiry, same meaning as for rooms
    # (_migrate_rooms). No backfill needed: NULL is already the right value.
    if "access_duration_days" not in topic_cols:
        conn.execute("ALTER TABLE agora_topics ADD COLUMN access_duration_days INTEGER")


def _migrate_rooms(conn: Any) -> None:
    """Add `access_duration_days` to `rooms` (P2-3) — how long one payment's
    access lasts. `rooms.py` has no migration mechanism of its own
    (`init_rooms_schema()` is a bare `CREATE TABLE IF NOT EXISTS`), so this lives
    here rather than there, called right after `rooms_db.init_rooms_schema(conn)`
    in `init_db()`. `NULL` (the default `ADD COLUMN` gives every pre-existing
    row) means no expiry — the correct value for every room created before this
    pass, none of which had any concept of an access window — so no backfill is
    needed, only the column."""
    room_cols = {r[1] for r in conn.execute("PRAGMA table_info(rooms)").fetchall()}
    if "access_duration_days" not in room_cols:
        conn.execute("ALTER TABLE rooms ADD COLUMN access_duration_days INTEGER")


def _migrate_payments(conn: Any) -> None:
    conn.execute(
        "DELETE FROM payments WHERE rowid NOT IN ("
        "SELECT MIN(rowid) FROM payments GROUP BY payment_intent_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_payment_intent_id "
        "ON payments(payment_intent_id)"
    )


def _migrate_settlements(conn: Any) -> None:
    """Add `payer_id` if missing (N7 review R1/R2) and backfill existing rows.

    A room or Agora settlement is one row per payer, written with exactly one
    PaymentIntent id (join_room/subscribe_agora_topic both call record_settlement
    with `"payment_intent_ids": [confirmed_pi]`); a session settlement is one
    aggregate row for the whole shared escrow, with zero or more PIs across every
    participant, and correctly has no single payer. Backfill only where both
    signals agree: the row has exactly one PI (never true for a multi-participant
    session escrow with more than one payer) AND its session_id doesn't match a
    row in `sessions` at all (ruling out the common case of a 1-payer, 2-party
    session too). Ambiguous or already-null-PI rows are left NULL.

    N8 review R3 (fixed the every-startup rescan) then N9 review R2 (fixed what
    that fix broke): a session's aggregate row legitimately keeps payer_id NULL
    forever, so a naive `WHERE payer_id IS NULL` backfill scan never empties out
    and would re-scan every session settlement ever written on every startup. N8
    stopped that by running the backfill only inside `if "payer_id" not in
    settlement_cols`, alongside the ADD COLUMN — but that conflated "has the
    column been added" with "has the backfill finished," and removed the only
    retry. Concretely: Python's sqlite3 module commits `ALTER TABLE` immediately,
    independent of the surrounding `with conn:` block, while the backfill's
    UPDATEs share one implicit transaction with everything after them — so a
    crash mid-backfill (verified empirically: kill the process between the ALTER
    and the loop's completion) leaves the column present but every legacy
    room/Agora row still `payer_id IS NULL`, permanently, since the next startup's
    guard sees the column and never re-enters. And unlike N8-4's docstring
    claimed, that is NOT a safe degradation: `add_participant()` is called only
    from session joins (`:3186`, `:3356`), so a room/Agora row's session_id never
    appears in `participants` — such a row satisfies neither arm of
    `_caller_owns_settlement_row` and becomes visible only to the relay operator,
    unreachable by its actual payer.

    Driven off `schema_migrations` instead: the backfill runs once — on whichever
    startup finds no `settlements_payer_id_backfill` row — and the completion
    marker is only inserted after the loop finishes, in the same implicit
    transaction as the UPDATEs it's marking complete. A crash before that marker
    commits rolls the whole batch back together (nothing partially applied), so
    the next startup retries cleanly instead of silently giving up.
    """
    settlement_cols = {r[1] for r in conn.execute("PRAGMA table_info(settlements)").fetchall()}
    if "payer_id" not in settlement_cols:
        conn.execute("ALTER TABLE settlements ADD COLUMN payer_id TEXT")

    backfill_key = "settlements_payer_id_backfill"
    already_done = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE key = ?", (backfill_key,)
    ).fetchone()
    if already_done:
        return

    rows = conn.execute(
        "SELECT settlement_id, session_id, payment_intent_ids FROM settlements "
        "WHERE payer_id IS NULL"
    ).fetchall()
    for row in rows:
        pi_ids = json.loads(row["payment_intent_ids"] or "[]")
        if len(pi_ids) != 1:
            continue
        is_session = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (row["session_id"],)
        ).fetchone()
        if is_session:
            continue
        payer = conn.execute(
            "SELECT agent_id FROM payments WHERE payment_intent_id = ?", (pi_ids[0],)
        ).fetchone()
        if payer:
            conn.execute(
                "UPDATE settlements SET payer_id = ? WHERE settlement_id = ?",
                (payer["agent_id"], row["settlement_id"]),
            )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (key, applied_at) VALUES (?, ?)",
        (backfill_key, datetime.now(UTC).isoformat()),
    )


def _migrate_settlement_dedupe_key(conn: Any) -> None:
    """Add `dedupe_key` and a UNIQUE index on it, and (re)compute it from
    `settlement_dedupe_key()` (P1r-1, R1) so record_settlement()'s
    `INSERT OR IGNORE` can actually collide on a replay — and *only* a replay.

    `session_id` alone isn't the real natural key — it's overloaded across three
    resource kinds (see `_settlement_resource_viewer_allowed()`'s docstring) and a
    session's own aggregate escrow row legitimately has zero-or-more payers, one
    row for the whole resource, not one per payer, with `payer_id` correctly
    NULL. P1a-2 keyed on `(session_id, payer_id)` alone, stored as `dedupe_key =
    session_id || ':' || COALESCE(payer_id, '')`; P1r-1 found that this collapses
    a payer's second genuine payment for the same resource (rejoin after a kick,
    resubscribe after an unsubscribe) into their first, so the key now also folds
    in a digest of `payment_intent_ids` — see `settlement_dedupe_key()`'s
    docstring for the full shape.

    A stored column (not a bare `UNIQUE(session_id, payer_id, ...)`) is still
    required: SQLite treats every NULL as distinct from every other NULL in a
    UNIQUE index, which would not dedupe session-escrow rows (all sharing
    `payer_id IS NULL`) at all — the exact case that matters most.

    **Marker-gated one-shot, not P1a-2's unconditional every-startup rewrite.**
    Every existing row's key was computed under the old 2-part shape and must be
    rewritten once under the new 3-part one — not just backfilled where NULL,
    since these rows already have a (stale) non-NULL value. Follows
    `_migrate_settlements()`'s own pattern for exactly the reason its docstring
    gives: an unconditional per-startup rewrite here would be harmless once
    settled, but there is no cheap "already correct" check short of recomputing
    every row's key and comparing, so gate it explicitly instead of relying on
    that coincidence. Order matters and mirrors the docstring's reasoning
    end-to-end: drop the old index (a value this migration is about to write may
    already exist as some *other* row's stale value, and the old index would
    reject that write) → recompute every row's key from its own
    payment_intent_ids/payer_id/session_id → delete genuine duplicates (rows that
    still collide after moving to the strictly finer key — recomputing can only
    split old groups apart, never merge them, so this should find nothing; kept
    as a backstop rather than assumed) → recreate the index → set the marker.
    `ALTER TABLE` commits immediately in Python's sqlite3 regardless of the
    surrounding `with conn:` (the N9-R2 lesson); the marker row shares its
    transaction with the rewrite/delete so a crash before it commits retries the
    whole batch next startup instead of leaving a mix of old- and new-shape keys.

    Pre-existing genuine duplicates (both rows keying identically even under the
    finer key — a true replay of the same payment, double-inserted by the bug
    P1a-2 fixed) are deleted, keeping the earliest by rowid, logged via `print()`
    first so an unexpected hit is auditable.

    P2-1 (R3): only the rewrite/collapse above is marker-gated — the
    `CREATE UNIQUE INDEX` itself runs on *every* call, gated branch or not
    (`IF NOT EXISTS` makes the already-present case free). P1r-1 put the index
    creation inside the gated block, so once the marker was set the index was
    never asserted again; losing it after that point (a schema rebuild, manual
    `DROP INDEX`, any future migration that rebuilds this table — SQLite's
    standard ALTER-by-rebuild recipe drops every index) left it gone permanently,
    and every `record_settlement()` after that failed with `OperationalError`
    since `ON CONFLICT(dedupe_key)` had nothing to conflict on.
    """
    from emporia.payments import settlement_dedupe_key

    cols = {r[1] for r in conn.execute("PRAGMA table_info(settlements)").fetchall()}
    if "dedupe_key" not in cols:
        conn.execute("ALTER TABLE settlements ADD COLUMN dedupe_key TEXT")

    marker_key = "settlements_dedupe_key_payment_scoped"
    already_done = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE key = ?", (marker_key,)
    ).fetchone()
    if already_done:
        # P2-1 (R3): asserted unconditionally, every startup — mirrors P1a-2's
        # original every-startup CREATE INDEX. Only the one-shot rewrite/collapse
        # below is marker-gated; the index itself must never depend on the marker,
        # or a rebuild/manual DROP INDEX after the marker is set (SQLite's
        # standard ALTER-by-rebuild recipe drops every index) permanently starves
        # record_settlement()'s ON CONFLICT(dedupe_key) of anything to conflict
        # on — proved live: drop the index post-marker, restart, every settlement
        # insert after that dies with "ON CONFLICT clause does not match any
        # PRIMARY KEY or UNIQUE constraint".
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_dedupe_key ON settlements(dedupe_key)"
        )
        return

    conn.execute("DROP INDEX IF EXISTS idx_settlements_dedupe_key")

    rows = conn.execute(
        "SELECT settlement_id, session_id, payer_id, payment_intent_ids FROM settlements"
    ).fetchall()
    for row in rows:
        pi_ids = json.loads(row["payment_intent_ids"] or "[]")
        new_key = settlement_dedupe_key(row["session_id"], row["payer_id"], pi_ids)
        conn.execute(
            "UPDATE settlements SET dedupe_key = ? WHERE settlement_id = ?",
            (new_key, row["settlement_id"]),
        )

    _collapse_duplicate_settlements_by_dedupe_key(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_dedupe_key ON settlements(dedupe_key)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (key, applied_at) VALUES (?, ?)",
        (marker_key, datetime.now(UTC).isoformat()),
    )


def _collapse_duplicate_settlements_by_dedupe_key(conn: Any) -> None:
    """Delete every settlements row that shares its `dedupe_key` with an earlier
    one, keeping the earliest by rowid. Split out of
    `_migrate_settlement_dedupe_key()` so this specific piece — a true replay of
    the same payment, double-inserted by the bug P1a-2 fixed — is independently
    testable, including the guard below in isolation from the recompute loop
    that (today) always precedes it.

    P1r-4 (R4): guarded on `dedupe_key IS NOT NULL` at both the scan and the
    delete. `GROUP BY` folds every NULL into a single group — unlike `UNIQUE`,
    which treats each NULL as distinct — so an ungated `GROUP BY dedupe_key`
    would treat any NULL-key rows as one duplicate group and delete all but one
    of them. `_migrate_settlement_dedupe_key()`'s recompute loop gives every row
    a non-NULL key before calling this, so the guard is unreachable via that
    caller today; kept because these two NULL semantics disagreeing is exactly
    the kind of mismatch that has bitten this table before (N8-R3/N9-R2), and
    because this function may gain other callers.
    """
    dupes = conn.execute(
        "SELECT dedupe_key, COUNT(*) c, GROUP_CONCAT(settlement_id) ids "
        "FROM settlements WHERE dedupe_key IS NOT NULL GROUP BY dedupe_key HAVING c > 1"
    ).fetchall()
    for row in dupes:
        print(
            f"[migrate] settlements: dedupe_key {row['dedupe_key']!r} has {row['c']} "
            f"duplicate rows ({row['ids']}) — keeping the earliest, removing the rest"
        )
    conn.execute(
        "DELETE FROM settlements WHERE dedupe_key IS NOT NULL AND rowid NOT IN "
        "(SELECT MIN(rowid) FROM settlements WHERE dedupe_key IS NOT NULL GROUP BY dedupe_key)"
    )


def _migrate_settlement_refund_columns(conn: Any) -> None:
    """Add `refund_id`/`reversal_id` to settlements (P2r-3, R3) — P2-7's
    `_refund_kicked_room_payer()` called Stripe to reverse a transfer and
    refund a charge but recorded neither outcome: `transfer_status` stayed
    `'transferred'`, `payments.status` stayed `'confirmed'`, and the Stripe
    refund/reversal ids weren't stored anywhere — money moved, no record,
    the exact class of bug P1a-2 fixed for settlements in the first place,
    reintroduced on the refund side by the pass that added refunds.

    NULL on every row (existing or freshly inserted) means "no refund
    happened" — the correct default, no backfill needed — so no marker gate,
    same as `access_duration_days` (P2-3).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(settlements)").fetchall()}
    if "refund_id" not in cols:
        conn.execute("ALTER TABLE settlements ADD COLUMN refund_id TEXT")
    if "reversal_id" not in cols:
        conn.execute("ALTER TABLE settlements ADD COLUMN reversal_id TEXT")


def _migrate_authorized_agents(conn: Any) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(authorized_agents)").fetchall()
    }
    migrations = [
        ("nous_user_id", "TEXT"),
        ("trust_level", "TEXT NOT NULL DEFAULT 'key_only'"),
        ("identity_providers_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for col, defn in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE authorized_agents ADD COLUMN {col} {defn}")
    # Non-unique index — one Nous user may operate multiple agent personas.
    # Drop the old unique variant if it exists (created by an earlier migration).
    conn.execute("DROP INDEX IF EXISTS idx_agents_nous_user_id")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agents_nous_user_id "
        "ON authorized_agents(nous_user_id) WHERE nous_user_id IS NOT NULL"
    )


# ============================================================================
# Payment helpers
# ============================================================================

def _payment_metadata_matches(
    metadata: dict[str, Any],
    *,
    resource_type: str,
    resource_id: str,
) -> bool:
    if metadata.get("resource_type") == resource_type and metadata.get("resource_id") == resource_id:
        return True
    legacy_key = {
        "session": "session_id",
        "room": "room_id",
        "agora": "topic_id",
    }.get(resource_type)
    if legacy_key and metadata.get(legacy_key) == resource_id:
        return True
    service_type = metadata.get("service_type")
    if resource_type == "room" and metadata.get("session_id") == resource_id:
        return service_type == "emporia:room_entry"
    if resource_type == "agora" and metadata.get("session_id") == resource_id:
        return service_type == "emporia:agora_subscribe"
    return False


def _assert_payment_intent_matches(
    payment: dict[str, Any],
    *,
    amount_cents: int,
    currency: str,
    resource_type: str,
    resource_id: str,
    expected_capture_method: str,
    allowed_statuses: tuple[str, ...],
    expected_payer_agent_id: str | None = None,
) -> None:
    """`expected_payer_agent_id` (P1a-1, R1): a PaymentIntent used to be verified as
    bound to a *resource* but never to a *payer* — any registered agent presenting a
    valid, correctly-bound PI id passed every check here, including one they didn't
    pay for. Both PI-minting paths (`create_stake_intent`, `confirm_spt`) already stamp
    `metadata[buyer_agent_id]`; this was simply never read. Only
    `_verify_and_confirm_payment()`'s direct-`payment_intent_id` branch passes this —
    the caller is authenticated there, so the check is real. The SPT branch mints the
    PI from the caller's own claimed `agent_id` in the same call, so comparing them
    would be circular; deliberately left `None` (skipped) on that branch. A PI with no
    `buyer_agent_id` at all is rejected outright when a payer is expected — Emporia
    stamps every PI it accepts, so "absent" means "not ours," not "trust it anyway."
    """
    status = payment.get("status")
    if status not in allowed_statuses:
        raise HTTPException(402, f"Payment not confirmed (status={status}).")
    if int(payment.get("amount") or 0) != amount_cents:
        raise HTTPException(
            402,
            f"Payment amount mismatch: expected {amount_cents}, got {payment.get('amount')}",
        )
    actual_currency = (payment.get("currency") or "usd").lower()
    if actual_currency != currency.lower():
        raise HTTPException(
            402,
            f"Payment currency mismatch: expected {currency.lower()}, got {actual_currency}",
        )
    actual_capture = (payment.get("capture_method") or "automatic").lower()
    if actual_capture != expected_capture_method:
        raise HTTPException(
            402,
            f"Payment capture_method mismatch: expected {expected_capture_method}, got {actual_capture}",
        )
    metadata = payment.get("metadata") or {}
    if not _payment_metadata_matches(
        metadata,
        resource_type=resource_type,
        resource_id=resource_id,
    ):
        raise HTTPException(402, "PaymentIntent is not bound to this resource")
    if expected_payer_agent_id is not None:
        actual_payer = metadata.get("buyer_agent_id")
        if actual_payer != expected_payer_agent_id:
            raise HTTPException(402, "PaymentIntent is not bound to this payer")


def _x402_network_family(network: str) -> str | None:
    """CAIP-2 network id -> the wallet key space it needs (P1b-2's
    network_family). Unrecognized network id namespaces resolve to None so a
    misconfigured EMPORIA_X402_NETWORKS entry is silently excluded rather
    than raising mid-request."""
    if network.startswith("eip155:"):
        return "evm"
    if network.startswith("solana:"):
        return "svm"
    return None


def _x402_pay_to_by_network(creator_id: str | None) -> dict[str, str]:
    """Resolve which configured x402 networks this creator can actually
    receive on. A network whose family the creator has no wallet for is
    silently excluded — same no-auto-provision posture as P1b-2 (there's no
    wallet to invent on the creator's behalf).

    P1brr-3: reads the creator's wallets once via `get_x402_pay_to_all()`
    rather than calling `get_x402_pay_to()` per configured network — this
    runs on every 402 challenge and, since P1br-1 made `_x402_offer()` the
    credential gate, on every x402 payment attempt too, so N configured
    networks used to mean N connections for one single-row lookup."""
    if not creator_id:
        return {}
    from emporia.payments import _x402_networks

    wallets = AGENT_REGISTRY.get_x402_pay_to_all(creator_id)
    result: dict[str, str] = {}
    for network in _x402_networks():
        family = _x402_network_family(network)
        if not family:
            continue
        pay_to = wallets.get(family)
        if pay_to:
            result[network] = pay_to
    return result


async def _verify_x402_payment(
    x402_payload: dict[str, Any],
    *,
    creator_id: str | None,
    amount_cents: int,
    currency: str,
    challenge_resource: str,
) -> "PaymentRef":
    """The x402 analogue of `_verify_and_confirm_payment()`'s SPT/direct-PI
    branches — verify a client-supplied x402 credential against what this
    relay actually requires for this resource, then settle it. Raises
    HTTPException(402) on any binding/verification/settlement failure, the
    same posture as the Stripe branches it sits beside.

    The resource-binding assertion runs BEFORE any facilitator call — a
    deliberate refinement of the plan's original verify-then-assert
    ordering, made during implementation: asserting first is strictly
    cheaper (no network round-trip on a doomed request) and strictly safer
    — it never sends a `paymentRequirements` object to the facilitator that
    this relay didn't itself construct, so a payer who edits
    `accepted.payTo` to their own address is rejected locally rather than
    reaching the facilitator with a claim the facilitator has no way to
    know is wrong (verifying a signature's validity against a requirement
    says nothing about whether that requirement is what *this resource*
    actually charges).

    Known gap, recorded rather than silently accepted (SECURITY.md): unlike
    the Stripe branches, there is no pre-created object (a PaymentIntent) to
    safely re-verify against on retry. If the relay crashes after
    x402_settle_payment() succeeds but before record_payment() persists the
    transaction, a client retry re-signs a fresh authorization (the original
    nonce is now spent) and this function has no way to detect that the
    resource was already paid for — record_payment()'s idempotency only
    catches a replay of the *same* transaction hash, not a second genuinely
    valid settlement for the same purchase. x402 has no Idempotency-Key
    equivalent in its /settle contract to close this with.
    """
    from emporia.payments import (
        RAIL_X402, PaymentRef, x402_verify_payment, x402_settle_payment,
        x402_requirement, _x402_asset_for,
    )

    # P1br-1: unreachable via this function's one real call site as of that
    # pass — `_verify_and_confirm_payment()` only reaches here when
    # `_x402_offer()` (== `build_x402_challenge()`) is truthy, and that
    # function already refuses non-"usd" currency before returning anything.
    # Kept as defense in depth: this function's contract shouldn't silently
    # depend on a caller having already checked this.
    if currency != "usd":
        raise HTTPException(402, "x402 only supports currency=usd on this relay")

    accepted = x402_payload.get("accepted") or {}
    network = accepted.get("network")
    family = _x402_network_family(network) if network else None
    pay_to = (
        AGENT_REGISTRY.get_x402_pay_to(creator_id, family)
        if creator_id and family else None
    )
    asset = _x402_asset_for(network) if network else None
    # P1br-2: same x402_requirement() build_x402_challenge() uses to build
    # what this relay OFFERS — a single definition, so the two can never
    # compute two different `scheme`/`maxTimeoutSeconds` for the same
    # request. Tolerant of None network/asset/payTo (a missing or
    # unresolvable field) so every mismatch is listed at once below, not
    # just the first one that would otherwise raise.
    expected = x402_requirement(network, amount_cents, asset, pay_to)
    mismatched = [
        field for field, want in expected.items()
        if want is None or accepted.get(field) != want
    ]
    if mismatched:
        raise HTTPException(
            402,
            f"x402 payment requirement mismatch on {mismatched} for "
            f"resource {challenge_resource!r}",
        )

    try:
        verify_result = await x402_verify_payment(x402_payload, expected)
        if not verify_result.get("isValid"):
            raise HTTPException(
                402, f"x402 payment invalid: {verify_result.get('invalidReason', 'unknown')}"
            )
        settle_result = await x402_settle_payment(x402_payload, expected)
        if not settle_result.get("success"):
            raise HTTPException(
                402, f"x402 settlement failed: {settle_result.get('errorReason', 'unknown')}"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(402, f"x402 verification failed: {e}")

    transaction = settle_result.get("transaction")
    if not transaction:
        raise HTTPException(402, "x402 settlement returned no transaction id")
    return PaymentRef(RAIL_X402, transaction)


async def _verify_and_confirm_payment(
    request: Request,
    *,
    agent_id: str,
    identity_verified: bool,
    amount_cents: int,
    currency: str,
    resource_type: str,
    resource_id: str,
    service_type: str,
    capture_method: str,
    allowed_statuses: tuple[str, ...],
    req_payment_intent_id: str | None,
    challenge_message: str,
    challenge_resource: str,
    creator_id: str | None = None,
) -> "PaymentRef | JSONResponse":
    """Verify a payment credential (SPT via Authorization header, or a client-supplied
    PaymentIntent id) against the expected amount/currency/capture-method/resource binding,
    then return a `PaymentRef` — the confirmed reference, tagged with the rail it was
    verified on (P3, item 3). This is the one funnel that mints a `PaymentRef`; every
    downstream persistence point (record_payment, entitlements.grant, record_settlement)
    gets its `rail` from here rather than assuming one.

    If no credential was supplied, returns a ready-to-return 402 MPP challenge JSONResponse
    instead of raising — callers must check `isinstance(result, JSONResponse)` and return it
    directly (a 402 challenge carries X-MPP-* headers that HTTPException's flat detail body
    doesn't preserve, and each call site's challenge body/resource string legitimately
    differs). Raises HTTPException(402) on any verification/validation failure — this was
    already the uniform shape across all three call sites before this was extracted.

    Shared by /sessions/{id}/join, /rooms/{id}/join, and /agoras/topics/{slug}/subscribe —
    previously ~40 lines of identical verify/confirm/validate logic copy-pasted at each site.

    `identity_verified` (P1a-1, R1): whether `agent_id` was actually proven, i.e. the
    caller's own `_require_caller_unless_mpp_payment()` return value — threaded through
    explicitly rather than re-derived from `extract_mpp_token()` here, so the guarantee
    is carried, not inferred from two functions happening to test the same condition.
    Only the direct-`payment_intent_id` branch below uses it to bind the PI to a payer;
    see that branch's own comment for why the SPT branch can't.

    `creator_id` (P1b-4/5): whose x402 wallet(s) to offer in the 402 challenge and
    verify a submitted x402 payment against. None on the session-join route —
    session escrow stays Stripe-only (no x402 analogue), so neither ever fires
    regardless of this being passed.

    P1br-1: a `PAYMENT-SIGNATURE` credential is only ever routed to
    `_verify_x402_payment()` when x402 can genuinely be offered for this
    exact request (enabled, and this resource has something to offer via
    `_x402_offer()` below). Otherwise the credential is **declined
    explicitly** — never silently ignored in favor of the SPT/direct-PI
    branches below. A `PAYMENT-SIGNATURE` header is the payer explicitly
    choosing x402; falling through to Stripe would override that choice
    (see `ROADMAP.md`'s composable-payment-options principle). This was a
    real gap in P1b-5: the branch used to route on the header's mere
    presence, with no `_x402_enabled()` check at all, so a disabled relay
    or a route with no creator (session escrow) still tried to verify and
    402'd three layers down on an unrelated `payTo`/`asset` mismatch,
    swallowing the Stripe fallback by accident rather than by design.
    """
    from emporia.payments import (
        RAIL_STRIPE, build_mpp_challenge, build_x402_challenge, extract_mpp_token,
        extract_x402_payload, confirm_spt, verify_payment_intent, confirm_stripe_intent,
        PaymentRef, _x402_enabled,
    )
    _assert_agent_budget(agent_id, amount_cents)

    def _x402_offer() -> dict[str, Any] | None:
        """What x402 currently offers for this exact request, or None if
        nothing can be offered (disabled, no creator, no wallet, wrong
        currency) — the single source both the credential-honoring check
        below and the eventual 402's "x402" body key read from, so they
        can never disagree about whether x402 applies here.

        P4-1: checks `_x402_enabled()` before doing any wallet lookup.
        `_x402_pay_to_by_network()` used to be evaluated unconditionally as
        an argument to `build_x402_challenge()` — which has its own
        `_x402_enabled()` guard, but only *after* the wallet read already
        happened. On a disabled relay (the shipped default) that meant one
        unconditional DB read per 402 challenge. `build_x402_challenge()`
        keeps its own check too — defense in depth, same posture as
        `_verify_x402_payment()`'s currency check."""
        if not creator_id or not _x402_enabled():
            return None
        return build_x402_challenge(
            amount_cents, challenge_resource, _x402_pay_to_by_network(creator_id),
            currency=currency,
        )

    def _no_credential_402(
        message: str, offer: dict[str, Any] | None, *, declined_reason: str | None = None,
    ) -> JSONResponse:
        challenge_headers = build_mpp_challenge(amount_cents, challenge_resource, currency=currency)
        content: dict[str, Any] = {
            "error": "payment_required",
            "message": message,
            "amount_cents": amount_cents,
            "resource": challenge_resource,
            "protocol": "emporia:v1+mpp",
        }
        # P1b-4: offer both rails in one 402 — every existing key above is
        # untouched, so an MPP-only client sees byte-identical output. x402's
        # own payload is nested under "x402" (not flattened) since its
        # `resource` field is an object and would otherwise collide with the
        # string "resource" key above; see build_x402_challenge()'s docstring.
        if offer:
            content["x402"] = offer["body"]
            challenge_headers = {**challenge_headers, **offer["headers"]}
        # P1brr-2: only set on the "a PAYMENT-SIGNATURE credential was
        # submitted and declined" path below — the ordinary no-credential
        # challenge (the call site with no declined_reason) never sets it,
        # so an MPP-only client, which never sends that header, never sees
        # this key. P1b-4's byte-identical-when-no-credential contract is
        # untouched. Machine-readable so an x402-native client can tell
        # "don't retry this rail" apart from "retry with a signed payload,"
        # rather than parsing English prose in `message`.
        if declined_reason:
            content["declined"] = {"rail": "x402", "reason": declined_reason}
        return JSONResponse(status_code=402, content=content, headers=challenge_headers)

    x402_payload = extract_x402_payload(request.headers.get("PAYMENT-SIGNATURE"))
    if x402_payload is not None:
        offer = _x402_offer()
        if offer:
            return await _verify_x402_payment(
                x402_payload, creator_id=creator_id, amount_cents=amount_cents,
                currency=currency, challenge_resource=challenge_resource,
            )
        # P1brr-2: _x402_offer() returns None for any of four distinct
        # reasons (disabled / wrong currency / no creator on this route /
        # no wallet-and-asset match) — attribute the actual one rather than
        # collapsing them into one message. P1br-1 conflated "no payout
        # wallet" and "route has no x402 analogue" into a single sentence
        # covering both, which also mis-described a non-USD resource (a
        # real regression: pre-P1br-1 that case correctly said
        # "currency=usd", and P1br-1 silently lost that distinction).
        if not _x402_enabled():
            reason_code, reason = "x402_disabled", "x402 is not enabled on this relay"
        elif currency != "usd":
            reason_code, reason = (
                "currency_unsupported",
                f"x402 only supports currency=usd on this relay "
                f"(this resource is priced in {currency})",
            )
        elif not creator_id:
            reason_code, reason = (
                "route_unsupported",
                "this route has no x402 analogue (e.g. session escrow)",
            )
        else:
            reason_code, reason = (
                "no_payout_wallet",
                "no x402 payout wallet is registered for this resource",
            )
        return _no_credential_402(
            f"x402 payment declined: {reason}. {challenge_message}", None,
            declined_reason=reason_code,
        )

    auth_header = request.headers.get("Authorization")
    spt_token = extract_mpp_token(auth_header)

    if spt_token:
        try:
            result = await confirm_spt(
                spt_token, amount_cents, resource_id, agent_id, service_type,
                currency=currency, capture_method=capture_method, resource_type=resource_type,
            )
            result = await verify_payment_intent(result["payment_intent_id"])
            # No expected_payer_agent_id: confirm_spt() mints this PI from the very
            # agent_id this request claimed, so comparing them would be circular and
            # prove nothing — see _assert_payment_intent_matches()'s docstring.
            _assert_payment_intent_matches(
                result, amount_cents=amount_cents, currency=currency,
                resource_type=resource_type, resource_id=resource_id,
                expected_capture_method=capture_method, allowed_statuses=allowed_statuses,
            )
            return PaymentRef(RAIL_STRIPE, result["payment_intent_id"])
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(402, f"SPT validation failed: {e}")

    if req_payment_intent_id:
        try:
            result = await verify_payment_intent(req_payment_intent_id)
            status = result.get("status")
            if status not in allowed_statuses:
                if os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
                    await confirm_stripe_intent(req_payment_intent_id)
                    result = await verify_payment_intent(req_payment_intent_id)
                    status = result.get("status")
                if status not in allowed_statuses:
                    raise HTTPException(402, f"Payment not confirmed (status={status}).")
            # The caller *is* authenticated on this branch (reachable only when
            # extract_mpp_token() is falsy, the exact condition identity_verified
            # reflects), so binding to agent_id here is real, not circular.
            _assert_payment_intent_matches(
                result, amount_cents=amount_cents, currency=currency,
                resource_type=resource_type, resource_id=resource_id,
                expected_capture_method=capture_method, allowed_statuses=allowed_statuses,
                expected_payer_agent_id=(agent_id if identity_verified else None),
            )
            return PaymentRef(RAIL_STRIPE, req_payment_intent_id)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(402, f"Payment verification failed: {e}")

    return _no_credential_402(challenge_message, _x402_offer())


def record_payment(
    payment_intent_id: str,
    agent_id: str,
    amount_cents: int,
    payment_type: str,
    session_id: str | None = None,
    room_id: str | None = None,
    currency: str = "usd",
    *,
    rail: str,
) -> dict[str, Any]:
    """P3 (item 3): `rail` is required and keyword-only, same posture as
    `entitlements.grant()` — every real call site now has a `PaymentRef` to
    thread it from (all three routes that reach this function verify a
    payment via `_verify_and_confirm_payment()` first), so there is no
    legitimate caller left to silently default for, unlike
    `record_settlement()` (session escrow's aggregate settlement has no
    single `PaymentRef` and stays out of scope this pass).
    """
    from emporia.payments import KNOWN_RAILS
    if rail not in KNOWN_RAILS:
        raise ValueError(f"Unknown payment rail {rail!r}, expected one of {sorted(KNOWN_RAILS)}")
    with DB_LOCK, get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM payments WHERE payment_intent_id = ?",
            (payment_intent_id,),
        ).fetchone()
        if existing:
            row = dict(existing)
            if (
                row.get("agent_id") == agent_id
                and row.get("amount_cents") == amount_cents
                and row.get("currency") == currency
                and row.get("payment_type") == payment_type
                and row.get("session_id") == session_id
                and row.get("room_id") == room_id
                and row.get("rail") == rail
            ):
                return row
            raise ValueError(
                f"PaymentIntent {payment_intent_id} is already recorded for a different payment"
            )

        payment_id = f"pay_{uuid.uuid4().hex[:16]}"
        created_at = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO payments "
            "(payment_id, session_id, room_id, agent_id, amount_cents, currency, "
            "payment_intent_id, payment_type, status, created_at, rail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            (payment_id, session_id, room_id, agent_id, amount_cents, currency,
             payment_intent_id, payment_type, created_at, rail),
        )
        return {
            "payment_id": payment_id,
            "session_id": session_id,
            "room_id": room_id,
            "agent_id": agent_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "payment_intent_id": payment_intent_id,
            "payment_type": payment_type,
            "status": "confirmed",
            "created_at": created_at,
            "rail": rail,
        }


def get_agent_total_spend_cents(agent_id: str) -> int:
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM payments WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    return int(row["total"] if row else 0)


def _assert_agent_budget(agent_id: str, amount_cents: int) -> None:
    if MAX_TOTAL_SPEND_CENTS <= 0:
        return
    current = get_agent_total_spend_cents(agent_id)
    if current + amount_cents > MAX_TOTAL_SPEND_CENTS:
        raise HTTPException(402, f"Agent spend limit exceeded: {current + amount_cents}>{MAX_TOTAL_SPEND_CENTS} cents")


def get_session_payments(session_id: str) -> list[dict[str, Any]]:
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE session_id = ? AND payment_type = 'session_stake'",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_settlement(settlement: dict[str, Any]) -> None:
    """`payer_id` (N7 review R1/R2) should be set for a room/Agora settlement — one
    row per payer, see join_room/subscribe_agora_topic — and left unset (NULL) for
    a session's aggregate escrow row, which has no single payer. See
    _caller_owns_settlement_row for how reads use this column.

    P1a-1 (R2): idempotency runs on `dedupe_key`, not `settlement_id` — a fresh
    UUID minted right before every insert meant the old `INSERT OR IGNORE` had no
    key that could ever actually collide, so a replayed settlement always
    inserted a new row.

    P1r-1 (R1): `dedupe_key` is now `settlement_dedupe_key(session_id, payer_id,
    payment_intent_ids)` — payment-scoped, not just payer-scoped. The P1a-2 shape
    (`session_id||':'||COALESCE(payer_id,'')`) treated a payer's second genuine
    payment for the same resource (rejoin after a kick, resubscribe after an
    unsubscribe — both supported flows) as a replay of their first, silently
    dropping the settlement row for real money already taken. See
    `settlement_dedupe_key()`'s docstring for the full shape and why it's hashed.
    """
    from emporia.payments import KNOWN_RAILS, RAIL_STRIPE, settlement_dedupe_key

    settlement_id = f"stl_{uuid.uuid4().hex[:16]}"
    session_id = settlement["session_id"]
    payer_id = settlement.get("payer_id")
    rail = settlement.get("rail", RAIL_STRIPE)
    if rail not in KNOWN_RAILS:
        raise ValueError(f"Unknown payment rail {rail!r}, expected one of {sorted(KNOWN_RAILS)}")
    dedupe_key = settlement_dedupe_key(
        session_id, payer_id, settlement.get("payment_intent_ids", [])
    )
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO settlements "
            "(settlement_id, session_id, winner_id, total_stake_cents, platform_fee_cents, "
            "winner_payout_cents, platform_fee_bps, payment_intent_ids, "
            "transfer_id, transfer_status, status, created_at, payer_id, dedupe_key, rail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dedupe_key) DO NOTHING",
            (
                settlement_id,
                session_id,
                settlement.get("winner_id"),
                settlement.get("total_stake_cents", 0),
                settlement.get("platform_fee_cents", 0),
                settlement.get("winner_payout_cents", 0),
                settlement.get("platform_fee_bps", OPERATOR_FEE_BPS),
                json.dumps(settlement.get("payment_intent_ids", [])),
                settlement.get("transfer_id"),
                settlement.get("transfer_status", "pending_connect"),
                settlement.get("status", "settled"),
                datetime.now(UTC).isoformat(),
                payer_id,
                dedupe_key,
                # P3: an unrecognized value is rejected above rather than let
                # through to storage. _settle_access_payment() threads the
                # real PaymentRef.rail here; other callers still fall back to
                # the "rail" dict key's RAIL_STRIPE default.
                rail,
            ),
        )


# ============================================================================
# Agent Registry (rate limiter + Ed25519 auth)
# ============================================================================

# N2-7: minimal agent_id namespace hardening. Registration binds a keypair to a name
# (_consume_challenge proves possession, the key-mismatch check below blocks takeover of
# an existing name) but never validated the name itself — any charset, any length, no
# normalization. On an open-registration relay, agent_id="stripe_official" or a
# Cyrillic-а homoglyph of an existing agent was free and legitimately claimable.
#
# NFKC-normalizing before validating (rather than after) means Unicode *compatibility*
# variants — full-width, fraktur, other "looks like ASCII but isn't" forms — collapse to
# their canonical ASCII spelling first, so they collide with an existing name through the
# ordinary key-mismatch check below rather than needing a separate confusable-skeleton
# table. True cross-script homoglyphs (Cyrillic, Greek, ...) need no separate handling
# either: they were never valid NFKC-compatibility variants of ASCII letters, so the
# charset restriction below rejects them outright. Charset/length mirror
# identity._key_path()'s own filename sanitization, so the id stored here always matches
# the id its keyfile is named for.
#
# Deliberately NOT built here (see ROADMAP.md): a stable content-addressed id distinct
# from this mutable display name, transfer/release semantics, or reserved/premium names
# gated on trust_level — full namespace ownership is a bigger design than this warrants
# before x402, so this stays scoped to charset/normalization/collision only.
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")


def _normalize_agent_id(agent_id: str) -> str:
    """NFKC-normalize and validate a proposed agent_id. Raises ValueError with a message
    safe to surface to the caller. The regex's required alnum first/last char already
    rejects a leading '_' or '-', which is what keeps a registration from shadowing the
    relay's own reserved '_relay_identity' keyfile name."""
    normalized = unicodedata.normalize("NFKC", agent_id).strip().lower()
    if not _AGENT_ID_RE.match(normalized):
        raise ValueError(
            "agent_id must be 3-64 chars, lowercase letters/digits/underscore/hyphen only, "
            "and start and end with a letter or digit"
        )
    return normalized


class AgentRegistry:
    def __init__(self) -> None:
        self.rate_limits: dict[str, list[float]] = defaultdict(list)
        self.max_per_minute = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))

    def register(
        self,
        agent_id: str,
        public_key_hex: str,
        display_name: str = "",
        stripe_account_id: str | None = None,
        identity_claims: list[dict] | None = None,
    ) -> dict:
        """Register an agent. Returns enriched profile dict with trust_level."""
        agent_id = _normalize_agent_id(agent_id)
        if not public_key_hex:
            raise ValueError("public_key_hex is required — agents must register with an Ed25519 pubkey")
        # Validate Ed25519 pubkey: exactly 64 hex chars (32 bytes).
        try:
            if len(bytes.fromhex(public_key_hex)) != 32:
                raise ValueError()
        except ValueError:
            raise ValueError("public_key_hex must be a 64-char hex string (Ed25519 public key)")

        nous_user_id: str | None = None
        trust_level = "key_only"
        verified_display_name = display_name
        providers_verified: list[str] = []

        if identity_claims:
            from emporia.identity_providers import verify_claim, IdentityVerificationError
            for claim_req in identity_claims:
                provider = claim_req.get("provider", "")
                token = claim_req.get("token", "")
                if not provider or not token:
                    continue
                try:
                    claim = verify_claim(provider, token)
                    providers_verified.append(provider)
                    if provider == "nous":
                        nous_user_id = claim.subject_id
                        if claim.display_name and not verified_display_name:
                            verified_display_name = claim.display_name
                        trust_level = "nous_verified"
                except IdentityVerificationError:
                    pass  # skip bad claims; don't fail the whole registration

        # nous_user_id is stored for auditing/correlation but does not
        # redirect to a canonical agent_id — one Nous user may operate
        # multiple agent personas (demo bots, specialist profiles, etc.).

        try:
            with DB_LOCK, get_db() as conn:
                existing_row = conn.execute(
                    "SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                if existing_row and existing_row["public_key_hex"] != public_key_hex:
                    import hashlib as _hl
                    suffix = _hl.sha256(public_key_hex.encode()).hexdigest()[:6]
                    raise ValueError(
                        f"Agent ID '{agent_id}' is already taken by a different key. "
                        f"Choose a unique agent_id — e.g. '{agent_id}_{suffix}'."
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO authorized_agents "
                    "(agent_id, public_key_hex, display_name, registered_at, is_active, "
                    "stripe_account_id, nous_user_id, trust_level, identity_providers_json) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                    (agent_id, public_key_hex, verified_display_name,
                     datetime.now(UTC).isoformat(), stripe_account_id,
                     nous_user_id, trust_level, json.dumps(providers_verified)),
                )
            return {
                "agent_id": agent_id,
                "display_name": verified_display_name,
                "trust_level": trust_level,
                "nous_user_id": nous_user_id,
                "providers_verified": providers_verified,
            }
        except ValueError:
            raise  # key-conflict or validation errors — preserve message for HTTP status mapping
        except Exception as e:
            raise ValueError(f"Registration failed: {e}") from e

    def get_stripe_account(self, agent_id: str) -> str | None:
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT stripe_account_id FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            return row["stripe_account_id"] if row else None
        except Exception:
            return None

    def set_stripe_account(self, agent_id: str, stripe_account_id: str) -> None:
        with DB_LOCK, get_db() as conn:
            conn.execute(
                "UPDATE authorized_agents SET stripe_account_id = ? WHERE agent_id = ?",
                (stripe_account_id, agent_id),
            )

    def get_x402_pay_to(self, agent_id: str, network_family: str) -> str | None:
        """P1b-2: the x402 analogue of get_stripe_account — except one Stripe
        Connect account serves every currency, while an EVM address and a
        Solana address are different key spaces entirely, so wallets are keyed
        per `network_family` ("evm" | "svm") rather than one column. Not
        auto-provisioned like Connect: there's no wallet to create on an
        agent's behalf, so this returns None until the agent sets one."""
        return self.get_x402_pay_to_all(agent_id).get(network_family)

    def get_x402_pay_to_all(self, agent_id: str) -> dict[str, str]:
        """P1brr-3: every one of this agent's registered wallets in one read
        — `{network_family: address}`. `_x402_pay_to_by_network()`
        (relay_server.py) used to call `get_x402_pay_to()` once per
        configured network, each opening its own connection to re-read the
        same single-row JSON blob; there are only ever two families (`evm`,
        `svm`) inside it, so one read always suffices. Same
        swallow-and-return-empty posture as `get_x402_pay_to()`: a lookup
        failure means "offer nothing," fails closed."""
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT x402_pay_to_json FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            if not row:
                return {}
            return json.loads(row["x402_pay_to_json"] or "{}")
        except Exception:
            return {}

    def set_x402_pay_to(self, agent_id: str, network_family: str, address: str) -> None:
        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT x402_pay_to_json FROM authorized_agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            wallets = json.loads(row["x402_pay_to_json"] or "{}") if row else {}
            wallets[network_family] = address
            conn.execute(
                "UPDATE authorized_agents SET x402_pay_to_json = ? WHERE agent_id = ?",
                (json.dumps(wallets), agent_id),
            )

    def is_authorized(self, agent_id: str) -> bool:
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT is_active FROM authorized_agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()
            return row is not None and bool(row[0])
        except Exception:
            return False

    def verify_signature(self, agent_id: str, payload: dict[str, Any], signature: str) -> bool:
        """Verify Ed25519 signature. Rejects if no public key is registered."""
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()
            if not row or not row[0]:
                # No key registered — reject. Agents MUST register with a pubkey.
                return False
            return verify(payload, signature, row[0])
        except Exception:
            return False

    def check_rate_limit(self, agent_id: str) -> bool:
        now = time.time()
        minute_ago = now - 60
        self.rate_limits[agent_id] = [t for t in self.rate_limits[agent_id] if t > minute_ago]
        if len(self.rate_limits[agent_id]) >= self.max_per_minute:
            return False
        self.rate_limits[agent_id].append(now)
        return True

    def log_message(
        self,
        from_agent: str,
        to_agent: str,
        session_id: str | None,
        msg_type: str,
        payload: dict[str, Any],
        signature: str | None = None,
    ) -> str:
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(UTC).isoformat()
        with DB_LOCK, get_db() as conn:
            conn.execute(
                "INSERT INTO messages "
                "(message_id, from_agent, to_agent, session_id, type, payload_json, signature, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, from_agent, to_agent, session_id,
                 msg_type, json.dumps(payload), signature, created_at),
            )
        # JSONL audit line
        _write_audit_line({
            "timestamp": created_at,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "session_id": session_id,
            "type": msg_type,
            "payload": payload,
        })
        return message_id


AGENT_REGISTRY = AgentRegistry()


def _enforce_agent_rate_limit(agent_id: str) -> None:
    """Per-agent write throttle (N11-4). The identity-keyed complement to the global
    IP middleware below — that middleware exempts localhost and the Docker gateway
    (_RATE_LIMIT_EXEMPT_HOSTS), which is the *default co-located deployment's caller*,
    so IP throttling alone enforces nothing there. Call immediately after resolving
    and validating `agent_id` as the authenticated caller, before any write. Raises
    429 if the agent has exceeded AGENT_REGISTRY.max_per_minute (RATE_LIMIT_PER_MINUTE)
    writes in the trailing 60s.
    """
    if not AGENT_REGISTRY.check_rate_limit(agent_id):
        raise HTTPException(429, "Rate limit exceeded — too many requests, slow down")


# Live counters for the dashboard's Trust & Safety panel — process-lifetime,
# reset on relay restart. Not persisted; this is a demo signal, not an audit log
# (the hash-chained audit log in session_audit.py is the durable record).
SAFETY_STATS: dict[str, int] = {
    "guardrail_blocks": 0,
    "nemo_guardrail_blocks": 0,
    "por_rejections": 0,
    "unsigned_actions_rejected": 0,
}


async def _assert_payload_safe_counted(payload: dict[str, Any]) -> None:
    """Wrap guardrails.assert_payload_safe_async to track block counts for the
    dashboard, distinguishing the always-on regex layer from the optional
    NVIDIA NIM semantic layer via the structured GuardrailResult attached to
    GuardrailBlocked (not by parsing the exception's message text — that broke
    once already when the message wording changed)."""
    try:
        await assert_payload_safe_async(payload)
    except GuardrailBlocked as e:
        if e.result.matched_pattern == "nemo:semantic":
            SAFETY_STATS["nemo_guardrail_blocks"] += 1
        else:
            SAFETY_STATS["guardrail_blocks"] += 1
        raise


def _write_audit_line(entry: dict[str, Any]) -> None:
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    path = LOG_DIR / f"messages_{date_str}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


# ============================================================================
# PoR / Anti-cheat helpers
# ============================================================================

def validate_proof_of_reasoning(turn: dict[str, Any]) -> str:
    """Validate proof-of-reasoning. Returns rationale text or raises PermissionError."""
    rationale = turn.get("peer_text_rationale") or turn.get("rationale") or ""
    if not isinstance(rationale, str) or len(rationale.strip()) < MIN_RATIONALE_CHARS:
        SAFETY_STATS["por_rejections"] += 1
        raise PermissionError(
            f"REJECTED_INFRACTION: Missing Proof-of-Reasoning "
            f"(need >= {MIN_RATIONALE_CHARS} chars)"
        )
    lowered = rationale.casefold()
    for fp in BOT_FINGERPRINTS:
        if fp.casefold() in lowered:
            SAFETY_STATS["por_rejections"] += 1
            raise PermissionError(f"REJECTED_INFRACTION: Bot fingerprint detected: {fp}")
    return rationale


# ============================================================================
# Session Management
# ============================================================================

def _session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "module_type": row["module_type"],
        "config": json.loads(row["config_json"]),
        "payment_rules": json.loads(row["payment_rules_json"]),
        "state": json.loads(row["state_json"]),
        "current_agent": row["current_agent"],
        "step_number": row["step_number"],
        "status": row["status"],
        "participants": json.loads(row["participants_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_session(
    session_id: str,
    module_type: str,
    config: dict,
    payment_rules: dict,
    state: SessionState,
    current_agent: str,
    step_number: int,
    status: str,
    participants: list[str],
    created_at: str,
    updated_at: str,
) -> None:
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, module_type, config_json, payment_rules_json, state_json, "
            "current_agent, step_number, status, participants_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, module_type, json.dumps(config), json.dumps(payment_rules),
             json.dumps(state.to_dict()), current_agent, step_number, status,
             json.dumps(participants), created_at, updated_at),
        )


def load_session(session_id: str) -> dict[str, Any] | None:
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _session_row_to_dict(row) if row else None


def add_participant(session_id: str, agent_id: str, stake_paid: bool = False) -> None:
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO participants (session_id, agent_id, joined_at, stake_paid) "
            "VALUES (?, ?, ?, ?)",
            (session_id, agent_id, datetime.now(UTC).isoformat(), int(stake_paid)),
        )


def record_action(
    session_id: str,
    agent_id: str,
    action_type: str,
    payload: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> str:
    action_id = f"act_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO actions "
            "(action_id, session_id, agent_id, action_type, payload_json, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action_id, session_id, agent_id, action_type,
             json.dumps(payload), json.dumps(result) if result else None,
             datetime.now(UTC).isoformat()),
        )
    return action_id


# ============================================================================
# WebSocket Manager
# ============================================================================

async def broadcast_to_session(session_id: str, message: dict[str, Any]) -> None:
    dead: set[WebSocket] = set()
    for ws in ws_connections.get(session_id, set()):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        ws_connections[session_id].discard(ws)


async def broadcast_to_agent(agent_id: str, message: dict[str, Any]) -> None:
    key = f"agent:{agent_id}"
    dead: set[WebSocket] = set()
    for ws in ws_connections.get(key, set()):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        ws_connections[key].discard(ws)
    # Persist to inbox so agents can poll even without a WS connection
    inbox_id = f"ibx_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(UTC).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_inbox (inbox_id, agent_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (inbox_id, agent_id, message.get("type", "unknown"), json.dumps(message), now_iso),
        )


# ============================================================================
# FastAPI App
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    notice = _stripe_mpp_admin_notice()
    if notice:
        import logging

        logging.getLogger("emporia.relay").warning("%s", notice)
    global _SWEEP_TASK
    _SWEEP_TASK = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        _SWEEP_TASK.cancel()
        with suppress(asyncio.CancelledError):
            await _SWEEP_TASK


app = FastAPI(title="Emporia Relay", version="0.1.0", lifespan=lifespan)

# CORS allowlist: relay's own URL + local dev dashboard ports by default, extendable
# via EMPORIA_CORS_ORIGINS (comma-separated) for a remote-hosted dashboard.
# allow_credentials=True + allow_origins=["*"] would let any site replay a browser's
# stored dashboard JWT/cookie against this relay — keep this an explicit allowlist.
_CORS_ORIGINS = {RELAY_BASE_URL, "http://localhost:8088", "http://localhost:5173"}
_CORS_ORIGINS.update(
    o.strip() for o in os.getenv("EMPORIA_CORS_ORIGINS", "").split(",") if o.strip()
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(_CORS_ORIGINS),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    # P1b-4: PAYMENT-SIGNATURE is the x402 retry header (the browser-wallet
    # analogue of Authorization: Payment for MPP) — without it in
    # allow_headers, a browser-based payer's retry is silently blocked by CORS
    # before it ever reaches the route. PAYMENT-RESPONSE must be exposed for
    # the same reason a JS client would otherwise be unable to read it off a
    # cross-origin response at all.
    allow_headers=["Content-Type", "Authorization", "X-Emporia-Agent-Id", "PAYMENT-SIGNATURE"],
    expose_headers=["PAYMENT-RESPONSE"],
)

# ─── Rate limiting (sliding window, in-memory) ────────────────────────────────
from collections import deque as _deque

_RATE_WINDOWS: dict[tuple[str, str], _deque] = defaultdict(lambda: _deque())
# (ip, bucket) → deque of timestamps
_RATE_LIMITS: dict[str, tuple[int, float]] = {
    "default":   (120, 60.0),   # 120 req / 60s per IP
    "auth":      (10,  60.0),   # 10 req / 60s — dashboard challenge/session
    "write":     (60,  60.0),   # 60 req / 60s — POST/PUT/DELETE
}
_AUTH_PATHS = {"/dashboard/challenge", "/dashboard/session", "/agents/challenge", "/agents/register"}


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    # Always use the real TCP peer address — never trust X-Forwarded-For for bypass decisions.
    # If behind a trusted reverse proxy (nginx, Caddy), set TRUSTED_PROXY=1 and the proxy
    # must rewrite the source IP at TCP level or strip/replace the X-Real-IP header server-side.
    ip = (request.client.host if request.client else "unknown")
    # Localhost, operator-listed proxy IPs, and test client: no rate limiting. Separate from
    # _HEADER_AUTH_TRUSTED_HOSTS — exempting an IP from throttling is a much lower-stakes
    # bypass than trusting it to claim any agent identity.
    if ip in _RATE_LIMIT_EXEMPT_HOSTS or ip == "testclient":
        return await call_next(request)
    path = request.url.path
    bucket = "auth" if path in _AUTH_PATHS else ("write" if request.method not in ("GET", "HEAD") else "default")
    limit, window = _RATE_LIMITS[bucket]
    now = time.time()
    key = (ip, bucket)
    q = _RATE_WINDOWS[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return JSONResponse(
            {"detail": f"Rate limit exceeded — {limit} requests per {int(window)}s per IP"},
            status_code=429,
            headers={"Retry-After": str(int(window - (now - q[0])))},
        )
    q.append(now)
    return await call_next(request)


# ============================================================================
# Background sweep — the relay's only background task (N11-3)
# ============================================================================
# Several in-memory dicts prune their *contents* on natural access paths (a deque
# drains on its own bucket's next request; a WS set discards its own entry on
# disconnect via `finally`) but never drop the now-empty *key* — one permanent
# key per IP/session/agent/challenge ever seen, for the life of a process meant
# to run for months unattended. This sweep evicts those keys on an interval.

_SWEEP_INTERVAL_SECONDS = float(os.getenv("EMPORIA_SWEEP_INTERVAL_SECONDS", "300"))
_SWEEP_TASK: asyncio.Task | None = None  # set/cancelled in lifespan(); exposed for tests/ops

# Process-lifetime sweep health, surfaced read-only via GET /safety/stats (N12-1) — without
# this there was no way to tell the sweep had ever run, which is exactly how a silent
# failure (see _sweep_loop's docstring) would have stayed invisible on a relay meant to
# run unattended for months. Counters only; never put exception text in here — this dict
# is served on a _DELIBERATELY_PUBLIC route.
SWEEP_STATS: dict[str, Any] = {
    "runs": 0,
    "last_run_at": None,
    "last_error_at": None,
    "consecutive_failures": 0,
    "evicted": {"rate_windows": 0, "pending_tokens": 0, "ws_connections": 0, "agent_rate_limits": 0},
}


def _sweep_expired_state() -> dict[str, int]:
    """Evict empty/expired keys from every unbounded in-memory dict. Pure and
    synchronous so it can be tested directly without running the async loop;
    returns counts removed per dict for that purpose."""
    now = time.time()
    removed = {"rate_windows": 0, "pending_tokens": 0, "ws_connections": 0, "agent_rate_limits": 0}

    # _RATE_WINDOWS: drain each key's deque against its own bucket's window (same logic
    # the middleware applies per-request), then drop the key if nothing survived — a key
    # for an IP that stopped sending requests is never touched by the middleware again.
    for key in list(_RATE_WINDOWS.keys()):
        _, bucket = key
        _, window = _RATE_LIMITS[bucket]
        q = _RATE_WINDOWS[key]
        while q and q[0] < now - window:
            q.popleft()
        if not q:
            del _RATE_WINDOWS[key]
            removed["rate_windows"] += 1

    # _PENDING_TOKENS: a JWT nobody polled for stays resident (and valid) past its own
    # exp otherwise. _jwt_decode() already verifies signature + exp — reuse it rather
    # than a second, unverified expiry check.
    for challenge_id in [c for c, tok in _PENDING_TOKENS.items() if _jwt_decode(tok) is None]:
        _PENDING_TOKENS.pop(challenge_id, None)
        removed["pending_tokens"] += 1

    # ws_connections: sockets are already discarded from their set on disconnect
    # (every WS handler's `finally: ws_connections[key].discard(ws)`); this only drops
    # the key once its set is empty.
    for key in [k for k, conns in ws_connections.items() if not conns]:
        del ws_connections[key]
        removed["ws_connections"] += 1

    # AGENT_REGISTRY.rate_limits: same shape as _RATE_WINDOWS above, but only pruned
    # (via check_rate_limit) when that specific agent makes another call.
    minute_ago = now - 60
    for agent_id in list(AGENT_REGISTRY.rate_limits.keys()):
        alive = [t for t in AGENT_REGISTRY.rate_limits[agent_id] if t > minute_ago]
        if alive:
            AGENT_REGISTRY.rate_limits[agent_id] = alive
        else:
            del AGENT_REGISTRY.rate_limits[agent_id]
            removed["agent_rate_limits"] += 1

    return removed


async def _sweep_loop() -> None:
    """Runs _sweep_expired_state() on EMPORIA_SWEEP_INTERVAL_SECONDS (default 300s).
    Started in lifespan() below; cancelled there on shutdown.

    N12-1: a raise out of _sweep_expired_state() must not end this loop for the rest of
    the process's life, silently, with the dicts it exists to bound growing unchecked
    from that point on — the first version of this loop had no exception handling at
    all. Logged and counted in SWEEP_STATS, then the loop keeps going.
    asyncio.CancelledError (shutdown, via lifespan's task.cancel()) is not swallowed
    here — it must propagate so lifespan's own cancel+await sees a real cancellation,
    not a task that already died for an unrelated reason.
    """
    import logging

    logger = logging.getLogger("emporia.relay")
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            removed = _sweep_expired_state()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background sweep failed — will retry next interval")
            SWEEP_STATS["last_error_at"] = datetime.now(UTC).isoformat()
            SWEEP_STATS["consecutive_failures"] += 1
            continue
        SWEEP_STATS["runs"] += 1
        SWEEP_STATS["last_run_at"] = datetime.now(UTC).isoformat()
        SWEEP_STATS["consecutive_failures"] = 0
        for key, count in removed.items():
            SWEEP_STATS["evicted"][key] += count


# ============================================================================
# Pydantic Models
# ============================================================================

class IdentityClaimRequest(BaseModel):
    provider: str   # "nous" | "github" | ...
    token: str      # raw credential — verified server-side, never stored


class RegisterAgentRequest(BaseModel):
    agent_id: str
    public_key_hex: str
    display_name: str = ""
    identity_claims: list[IdentityClaimRequest] = Field(default_factory=list)
    challenge_id: str = ""
    challenge_signature: str = ""  # hex Ed25519 sig of the nonce bytes


class SetX402WalletRequest(BaseModel):
    network_family: str  # "evm" | "svm"
    address: str


class CreateSessionRequest(BaseModel):
    module_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    payment_rules: dict[str, Any] | None = None
    creator_gateway_url: str = ""


class JoinSessionRequest(BaseModel):
    agent_id: str
    agent_gateway_url: str = ""
    payment_intent_id: str | None = None


class SubmitActionRequest(BaseModel):
    agent_id: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    peer_text_rationale: str = ""
    signature: str | None = None


class SendMessageRequest(BaseModel):
    to_agent: str
    session_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None


class CreateListingRequest(BaseModel):
    title: str
    description: str = ""
    listing_type: str = "service"
    payment_mode: str = "free"
    price_usd: str = "0"
    module_type: str | None = None
    expires_in_hours: int = 72
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateEventRequest(BaseModel):
    title: str
    description: str = ""
    module_type: str
    payment_mode: str = "free"
    entry_fee_usd: str = "0"


class CreateRoomRequest(BaseModel):
    name: str
    description: str = ""
    room_type: str = "public"         # "public" | "private"
    gate_type: str = "open"           # "open" | "invite" | "paid"
    entry_fee_cents: int = 0
    currency: str = "USD"
    max_members: int | None = None
    encrypted: bool = False           # True → relay stores opaque ciphertext, skips guardrails
    linked_session_id: str | None = None  # link room to a game session
    # P2-3: how long one payment's access lasts for a paid room. None
    # (default) = no expiry — access lasts as long as the room does. Ignored for
    # non-paid rooms (membership there was never entitlement-gated).
    access_duration_days: int | None = None


class JoinRoomRequest(BaseModel):
    agent_id: str
    payment_intent_id: str | None = None  # required for paid gate


class InviteToRoomRequest(BaseModel):
    invitee_id: str


class SendRoomMessageRequest(BaseModel):
    content: str
    msg_type: str = "chat"            # "chat" | "collab" | "code" | "counter_offer" | "accept" | "reject"
    parent_message_id: str | None = None
    signature: str | None = None
    # negotiation constraints (used when msg_type="counter_offer")
    negotiation_constraints: dict[str, Any] | None = None


class KickFromRoomRequest(BaseModel):
    target_agent_id: str  # agent to kick
    # P2-7: the kicker's choice, per join — defaults to False so a kick alone
    # never moves money. Revocation (P2-6) happens regardless of this flag.
    refund: bool = False


# ============================================================================
# Health & Info
# ============================================================================

def _payment_rails(*, has_stripe: bool | None = None) -> tuple[list[str], list[str]]:
    """The single definition for `payment_rails`/`payment_methods` (P3, item 5)
    — previously two independently hand-built lists, one in `health()`, one in
    `get_agent_profile()`. Pure advertisement: nothing in the payment path
    reads either list to route a charge — this becomes the one place P1b
    builds x402's `accepts` array from.

    `has_stripe=None` selects the relay-wide variant (`GET /health`): no
    agent-specific `stripe_connect` rail, "mpp" derived from whether any
    payment method exists at all. `has_stripe=<bool>` selects the per-agent
    variant (`GET /agents/{id}`): `stripe_connect` appended only when this
    specific agent has a stored Connect account and the relay's MPP profile
    is ready.

    The two variants' formulas are preserved byte-for-byte as found, not
    reconciled — "no wire change" is this item's whole contract, and they
    were already, independently, not equivalent: with `STRIPE_SECRET_KEY`
    set but the MPP profile not ready (`stripe_profile_ready=False`) and
    `EMPORIA_MPP_TEMPO_ENABLED=1`, the relay-wide variant includes `"mpp"`
    in `payment_rails` (payment_methods=["tempo"] is truthy) while the
    per-agent variant's `elif` chain resolves `stripe_enabled` first and
    never reaches the `elif TEMPO_ENABLED` branch — so it silently omits
    `"mpp"`. Recorded in SECURITY.md's P3 section as a noted, unfixed
    finding; both formulas are reproduced here exactly rather than unified.

    P1b-7: `"x402"` joins `payment_rails` — deliberately not `payment_methods`,
    which is Stripe's own MPP-specific sub-vocabulary (`stripe`/`tempo`,
    both settlement backends reachable *through* MPP's challenge/response
    transport). x402 is a rail exactly the way `stripe_pi`/`stripe_connect`
    are: a settlement backend, `KNOWN_RAILS`-tagged, with no MPP transport
    underneath it. Appended identically to both variants (unlike the two
    pre-existing formulas above, there is nothing to preserve byte-for-byte
    here — this is new). Advertised whenever the rail is enabled at all,
    not filtered by whether *this specific* creator has a wallet set — the
    per-creator/per-network filtering already happens where it has to,
    in the 402 challenge itself (_x402_pay_to_by_network(), P1b-4);
    advertising the rail is a relay-wide capability statement, closer to
    "stripe_pi" (available if configured) than "stripe_connect" (this
    agent specifically can receive payouts). This sharpens, rather than
    resolves, the payment_rails/payment_methods conflation this docstring
    already flags above — real now that a second axis (settlement rail vs.
    negotiation transport) actually exists, still not this pass's job to
    reconcile (ROADMAP.md).
    """
    stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))
    stripe_profile_ready = _stripe_profile_ready()
    from emporia.payments import _x402_enabled
    x402_enabled = _x402_enabled()

    if has_stripe is None:
        payment_methods = []
        if stripe_profile_ready:
            payment_methods.append("stripe")
        if TEMPO_ENABLED:
            payment_methods.append("tempo")
        payment_rails = ["free"]
        if payment_methods:
            payment_rails.append("mpp")
        if stripe_enabled:
            payment_rails.append("stripe_pi")
        if x402_enabled:
            payment_rails.append("x402")
        return payment_rails, payment_methods

    payment_rails = ["free"]
    payment_methods = []
    if stripe_profile_ready:
        payment_methods.append("stripe")
        payment_rails += ["mpp", "stripe_pi"]
        if has_stripe:
            payment_rails.append("stripe_connect")  # can receive payouts
    elif stripe_enabled:
        payment_rails.append("stripe_pi")
    elif TEMPO_ENABLED:
        payment_rails.append("mpp")
    if TEMPO_ENABLED:
        payment_methods.append("tempo")
    if x402_enabled:
        payment_rails.append("x402")
    return payment_rails, payment_methods


@app.get("/health")
async def health():
    listing_count = 0
    session_count = 0
    try:
        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM listings WHERE expires_at IS NULL OR expires_at > ?",
                (datetime.now(UTC).isoformat(),),
            ).fetchone()
            listing_count = row["c"] if row else 0
            row2 = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE status NOT IN ('complete','cancelled')"
            ).fetchone()
            session_count = row2["c"] if row2 else 0
    except Exception:
        pass
    stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))
    stripe_profile_ready = _stripe_profile_ready()
    payment_rails, payment_methods = _payment_rails()
    from emporia.payments import _x402_enabled, _x402_facilitator_url, _x402_networks
    return {
        "status": "ok",
        "service": BRAND,
        "relay_id": RELAY_ID,
        "version": "0.1.0",
        "modules": list(MODULE_REGISTRY.keys()),
        "listing_count": listing_count,
        "session_count": session_count,
        "guardrails_mode": guardrails.DEFAULT_MODE,
        "require_nous": REQUIRE_NOUS,
        "operator_fee_bps": OPERATOR_FEE_BPS,
        "mpp_enabled": bool(payment_methods),
        "payment_methods": payment_methods,
        "stripe_enabled": stripe_enabled,
        "stripe_profile_ready": stripe_profile_ready,
        "stripe_profile_id": _stripe_profile_id(),
        "stripe_api_version": os.getenv("STRIPE_API_VERSION", "2026-04-22.preview"),
        "tempo_enabled": TEMPO_ENABLED,
        "max_total_spend_cents": MAX_TOTAL_SPEND_CENTS,
        "payment_rails": payment_rails,
        "stripe_mpp_admin_notice": _stripe_mpp_admin_notice(),
        "chess_lib": _chess_lib_available(),
        # P1b-7: x402 has no per-agent variant (unlike stripe_connect) — the
        # rail is either configured relay-wide or it isn't; which creators
        # can actually receive on it is a 402-time detail, not a /health one.
        "x402_enabled": _x402_enabled(),
        "x402_networks": _x402_networks(),
        "x402_facilitator": _x402_facilitator_url(),
    }


def _chess_lib_available() -> bool:
    try:
        from emporia.modules import chess as _cm

        return bool(getattr(_cm, "_HAS_CHESS", False))
    except Exception:
        return False


@app.get("/safety/stats")
async def safety_stats():
    """Live guardrails + Proof-of-Reasoning rejection counters for the dashboard's
    Trust & Safety panel. Process-lifetime counts, not a persisted audit trail."""
    return {
        "guardrails_mode": guardrails.DEFAULT_MODE,
        "min_rationale_chars": MIN_RATIONALE_CHARS,
        "bot_fingerprints": list(BOT_FINGERPRINTS),
        "nemo_guardrails_enabled": guardrails.NEMO_GUARDRAILS_ENABLED,
        "nemo_guardrails_model": guardrails.NEMO_GUARDRAILS_MODEL if guardrails.NEMO_GUARDRAILS_ENABLED else None,
        "nemo_guardrail_errors": guardrails.NEMO_STATS["errors"],
        **SAFETY_STATS,
        "sweep": SWEEP_STATS,
    }


@app.get("/relay-card")
async def relay_card():
    """Serve the owner agent's relay descriptor when EMPORIA_AGENT_ID is set.

    The relay itself is not an agent. When running locally under an owning agent
    (EMPORIA_AGENT_ID=hackathon_hermes) the descriptor advertises that agent so
    peers can discover its pubkey and capabilities. In relay-only deployments
    (no env var) this endpoint returns 404 — federation peers use /health instead.

    This is Emporia's own document, not an A2A Agent Card — it does not live at
    A2A's reserved discovery path and does not validate against the A2A schema.
    See ARCHITECTURE.md's Identity model section.
    """
    owner = _relay_operator_id()
    if not owner:
        raise HTTPException(404, "Relay-only mode: no owner agent configured "
                            "(set EMPORIA_AGENT_ID to expose a relay descriptor)")
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT public_key_hex, display_name FROM authorized_agents WHERE agent_id=?",
            (owner,),
        ).fetchone()
    if not row:
        raise HTTPException(404, f"Owner agent '{owner}' is not yet registered on this relay")
    capabilities = [
        "emporia:listings:v1",
        "emporia:sessions:v1",
        "emporia:events:v1",
        "emporia:federation:v1",
        *MODULE_REGISTRY.keys(),
    ]
    descriptor = build_relay_descriptor(
        agent_id=owner,
        base_url=RELAY_BASE_URL,
        public_key_hex=row["public_key_hex"],
        display_name=row["display_name"] or owner,
        capabilities=capabilities,
    )
    # P1b-7: /relay-card advertised capabilities but no payment info at all —
    # a peer discovering this relay had no way to learn what it accepts
    # without a separate /health call. Reuses the same _payment_rails()
    # seam as /health and GET /agents/{id} rather than a third hand-built
    # list. build_relay_descriptor() (identity.py) stays payments-independent
    # on purpose — merged in here instead of widening its signature.
    payment_rails, payment_methods = _payment_rails()
    descriptor["payment_rails"] = payment_rails
    descriptor["payment_methods"] = payment_methods
    return descriptor


@app.get("/.well-known/agent.json")
@app.get("/.well-known/agent-card.json")
async def a2a_well_known_not_supported():
    """Emporia does not speak A2A. These are A2A's reserved discovery paths

    (v0.2 legacy and v1.0 canonical respectively) — answering them with a
    non-conformant document is worse than not answering at all, since an A2A
    client's discovery fallback (agent-card.json -> agent.json) would accept
    the 200 at face value instead of correctly concluding "not an A2A peer".
    See /relay-card for Emporia's own (non-A2A) relay descriptor.
    """
    raise HTTPException(
        404,
        "Not an A2A agent. Emporia's relay descriptor is at /relay-card "
        "(not an A2A Agent Card — see ARCHITECTURE.md's Identity model section).",
    )


@app.get("/ui-config")
async def ui_config():
    """Runtime config for the dashboard.

    Returns relay identity, feature flags, and live stats so the dashboard can
    show the operator's identity and relay health without baking these into the build.
    """
    owner = _relay_operator_id()
    with DB_LOCK, get_db() as conn:
        agent_count = conn.execute(
            "SELECT COUNT(*) FROM authorized_agents WHERE is_active = 1"
        ).fetchone()[0]
        active_session_count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'active'"
        ).fetchone()[0]
    return {
        "owner_agent_id": owner or None,
        "relay_id": RELAY_ID,
        "relay_url": RELAY_BASE_URL,
        "require_nous": REQUIRE_NOUS,
        "write_requires_nous": WRITE_REQUIRES_NOUS,
        "require_challenge": REQUIRE_CHALLENGE,
        "agent_count": agent_count,
        "active_session_count": active_session_count,
        "version": "2.0.0",
    }


class DashboardSessionRequest(BaseModel):
    agent_id: str
    challenge_id: str
    signature_hex: str  # Ed25519 sig of the nonce, hex-encoded


@app.post("/dashboard/challenge")
async def dashboard_challenge(request: Request):
    """Step 1 of dashboard auth: get a nonce to sign.

    The agent signs this nonce via the sign_dashboard_challenge MCP tool,
    then submits the signature to POST /dashboard/session for a JWT.
    """
    # Prune expired challenges
    now = time.time()
    expired = [k for k, (_, exp) in _CHALLENGE_STORE.items() if exp < now]
    for k in expired:
        _CHALLENGE_STORE.pop(k, None)

    challenge_id = secrets.token_urlsafe(16)
    nonce = secrets.token_hex(32)
    _CHALLENGE_STORE[challenge_id] = (nonce, now + _CHALLENGE_TTL)
    return {
        "challenge_id": challenge_id,
        "nonce": nonce,
        "expires_in": int(_CHALLENGE_TTL),
        "relay_id": RELAY_ID,
        "instructions": (
            "Sign this nonce with your Ed25519 key via: "
            "sign_dashboard_challenge(relay_url=..., challenge_id=..., nonce=...) "
            "then POST /dashboard/session with the result."
        ),
    }


@app.post("/dashboard/session")
async def dashboard_session(req: DashboardSessionRequest):
    """Step 2 of dashboard auth: verify Ed25519 signature → issue JWT.

    The JWT is scoped to this relay (relay_id in payload). It expires in 1 hour
    and is verified by _dashboard_agent_id() on subsequent requests.
    """
    entry = _CHALLENGE_STORE.pop(req.challenge_id, None)
    if not entry:
        raise HTTPException(400, "Unknown or expired challenge_id")
    nonce, expires_at = entry
    if time.time() > expires_at:
        raise HTTPException(400, "Challenge expired")

    # Look up the agent's registered public key
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?",
            (req.agent_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, f"Agent {req.agent_id!r} not registered on this relay")

    # Verify Ed25519 signature (raw bytes, hex sig — matches sign_dashboard_challenge's
    # sign_raw() on the client side; identity.verify() is for dict-payload/base64 sigs)
    sig_ok = verify_raw(nonce.encode(), req.signature_hex, row["public_key_hex"])
    if not sig_ok:
        raise HTTPException(403, "Invalid signature — ensure you signed the exact nonce bytes")

    now = time.time()
    jwt = _jwt_encode({
        "agent_id": req.agent_id,
        "relay_id": RELAY_ID,
        "iat": int(now),
        "exp": int(now + _JWT_TTL_SECONDS),
    })
    # Store under challenge_id so the polling dashboard can pick it up automatically
    _PENDING_TOKENS[req.challenge_id] = jwt
    return {
        "token": jwt,
        "agent_id": req.agent_id,
        "expires_in": _JWT_TTL_SECONDS,
        "token_type": "Bearer",
    }


@app.get("/dashboard/poll")
async def dashboard_poll(challenge_id: str):
    """Dashboard polls this after initiating /dashboard/challenge.

    Returns the JWT once the agent has completed signing (via sign_dashboard_challenge MCP tool).
    Token is consumed on first successful read — the dashboard stores it in sessionStorage.
    """
    token = _PENDING_TOKENS.pop(challenge_id, None)
    if not token:
        return {"ready": False}
    return {"ready": True, "token": token, "token_type": "Bearer", "expires_in": _JWT_TTL_SECONDS}


@app.get("/modules")
async def list_modules():
    return {"modules": list(MODULE_REGISTRY.keys())}


# ============================================================================
# Agent Registration — challenge / proof-of-key-possession
# ============================================================================

_CHALLENGE_TTL_SECONDS = 300  # nonces expire after 5 minutes


@app.post("/agents/challenge")
async def create_registration_challenge():
    """Issue a one-time nonce. Include challenge_id + Ed25519 signature in /agents/register
    to prove possession of the private key matching the submitted public_key_hex."""
    challenge_id = f"ch_{secrets.token_urlsafe(16)}"
    nonce = secrets.token_hex(32)
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=_CHALLENGE_TTL_SECONDS)
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO reg_challenges (challenge_id, nonce, created_at, expires_at) VALUES (?,?,?,?)",
            (challenge_id, nonce, now.isoformat(), expires.isoformat()),
        )
        # Prune expired challenges (best-effort, fire-and-forget)
        conn.execute("DELETE FROM reg_challenges WHERE expires_at < ?", (now.isoformat(),))
    return {"challenge_id": challenge_id, "nonce": nonce, "expires_in": _CHALLENGE_TTL_SECONDS}


def _consume_challenge(challenge_id: str, public_key_hex: str, signature_hex: str) -> None:
    """Verify and consume a registration challenge. Raises HTTPException on any failure.

    Consumption is a single conditional UPDATE (used=0 → used=1), not a separate
    check-then-write — two concurrent requests racing the same challenge_id can't both
    pass, because SQLite serializes the UPDATE itself; whichever loses sees rowcount==0.
    """
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT nonce, expires_at FROM reg_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if not row:
            raise HTTPException(400, "Unknown challenge_id — request a new one via POST /agents/challenge")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            raise HTTPException(400, "Challenge expired — request a new one")
        nonce = row["nonce"]
        cur = conn.execute(
            "UPDATE reg_challenges SET used = 1 WHERE challenge_id = ? AND used = 0",
            (challenge_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(400, "Challenge already used — request a new one")

    # Verify Ed25519 signature of the nonce
    try:
        pub_bytes = bytes.fromhex(public_key_hex)
        sig_bytes = bytes.fromhex(signature_hex)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(pub_bytes).verify(sig_bytes, nonce.encode())
    except Exception:
        raise HTTPException(403, "Challenge signature invalid — key mismatch or bad signature")


# ============================================================================
# Agent Registration
# ============================================================================

@app.post("/agents/register")
async def register_agent(req: RegisterAgentRequest):
    if not req.public_key_hex:
        raise HTTPException(400, "public_key_hex is required")
    if REQUIRE_NOUS:
        has_nous = any(c.provider == "nous" and c.token for c in req.identity_claims)
        if not has_nous:
            raise HTTPException(
                403,
                "This relay requires Nous identity verification. "
                "Include a valid Nous JWT in identity_claims[{provider: 'nous', token: '...'}].",
            )
    # Challenge verification — proves the registrant holds the matching private key
    if req.challenge_id and req.challenge_signature:
        _consume_challenge(req.challenge_id, req.public_key_hex, req.challenge_signature)
    elif REQUIRE_CHALLENGE:
        raise HTTPException(
            400,
            "This relay requires proof-of-key-possession. "
            "Request a nonce via POST /agents/challenge, sign it with your Ed25519 private key, "
            "and include challenge_id + challenge_signature in this request.",
        )

    try:
        profile = AGENT_REGISTRY.register(
            req.agent_id,
            req.public_key_hex,
            req.display_name,
            identity_claims=[c.model_dump() for c in req.identity_claims],
        )
    except ValueError as e:
        msg = str(e)
        status = 409 if "already registered" in msg or "Key rotation" in msg or "different key" in msg else 400
        raise HTTPException(status, msg)

    canonical_id = profile["agent_id"]

    # Auto-provision a Stripe Connected Account in test mode so the agent can
    # receive real (test) payouts without manual Stripe onboarding.
    stripe_account_id: str | None = None
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if stripe_key.startswith("sk_test_"):
        try:
            from emporia.payments import create_connected_account
            acct = await create_connected_account(canonical_id)
            stripe_account_id = acct["stripe_account_id"]
            AGENT_REGISTRY.set_stripe_account(canonical_id, stripe_account_id)
        except Exception:
            pass  # non-fatal — agent can still play free sessions

    return {
        "status": "registered",
        "agent_id": canonical_id,
        "display_name": profile["display_name"],
        "trust_level": profile["trust_level"],
        "nous_user_id": profile["nous_user_id"],
        "providers_verified": profile["providers_verified"],
        "stripe_account_id": stripe_account_id,
    }


@app.get("/agents")
async def list_agents(request: Request, limit: int = 100, search: str | None = None):
    """List registered agents. Optional ?search= filters by agent_id or display_name.

    N3-3 (N3 review R2): this was the identity-enumeration primitive the N2 review itself
    named as the reason every write-endpoint identity-as-proof attack was reachable
    (unauthenticated, uncapped `limit`) — N2 fixed every consumer of an enumerated identity
    and never closed the enumerator. Now requires an authenticated caller; agent discovery
    is a legitimate feature, so this makes it authenticated rather than removed.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        if search:
            pattern = f"%{search}%"
            rows, has_more = _fetch_page(
                conn,
                "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
                "nous_user_id, trust_level, identity_providers_json "
                "FROM authorized_agents WHERE agent_id LIKE ? OR display_name LIKE ? "
                "ORDER BY registered_at DESC LIMIT ?",
                (pattern, pattern), limit,
            )
        else:
            rows, has_more = _fetch_page(
                conn,
                "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
                "nous_user_id, trust_level, identity_providers_json "
                "FROM authorized_agents ORDER BY registered_at DESC LIMIT ?",
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
            for r in conn.execute(
                f"SELECT winner_id, COUNT(*) AS c FROM settlements "
                f"WHERE winner_id IN ({placeholders}) GROUP BY winner_id",
                agent_ids,
            ).fetchall():
                win_counts[r["winner_id"]] = r["c"]

    agents = []
    for row in rows:
        agent_id = row["agent_id"]
        agents.append({
            "agent_id": agent_id,
            "display_name": row["display_name"] or agent_id,
            "trust_level": row["trust_level"] or "key_only",
            "nous_user_id": row["nous_user_id"],
            "providers_verified": json.loads(row["identity_providers_json"] or "[]"),
            "registered_at": row["registered_at"],
            "is_active": bool(row["is_active"]),
            "has_stripe": bool(row["stripe_account_id"]),
            "session_count": session_counts.get(agent_id, 0),
            "win_count": win_counts.get(agent_id, 0),
        })
    return {"agents": agents, "count": len(agents), "has_more": has_more}


@app.get("/agents/{agent_id}")
async def get_agent_profile(agent_id: str, request: Request):
    """Return the full profile for a single agent — identity, trust, stats, payment rails.

    N4-3 (N4 review R2): this had no `request` param at all — the exact bug shape
    GET /agents was gated for in N3-3 ("the enumeration primitive"), one route over, and
    returning strictly more per-agent data (including the raw Stripe Connect account id)
    than the now-gated list endpoint. Requires an authenticated caller, matching GET
    /agents; agent discovery stays a feature, it just stops being anonymous.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
            "nous_user_id, trust_level, identity_providers_json "
            "FROM authorized_agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
    with DB_LOCK, get_db() as conn:
        session_count = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE agent_id = ?", (agent_id,)
        ).fetchone()[0]
        win_count = conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE winner_id = ?", (agent_id,)
        ).fetchone()[0]

    has_stripe = bool(row["stripe_account_id"])
    stripe_profile_ready = _stripe_profile_ready()
    payment_rails, payment_methods = _payment_rails(has_stripe=has_stripe)

    return {
        "agent_id": row["agent_id"],
        "display_name": row["display_name"] or row["agent_id"],
        "trust_level": row["trust_level"] or "key_only",
        "nous_user_id": row["nous_user_id"],
        "providers_verified": json.loads(row["identity_providers_json"] or "[]"),
        "registered_at": row["registered_at"],
        "is_active": bool(row["is_active"]),
        # N4-3 (R2): stripe_account_id (the raw Connect acct_... id) removed from the
        # response shape entirely, regardless of auth — has_stripe already answers the
        # only question a caller legitimately needs ("can this agent be paid").
        "has_stripe": has_stripe,
        "stripe_profile_ready": stripe_profile_ready,
        "session_count": session_count,
        "win_count": win_count,
        "payment_rails": payment_rails,
        "payment_methods": payment_methods,
    }


@app.get("/agents/{agent_id}/sessions")
async def get_agent_sessions(agent_id: str, request: Request, status: str | None = None, limit: int = 50):
    """Sessions this agent has participated in — same visibility rule as GET /sessions
    (_session_viewer_allowed), so this can't be used to bypass participants-only gating
    on a paid session just by knowing a co-participant's agent_id."""
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
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
    caller = _dashboard_agent_id(request)
    sessions = [_session_row_to_dict(r) for r in rows]
    return {"agent_id": agent_id, "sessions": [s for s in sessions if _session_viewer_allowed(s, caller)]}


@app.get("/agents/{agent_id}/listings")
async def get_agent_listings(agent_id: str, request: Request, limit: int = 50):
    """Listings created by this agent.

    N4-3 (N4 review R2): had no `request` param — sibling of the GET /agents/{agent_id}
    gap above. Requires an authenticated caller, matching that fix.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
    return {
        "agent_id": agent_id,
        "listings": [
            {**dict(r), "metadata": json.loads(r["metadata_json"])}
            for r in rows
        ],
    }


@app.get("/agents/{agent_id}/posts")
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
    viewer_id = _dashboard_agent_id(request)
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
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
            if not _topic_can_read(
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


@app.get("/agents/{agent_id}/inbox")
async def get_agent_inbox(
    agent_id: str, request: Request, unread_only: bool = True, limit: int = 50
):
    """Return pending inbox events for an agent. Poll this to receive events without a WS."""
    _require_caller_is(agent_id, request)
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
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
    return {"agent_id": agent_id, "events": events, "count": len(events)}


@app.post("/agents/{agent_id}/inbox/mark-read")
async def mark_inbox_read(agent_id: str, inbox_ids: list[str], request: Request):
    """Mark specific inbox events as read and auto-expire old read items (7+ days)."""
    _require_caller_is(agent_id, request)
    _enforce_agent_rate_limit(agent_id)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with DB_LOCK, get_db() as conn:
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


_X402_ADDRESS_PATTERNS = {
    # P1b-2: an EVM address and a Solana address are different key spaces
    # (secp256k1 vs. ed25519), so a wallet is set per network_family rather
    # than as one universal address — see AGENT_REGISTRY.get/set_x402_pay_to.
    "evm": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "svm": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),  # base58, Bitcoin alphabet (no 0OIl)
}


@app.post("/agents/{agent_id}/x402-wallet")
async def set_x402_wallet(agent_id: str, req: SetX402WalletRequest, request: Request):
    """Register (or update) this agent's x402 `payTo` wallet address for one
    network family. Unlike Stripe Connect (auto-provisioned at registration,
    `:2586`), there is no wallet to create on an agent's behalf — this must be
    called explicitly, and a wrong address is unrecoverable money, so the
    format is validated and rejected loudly rather than accepted silently."""
    _require_caller_is(agent_id, request)
    _require_registered(agent_id)
    _enforce_agent_rate_limit(agent_id)
    pattern = _X402_ADDRESS_PATTERNS.get(req.network_family)
    if pattern is None:
        raise HTTPException(
            400, f"network_family must be one of {sorted(_X402_ADDRESS_PATTERNS)}"
        )
    if not pattern.match(req.address):
        raise HTTPException(
            400, f"address does not look like a valid {req.network_family} wallet address"
        )
    AGENT_REGISTRY.set_x402_pay_to(agent_id, req.network_family, req.address)
    return {"agent_id": agent_id, "network_family": req.network_family, "address": req.address}


# ============================================================================
# Payments — create intents, list settlements
# ============================================================================

class CreatePaymentIntentRequest(BaseModel):
    amount_cents: int
    session_id: str | None = None
    room_id: str | None = None
    seller_id: str = "relay"
    service_type: str = "emporia:session"


@app.post("/payments/create-intent")
async def create_payment_intent_endpoint(req: CreatePaymentIntentRequest, request: Request):
    """Create a Stripe PaymentIntent for a specific paid session or room.
    Returns payment_intent_id to pass to /sessions/{id}/join or /rooms/{id}/join."""
    from emporia.payments import create_stake_intent

    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _enforce_agent_rate_limit(caller)

    if bool(req.session_id) == bool(req.room_id):
        raise HTTPException(400, "Provide exactly one of session_id or room_id")

    target_id = req.session_id or req.room_id or ""
    expected_amount = 0
    currency = "usd"
    capture_method = "manual"
    resource_type = "session"
    service_type = req.service_type

    if req.session_id:
        session = load_session(req.session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        payment_rules = session.get("payment_rules", {})
        stake = payment_rules.get("stake_per_participant", "0")
        expected_amount = int(float(stake) * 100) if stake else 0
        currency = (payment_rules.get("currency") or "usd").lower()
        service_type = service_type or session.get("module_type") or "emporia:session"
        if payment_rules.get("mode", "free") == "free" or expected_amount <= 0:
            raise HTTPException(400, "Session does not require a paid stake")
    else:
        room = rooms_db.get_room(target_id, db_path=DATABASE_PATH)
        if not room:
            raise HTTPException(404, "Room not found")
        expected_amount = int(room.entry_fee_cents or 0)
        currency = (room.currency or "usd").lower()
        capture_method = "automatic"
        resource_type = "room"
        if room.gate_type != "paid" or expected_amount <= 0:
            raise HTTPException(400, "Room does not require a paid entry fee")
        if service_type == "emporia:session":
            service_type = "emporia:room_entry"

    if req.amount_cents != expected_amount:
        raise HTTPException(
            400,
            f"amount_cents must match relay pricing for this resource ({expected_amount})",
        )

    _assert_agent_budget(caller, expected_amount)

    try:
        result = await create_stake_intent(
            session_id=target_id,
            amount_cents=expected_amount,
            buyer_id=caller,
            seller_id=req.seller_id,
            service_type=service_type,
            capture_method=capture_method,
            currency=currency,
            resource_type=resource_type,
        )
        return result
    except RuntimeError as e:
        raise HTTPException(402, str(e))


def _settlement_resource_viewer_allowed(resource_id: str, caller: str | None, conn: Any) -> bool:
    """A `settlements.session_id` column is overloaded across three resource kinds —
    session escrow, room entry, Agora paid_invite subscription — since
    record_settlement() keys every write on whatever id the call site passed as
    "session_id" (`room_id` for room entry (`:3829`), `topic_id` for Agora (`:4390`)).
    Resolve which kind a given id actually is and apply that resource's own visibility
    rule; an id matching none of the three (garbage, or a resource since deleted) is
    operator-only rather than assumed-public — this is the fix for N4 review R1(a):
    GET /payments/settlements/{session_id} previously had no check of any kind, making
    every resource's PaymentIntent ids anonymously readable regardless of kind.

    N5 review R1: the Agora branch used to key on `visibility` alone, via
    `_topic_can_read`. But `visibility` and `gate_type` are independent fields
    (CreateAgoraTopicRequest) — a topic can be `visibility="public"` (publicly
    discoverable) and still `gate_type="paid_invite"` (a paywall). `_topic_can_read`
    returns True unconditionally for public topics, so a paid topic's PaymentIntent
    ids were anonymously readable whenever it was also publicly listed — the natural
    paid-community configuration. The paywall is `gate_type`/`entry_fee_cents`, so a
    paid topic requires membership regardless of `visibility`; only an unpaid topic
    falls back to the topic's own read rule.
    """
    owner = _relay_operator_id()
    if owner and caller == owner:
        return True
    s = load_session(resource_id)
    if s:
        return _session_viewer_allowed(s, caller)
    room = rooms_db.get_room(resource_id, db_path=DATABASE_PATH)
    if room:
        # N5 review R3: this used to require membership unconditionally, stricter than
        # the room's own read rule (rooms.py / list_rooms filtering, room_type=="public"
        # → readable by anyone; only private rooms require membership). Public rooms
        # can only ever be free (create_room rejects room_type="public" with a non-open
        # gate_type), so this widening carries no payment-leak risk.
        if room.room_type == "public":
            return True
        return bool(caller and rooms_db.is_member(resource_id, caller, db_path=DATABASE_PATH))
    topic = conn.execute(
        "SELECT visibility, gate_type, entry_fee_cents, topic_id FROM agora_topics WHERE topic_id=?",
        (resource_id,),
    ).fetchone()
    if topic:
        if _topic_is_paid(topic["gate_type"], topic["entry_fee_cents"]):
            if not caller:
                return False
            # P2-5: expiry-aware — a lapsed paid_invite entitlement no longer
            # grants the membership-gated view of this resource's full
            # settlement history (the caller's own receipt stays reachable via
            # the separate ?agent_id= path regardless — see
            # test_kicked_room_member_can_still_read_own_receipt's sibling).
            return _agora_is_member(conn, topic["topic_id"], caller, gate_type=topic["gate_type"])
        return _topic_can_read(
            topic["visibility"], topic["gate_type"], topic["entry_fee_cents"],
            caller, topic["topic_id"], conn,
        )
    return False


def _caller_participant_session_ids(caller: str | None, conn: Any) -> set[str]:
    """The set of session_ids `caller` participates in, prefetched once per
    request rather than resolved per settlement row. N7 review R2: the previous
    per-row ownership check called load_session() for every row, and
    load_session() opens a brand-new sqlite3.Connection (get_db() → connect +
    PRAGMA journal_mode=WAL) on every call — an N-row settlement response cost up
    to N fresh DB connections, the exact class of N+1 N3-3 (O1) fixed once already
    for GET /agents."""
    if not caller:
        return set()
    return {
        r["session_id"]
        for r in conn.execute(
            "SELECT session_id FROM participants WHERE agent_id = ?", (caller,)
        ).fetchall()
    }


def _caller_owns_settlement_row(
    row: Any, caller: str | None, participant_session_ids: set[str]
) -> bool:
    """True if `caller` may see this specific settlement row's PaymentIntent ids
    unredacted — distinct from _settlement_resource_viewer_allowed, which decides
    whether the caller may see the *resource*'s settlements at all.

    N7 review R1/R2: settlements now carries a real `payer_id` column (see
    _migrate_settlements) instead of deriving ownership per-row from a
    payments-table join. A room or Agora settlement is one row PER PAYER —
    join_room and subscribe_agora_topic populate payer_id at write time — so
    ownership there is a plain column comparison. A session's settlement is one
    aggregate row for the whole shared escrow (payer_id stays NULL by design; see
    _migrate_settlements), correctly visible to any participant — resolved
    against the prefetched `participant_session_ids` (_caller_participant_session_ids)
    rather than a per-row load_session() call.

    N8 review R4/R5: list_settlements' `?agent_id=` branch re-expresses this exact
    rule directly in SQL (`payer_id = ? OR (payer_id IS NULL AND session_id IN
    (...))`) so the ownership filter runs in the database rather than after
    fetching every candidate row. The two must stay in lockstep — change one,
    change the other — or a row with someone else's payer_id whose session_id
    happens to also be one of the caller's participant sessions gets a different
    verdict from each. The agreement test
    (test_settlement_ownership_sql_matches_python_predicate) is what catches that
    drift.
    """
    if not caller:
        return False
    if row["payer_id"] is not None:
        return row["payer_id"] == caller
    return row["session_id"] in participant_session_ids


# P1r-3 (R2): every settlements column the API is allowed to return, and every
# column that is storage-only bookkeeping. _settlement_row_dict() used to build
# its response with `{**dict(row), ...}` — every column, unconditionally — which
# is why N6-2, N7-R4, N8-R1 and now P1r-3 are four separate patches to the same
# function for the same class of bug: each added column leaks by default unless
# someone remembers to redact it. `dedupe_key` (P1a-2) embeds `payer_id` in
# plaintext and was never redacted, undoing N8-R1's redaction of that same field.
# Classifying every column here — enforced by
# test_settlement_public_and_internal_columns_cover_the_schema, which fails the
# suite the moment PRAGMA table_info(settlements) gains a column not listed in
# either set — turns "did I remember to redact this" from a habit into a check.
_SETTLEMENT_PUBLIC_COLUMNS = frozenset({
    "settlement_id", "session_id", "winner_id", "total_stake_cents",
    "platform_fee_cents", "winner_payout_cents", "platform_fee_bps",
    "payment_intent_ids", "transfer_id", "transfer_status", "status",
    "created_at", "payer_id", "refund_id", "reversal_id", "rail",
})
_SETTLEMENT_INTERNAL_COLUMNS = frozenset({"dedupe_key"})


def _settlement_public_dict(row: Any) -> dict[str, Any]:
    """Project a `settlements` row down to `_SETTLEMENT_PUBLIC_COLUMNS`, with
    `payment_intent_ids` decoded. The one place every settlement read path builds
    its base dict from — P2-2 (R4): `_settlement_row_dict()` used this
    projection, but `list_settlements()`'s `?agent_id=` and unfiltered-operator
    branches still built their response with `{**dict(r), ...}` (every column,
    including `dedupe_key`), which is the exact `{**dict(row)}` pattern P1r-3
    fixed in `_settlement_row_dict()` and is why the guard test it added
    (`test_settlement_public_and_internal_columns_cover_the_schema`) stayed green
    while those two branches kept leaking: it checks that the two column *sets*
    cover the schema, not that every call site actually uses them. Those two
    branches don't call `_settlement_row_dict()` itself — their SQL is already
    ownership-scoped (N8-R4/R5) and calling it would apply its per-row redaction
    on top of rows that are supposed to come back unredacted — so the projection
    alone is factored out here for both to share.
    """
    d = {k: v for k, v in dict(row).items() if k in _SETTLEMENT_PUBLIC_COLUMNS}
    d["payment_intent_ids"] = json.loads(row["payment_intent_ids"])
    return d


def _settlement_row_dict(
    row: Any, caller: str | None, participant_session_ids: set[str]
) -> dict[str, Any]:
    """Settlement row as JSON, with `payment_intent_ids`/`transfer_id` redacted to
    `"[redacted]"` unless `caller` is the relay operator or owns this specific row
    (see _caller_owns_settlement_row). Used by every settlement read path so a
    caller authorized to see a resource's settlements (via
    _settlement_resource_viewer_allowed) doesn't also see every other payer's
    opaque per-transaction identifiers on that resource.

    N7 review R4: the original N6-2 redaction covered payment_intent_ids but not
    transfer_id — the Stripe transfer object id for that specific payer's payout,
    1:1 with their payment — so a co-payer still got a per-transaction Stripe
    identifier out of the same response N6-2 was written to lock down.
    `total_stake_cents`/`winner_payout_cents` are left alone: room/topic entry
    fees are uniform, so a co-payer's amount is already public from the resource's
    own fee — only the opaque identifiers need covering.

    N8 review R1: `payer_id` itself — the column N7-2 added — was never redacted
    either, so a caller authorized to see a resource's settlements got every
    co-payer's identity along with everything else. `_redact_payment_row` already
    treats `agent_id` as sensitive for the identical fact on the sibling
    /payments/records?room_id= endpoint; the two must agree. Guarded on `is not
    None` — a session's aggregate row legitimately keeps `payer_id` NULL (see
    _migrate_settlements), and that must stay `null` in the response, not become
    the string `"[redacted]"`.

    P1r-3 (R2): built from `_SETTLEMENT_PUBLIC_COLUMNS` only, not `{**dict(row)}`
    — see that set's own comment. `dedupe_key` (`_SETTLEMENT_INTERNAL_COLUMNS`)
    embeds `payer_id` in plaintext (P1r-1) and must never reach a response; it
    was leaking here from the moment P1a-2 added the column.
    """
    d = _settlement_public_dict(row)
    owner = _relay_operator_id()
    if owner and caller == owner:
        return d
    if _caller_owns_settlement_row(row, caller, participant_session_ids):
        return d
    d["payment_intent_ids"] = ["[redacted]"]
    if d.get("transfer_id"):
        d["transfer_id"] = "[redacted]"
    if d.get("payer_id") is not None:
        d["payer_id"] = "[redacted]"
    # P2r-3 (R3): refund_id/reversal_id are per-payment Stripe identifiers,
    # the same class N7-R4 already covers for transfer_id — redact alongside it.
    if d.get("refund_id"):
        d["refund_id"] = "[redacted]"
    if d.get("reversal_id"):
        d["reversal_id"] = "[redacted]"
    return d


def _redact_payment_row(row: Any, caller: str | None) -> dict[str, Any]:
    """Raw payment row as JSON, with `agent_id`/`payment_intent_id` redacted unless
    `caller` is the relay operator or the payer who made this specific payment.
    Sibling of _settlement_row_dict for list_payment_records' ?room_id= branch."""
    d = dict(row)
    owner = _relay_operator_id()
    if owner and caller == owner:
        return d
    if caller and d.get("agent_id") == caller:
        return d
    d["agent_id"] = "[redacted]"
    d["payment_intent_id"] = "[redacted]"
    return d


def _require_operator(caller: str | None, action: str) -> None:
    """Raise 403 for an operator-only maintenance action, with a reason that
    distinguishes "no relay operator is configured at all" (EMPORIA_AGENT_ID unset —
    no identity can ever satisfy this check) from "you aren't the configured
    operator" (EMPORIA_AGENT_ID is set, but not to you). N5 review R5 found
    trigger_federation_sync gave the same generic message either way, which reads
    as "authenticate as the right agent" when the real problem — on a default
    install, where EMPORIA_AGENT_ID is unset — is that the action is unreachable by
    anyone, including whoever would become the operator by setting it.
    """
    owner = _relay_operator_id()
    if not owner:
        raise HTTPException(
            403,
            f"{action}: no relay operator is configured (EMPORIA_AGENT_ID unset) — "
            "this action is unavailable on this relay"
        )
    if not caller or caller != owner:
        raise HTTPException(403, f"{action}: relay operator access required")


@app.get("/payments/settlements")
async def list_settlements(
    request: Request,
    session_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
):
    """List settlement records.

    Access rules:
      - ?session_id=X  → the referenced resource's own visibility rule (see
                          _settlement_resource_viewer_allowed) — not unconditionally
                          public; N4 review R1(a) found a leaked PaymentIntent id here
                          was one half of a working payment bypass for paid Agora topics.
                          Capped by `limit` (default 50) with `has_more` in the response
                          (N7 review R3 — this read every row for a resource with no cap
                          at all, unlike every other list endpoint after N3-6/N4-5).
      - ?agent_id=X    → requires dashboard auth (localhost or JWT); scoped to settlement rows
                          X actually owns — X's own room/Agora payer_id, or a session X
                          participated in (N5 review R3 fixed the original "no path to your own
                          receipt" gap; N6 review R1 found that fix's resource-level join leaked
                          co-payers' rows; N7 review R1 found N6-2's own ownership filter applied
                          LIMIT to *candidates* before filtering, so a caller's own older receipt
                          could still fall off a busy shared resource — the predicate is now
                          pushed into SQL so LIMIT applies to owned rows directly). Capped by
                          `limit` (default 50) with `has_more` in the response (N8 review R2 —
                          this and the unfiltered branch below were hardcoded to `LIMIT 100` and
                          always reported `has_more: false`, reintroducing on these two branches
                          the exact truncation-signal defect N6-3 fixed for ?session_id=)
      - no filter      → requires dashboard auth AND relay operator identity. Same `limit`/
                          `has_more` contract as the other two branches (N8 review R2)

    A caller authorized to see a resource's settlements (via ?session_id=, above) still only
    sees `payment_intent_ids`/`transfer_id`/`payer_id` on rows they own — see _settlement_row_dict.
    """
    dashboard_id = _dashboard_agent_id(request)
    owner = _relay_operator_id()
    limit = _clamp_limit(limit)

    # Unfiltered global list: operator only
    if not session_id and not agent_id:
        _require_operator(dashboard_id, "Global settlements")

    # Agent-scoped: must be the agent themselves (via dashboard auth)
    if agent_id and not session_id:
        if not dashboard_id:
            raise HTTPException(
                401,
                "Agent settlements require authentication. "
                "Access from localhost with X-Emporia-Agent-Id header, "
                "or use the get_settlements MCP tool."
            )
        if dashboard_id != agent_id and dashboard_id != owner:
            raise HTTPException(403, "Cannot view another agent's settlements")

    with DB_LOCK, get_db() as conn:
        if session_id:
            if not _settlement_resource_viewer_allowed(session_id, dashboard_id, conn):
                raise HTTPException(403, "Not authorized to view this resource's settlement")
            rows, has_more = _fetch_page(
                conn,
                "SELECT * FROM settlements WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id,), limit,
            )
            participant_session_ids = _caller_participant_session_ids(dashboard_id, conn)
            settlements = [_settlement_row_dict(r, dashboard_id, participant_session_ids) for r in rows]
        elif agent_id:
            # Ownership predicate applied directly in SQL (N7 review R1) — payer_id
            # covers room/Agora rows, the participants subquery covers a session's
            # aggregate escrow row. LIMIT therefore bounds owned rows, not
            # candidates-before-filtering.
            #
            # N8 review R4/R5: this predicate must stay in lockstep with
            # _caller_owns_settlement_row's Python rule — participation only
            # decides ownership when payer_id IS NULL, never as an independent
            # OR. The old unconditional `payer_id = ? OR session_id IN (...)` let
            # a row with someone else's payer_id count as "owned" here whenever
            # its session_id happened to also appear in the caller's own
            # participants rows — a false-positive ownership verdict this branch
            # trusts completely (unlike ?session_id=, it never calls
            # _settlement_row_dict, since its WHERE clause is supposed to already
            # be scoped to owned rows), so the row's real payment_intent_ids and
            # transfer_id leaked, not just payer_id. If you change either this
            # query or _caller_owns_settlement_row, change both — the agreement
            # test (test_settlement_ownership_sql_matches_python_predicate) is
            # what keeps them honest.
            rows, has_more = _fetch_page(
                conn,
                "SELECT * FROM settlements WHERE payer_id = ? "
                "OR (payer_id IS NULL AND session_id IN "
                "(SELECT session_id FROM participants WHERE agent_id = ?)) "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, agent_id), limit,
            )
            settlements = [_settlement_public_dict(r) for r in rows]
        else:
            rows, has_more = _fetch_page(
                conn,
                "SELECT * FROM settlements ORDER BY created_at DESC LIMIT ?",
                (), limit,
            )
            settlements = [_settlement_public_dict(r) for r in rows]
    return {"settlements": settlements, "count": len(settlements), "has_more": has_more}


@app.get("/payments/settlements/{session_id}")
async def get_settlement(session_id: str, request: Request, limit: int = 50):
    """N4 review R1(a): previously took no `request` param at all — anonymously
    readable regardless of the underlying resource's own visibility rule. See
    _settlement_resource_viewer_allowed. Rows the caller doesn't own have
    `payment_intent_ids`/`transfer_id` redacted — see _settlement_row_dict (N6
    review R1). Capped by `limit` (default 50) with `has_more` in the response
    (N7 review R3 — previously read every row for a resource with no cap at all)."""
    caller = _dashboard_agent_id(request)
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        if not _settlement_resource_viewer_allowed(session_id, caller, conn):
            raise HTTPException(403, "Not authorized to view this resource's settlement")
        rows, has_more = _fetch_page(
            conn,
            "SELECT * FROM settlements WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id,), limit,
        )
        participant_session_ids = _caller_participant_session_ids(caller, conn)
        settlements = [_settlement_row_dict(r, caller, participant_session_ids) for r in rows]
    if not rows:
        raise HTTPException(404, f"No settlement for session {session_id}")
    return {"settlements": settlements, "count": len(settlements), "has_more": has_more}


@app.get("/payments/records")
async def list_payment_records(
    request: Request, session_id: str | None = None, room_id: str | None = None
):
    """List raw payment records (confirmed PaymentIntents).

    Access rules mirror /payments/settlements:
      - ?session_id=X → same visibility as the session (_session_viewer_allowed); every
                         participant sees every row, matching the shared-escrow model
                         settlements use for sessions (not changed by N6 review R1)
      - ?room_id=X    → room members (or the relay operator) may see the room's payment
                         activity exists, but `agent_id`/`payment_intent_id` are redacted
                         on rows that aren't the caller's own payment (N6 review R1 —
                         sibling of the settlements redaction, same one-row-per-payer shape)
      - no filter     → relay operator only
    """
    caller = _dashboard_agent_id(request)
    owner = _relay_operator_id()
    is_owner = bool(owner and caller == owner)
    with DB_LOCK, get_db() as conn:
        if session_id:
            s = load_session(session_id)
            if not s or not _session_viewer_allowed(s, caller):
                raise HTTPException(403, "Not authorized to view this session's payment records")
            rows = conn.execute(
                "SELECT * FROM payments WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
            payments = [dict(r) for r in rows]
        elif room_id:
            room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
            if not room or not (
                is_owner or (caller and rooms_db.is_member(room_id, caller, db_path=DATABASE_PATH))
            ):
                raise HTTPException(403, "Not authorized to view this room's payment records")
            rows = conn.execute(
                "SELECT * FROM payments WHERE room_id = ? ORDER BY created_at DESC",
                (room_id,),
            ).fetchall()
            payments = [_redact_payment_row(r, caller) for r in rows]
        else:
            _require_operator(caller, "Global payment records")
            rows = conn.execute(
                "SELECT * FROM payments ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            payments = [dict(r) for r in rows]
    return {"payments": payments}


# ============================================================================
# Listings ("Craigslist for agents")
# ============================================================================

@app.post("/listings")
async def create_listing(req: CreateListingRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))

    listing_id = f"lst_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC).isoformat()
    from datetime import timedelta
    expires_at = (datetime.now(UTC) + timedelta(hours=req.expires_in_hours)).isoformat()

    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
            "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (listing_id, req.title, req.description, req.listing_type, caller,
             req.payment_mode, req.price_usd, req.module_type, RELAY_BASE_URL,
             expires_at, json.dumps(req.metadata), now),
        )
    await broadcast_global({
        "type": "listing_created",
        "listing_id": listing_id,
        "title": req.title,
        "module_type": req.module_type,
        "agent_id": caller,
    })
    return {"listing_id": listing_id, "status": "created"}


@app.get("/listings")
async def get_listings(
    listing_type: str | None = None,
    module_type: str | None = None,
    creator_id: str | None = None,
    include_rooms: bool = True,
    limit: int = 50,
):
    """Browse all discoverable listings.

    Includes standard listings (sessions, services, events) plus public and
    paid-private rooms when include_rooms=true (default). Rooms appear as
    listing_type='room' with gate_type, entry_fee_cents, and member_count visible.
    Private invite-only rooms are never shown to non-members.
    """
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        q = "SELECT * FROM listings WHERE (expires_at IS NULL OR expires_at > ?) "
        params: list[Any] = [datetime.now(UTC).isoformat()]
        if listing_type and listing_type != "room":
            q += "AND listing_type = ? "
            params.append(listing_type)
        if module_type:
            q += "AND module_type = ? "
            params.append(module_type)
        if creator_id:
            q += "AND agent_id = ? "
            params.append(creator_id)
        q += "ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()

    listings = [
        {**dict(r), "metadata": json.loads(r["metadata_json"])}
        for r in rows
    ]

    # Surface rooms as browsable listings so agents can discover them
    if include_rooms and listing_type in (None, "room"):
        room_list = rooms_db.list_rooms(room_type="public", limit=limit, db_path=DATABASE_PATH)
        # Also include paid private rooms (paid gate) — agents need to know they exist
        paid_private = rooms_db.list_rooms(room_type="private", limit=limit, db_path=DATABASE_PATH)
        for room in room_list + paid_private:
            if room.gate_type == "invite":
                continue  # never surfaced — invite-only means truly private
            listings.append({
                "listing_id": room.room_id,
                "listing_type": "room",
                "title": room.name,
                "description": room.description,
                "agent_id": room.creator_id,
                "room_type": room.room_type,
                "gate_type": room.gate_type,
                "entry_fee_cents": room.entry_fee_cents if room.gate_type == "paid" else 0,
                "currency": room.currency,
                "member_count": len(room.members),
                "max_members": room.max_members,
                "encrypted": room.encrypted,
                # payment_mode is a *different* vocabulary (listings/events axis,
                # PaymentMode in engine/game_registry.py) — "stripe_link" here is
                # that axis's own value, deliberately not renamed by the P3 gate_type
                # rename. Reconciling the two vocabularies is out of scope (see
                # ROADMAP.md's P3 section).
                "payment_mode": "stripe_link" if room.gate_type == "paid" else "free",
                "price_usd": f"{room.entry_fee_cents / 100:.2f}" if room.gate_type == "paid" else "0",
                "created_at": room.created_at,
                "metadata": {"linked_session_id": room.linked_session_id},
            })

    if module_type:
        listings = [l for l in listings if l.get("module_type") == module_type]

    return {"listings": listings[:limit], "count": len(listings[:limit])}


# ============================================================================
# Events / Tournaments
# ============================================================================

@app.post("/events")
async def create_event(req: CreateEventRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))

    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO events (event_id, title, description, module_type, organizer_id, "
            "payment_mode, entry_fee_usd, status, bracket_json, participants_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '{}', '[]', ?, ?)",
            (event_id, req.title, req.description, req.module_type, caller,
             req.payment_mode, req.entry_fee_usd, now, now),
        )
    # Also create a listing for the event
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
            "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, created_at) "
            "VALUES (?, ?, ?, 'event', ?, ?, ?, ?, ?, NULL, ?, ?)",
            (f"lst_{event_id}", req.title, req.description or "", caller,
             req.payment_mode, req.entry_fee_usd, req.module_type, RELAY_BASE_URL,
             json.dumps({"event_id": event_id}), now),
        )
    return {"event_id": event_id, "status": "open"}


@app.get("/events")
async def list_events(status: str | None = None, limit: int = 50):
    """N4-5 (N4 review R7): previously had no `limit` param at all — every event, with
    full bracket + participant lists, in one unbounded query. Public discovery stays
    public (same shape as GET /listings); this just caps the row scan, matching every
    other list endpoint after N3-6.

    N6 review R2: `count` (added N5-5) is the returned page size, not a total — it
    can never exceed `limit`, so it could never actually signal truncation despite
    that being N5-5's stated goal. `has_more` (via _fetch_page) is the real signal.
    """
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        if status:
            rows, has_more = _fetch_page(
                conn,
                "SELECT * FROM events WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status,), limit,
            )
        else:
            rows, has_more = _fetch_page(
                conn, "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (), limit,
            )
    events = [
        {
            **dict(r),
            "bracket": json.loads(r["bracket_json"]),
            "participants": json.loads(r["participants_json"]),
        }
        for r in rows
    ]
    return {"events": events, "count": len(events), "has_more": has_more}


@app.get("/events/{event_id}")
async def get_event(event_id: str):
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Event not found")
    return {
        **dict(row),
        "bracket": json.loads(row["bracket_json"]),
        "participants": json.loads(row["participants_json"]),
    }


# ============================================================================
# Federation
# ============================================================================

@app.get("/gaming/v1/federate/listings")
async def federated_listings_out():
    """Serve local listings for federation pull by peer relays.

    Signed with this relay's own Ed25519 key (persisted, distinct from any agent's key —
    see identity.generate_or_load_relay_keypair()) so a pulling peer can verify the batch
    actually came from here unaltered, rather than trusting the HTTP response wholesale.
    """
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE (expires_at IS NULL OR expires_at > ?) "
            f"AND origin_relay = ? ORDER BY created_at DESC LIMIT {_MAX_LIMIT}",
            (datetime.now(UTC).isoformat(), RELAY_BASE_URL),
        ).fetchall()
    listings = [{**dict(r), "metadata": json.loads(r["metadata_json"])} for r in rows]
    signature = sign_raw_relay(federation_signable_bytes(RELAY_BASE_URL, "listings", listings))
    return {
        "origin_relay": RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
        "listings": listings,
    }


async def _pull_federated_listings(peer_url: str) -> int:
    """Pull listings from a peer relay and upsert locally (federation gossip).

    Rejects the batch unless it carries a valid Ed25519 signature from the peer's own
    relay key (see federated_listings_out()) — an unsigned or badly-signed response is
    trusted no further than any other unauthenticated internet traffic. A peer's key is
    pinned on first successful verification (_FEDERATION_PEER_PUBKEYS) and any later pull
    presenting a different key for the same peer_url is rejected rather than silently
    re-trusted, so a URL/DNS hijack can't swap in a different signer unnoticed.
    """
    peer_url = normalize_peer_url(peer_url)  # N13-2 (R2): one identity for this peer
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{peer_url}/gaming/v1/federate/listings")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": False,
            "imported": 0,
            "error": str(e),
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    origin = data.get("origin_relay", peer_url)
    listings = data.get("listings", [])
    pubkey_hex = data.get("origin_public_key_hex", "")
    signature_hex = data.get("signature", "")

    def _reject(reason: str) -> int:
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": False,
            "imported": 0,
            "error": reason,
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    if not pubkey_hex or not signature_hex:
        return _reject("peer response is unsigned — refusing to import (upgrade the peer relay)")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(signature_hex),
            federation_signable_bytes(origin, "listings", listings),
        )
    except Exception:
        return _reject("signature verification failed — batch may be tampered or corrupt")

    _ensure_federation_pins_loaded()
    pinned = _FEDERATION_PEER_PUBKEYS.get(peer_url)
    if pinned and pinned != pubkey_hex:
        return _reject(
            f"peer key mismatch — pinned {pinned[:12]}..., got {pubkey_hex[:12]}... "
            "(possible URL hijack; remove from FEDERATED_RELAYS and re-add to re-pin deliberately)"
        )
    if pinned is None:
        _FEDERATION_PEER_PUBKEYS[peer_url] = pubkey_hex
        save_federation_peer_pubkeys(_FEDERATION_PEER_PUBKEYS)

    # Skip our own relay to prevent loops. Checked by public key, not just URL — the key is
    # a persistent identity tied to a key file, while RELAY_BASE_URL is an env var an
    # operator can change; a URL-only check would miss "this is actually our own batch,
    # reflected back by a peer" once the URL has drifted from what old listings recorded.
    # Both sides normalized (N13-2, R2) — a trailing-slash mismatch between a peer's
    # self-reported origin_relay and our own RELAY_BASE_URL must not defeat this check.
    if normalize_peer_url(origin) == normalize_peer_url(RELAY_BASE_URL) or pubkey_hex == get_relay_public_key_hex():
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": True,
            "imported": 0,
            "note": "skipped — peer reported our own relay as origin",
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    # N13-3 (R3): cap what we accept, mirroring the LIMIT this relay already applies to
    # what it serves (federated_listings_out). Applied after signature verification
    # (which covers the full, untruncated list) so this never invalidates a genuinely
    # signed batch; only caps the unbounded CPU/DB-write/guardrails-scan (N12-6,
    # including paid NIM calls when enabled) cost of importing it. A peer oversending
    # isn't silently dropped — noted in the sync record below, which
    # GET /federation/peers already surfaces.
    peer_batch_size = len(listings)
    if peer_batch_size > _MAX_LIMIT:
        listings = listings[:_MAX_LIMIT]

    imported = 0
    for listing in listings:
        if not listing.get("listing_id"):
            continue
        try:
            # N12-6: untrusted external content landing in a store the dashboard
            # renders — matches the scan MCP sync_lobby_from_peer already does for
            # challenges. Rejects the individual item, not the whole batch.
            await _assert_payload_safe_counted(listing)
        except Exception:
            continue
        with DB_LOCK, get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO listings "
                "(listing_id, title, description, listing_type, agent_id, payment_mode, "
                "price_usd, module_type, origin_relay, expires_at, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    listing["listing_id"],
                    listing.get("title", ""),
                    listing.get("description", ""),
                    listing.get("listing_type", "service"),
                    listing.get("agent_id", ""),
                    listing.get("payment_mode", "free"),
                    listing.get("price_usd", "0"),
                    listing.get("module_type"),
                    origin,
                    listing.get("expires_at"),
                    json.dumps(listing.get("metadata") or {}),
                    listing.get("created_at", datetime.now(UTC).isoformat()),
                ),
            )
        imported += 1
    sync_record: dict[str, Any] = {
        "ok": True,
        "imported": imported,
        "origin_relay": origin,
        "synced_at": datetime.now(UTC).isoformat(),
    }
    if peer_batch_size > _MAX_LIMIT:
        sync_record["truncated"] = True
        sync_record["peer_batch_size"] = peer_batch_size
    _FEDERATION_LAST_SYNC[peer_url] = sync_record
    return imported


@app.get("/gaming/v1/federate/lobby")
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
        for c in _game_registry.list_challenges("open", origin_relay=None, limit=_MAX_LIMIT)
    ]
    signature = sign_raw_relay(federation_signable_bytes(RELAY_BASE_URL, "challenges", challenges))
    return {
        "origin_relay": RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
        "challenges": challenges,
    }


async def _pull_federated_lobby(peer_url: str) -> int:
    """Pull open lobby challenges from a peer relay and import them locally (signed
    federation gossip) — the inbound half of task #8's fix. Mirrors
    _pull_federated_listings exactly (signature verify, TOFU key pin, mismatch
    rejection, self-origin skip by public key); see that function's docstring for the
    full rationale on each step.
    """
    peer_url = normalize_peer_url(peer_url)  # N13-2 (R2): one identity for this peer
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{peer_url}/gaming/v1/federate/lobby")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        _FEDERATION_LOBBY_LAST_SYNC[peer_url] = {
            "ok": False,
            "imported": 0,
            "error": str(e),
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    origin = data.get("origin_relay", peer_url)
    challenges = data.get("challenges", [])
    pubkey_hex = data.get("origin_public_key_hex", "")
    signature_hex = data.get("signature", "")

    def _reject(reason: str) -> int:
        _FEDERATION_LOBBY_LAST_SYNC[peer_url] = {
            "ok": False,
            "imported": 0,
            "error": reason,
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    if not pubkey_hex or not signature_hex:
        return _reject("peer response is unsigned — refusing to import (upgrade the peer relay)")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(signature_hex),
            federation_signable_bytes(origin, "challenges", challenges),
        )
    except Exception:
        return _reject("signature verification failed — batch may be tampered or corrupt")

    _ensure_federation_pins_loaded()
    pinned = _FEDERATION_PEER_PUBKEYS.get(peer_url)
    if pinned and pinned != pubkey_hex:
        return _reject(
            f"peer key mismatch — pinned {pinned[:12]}..., got {pubkey_hex[:12]}... "
            "(possible URL hijack; remove from FEDERATED_RELAYS and re-add to re-pin deliberately)"
        )
    if pinned is None:
        _FEDERATION_PEER_PUBKEYS[peer_url] = pubkey_hex
        save_federation_peer_pubkeys(_FEDERATION_PEER_PUBKEYS)

    # Skip our own relay to prevent loops — same by-public-key reasoning as
    # _pull_federated_listings (RELAY_BASE_URL is an env var that can drift; the key is
    # a persistent identity tied to a key file). Both sides normalized (N13-2, R2).
    if normalize_peer_url(origin) == normalize_peer_url(RELAY_BASE_URL) or pubkey_hex == get_relay_public_key_hex():
        _FEDERATION_LOBBY_LAST_SYNC[peer_url] = {
            "ok": True,
            "imported": 0,
            "note": "skipped — peer reported our own relay as origin",
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    # N13-3 (R3): cap what we accept, mirroring the LIMIT this relay already applies to
    # what it serves (federated_lobby_out, N12-4). Applied after signature verification
    # (which covers the full, untruncated list — federation_signable_bytes was computed
    # over it above) so this never invalidates a genuinely signed batch; only caps the
    # unbounded CPU/DB-write/guardrails-scan (N12-6, including paid NIM calls when
    # enabled) cost of importing it. A peer oversending isn't silently dropped — noted
    # in the sync record below, which GET /federation/peers already surfaces.
    peer_batch_size = len(challenges)
    if peer_batch_size > _MAX_LIMIT:
        challenges = challenges[:_MAX_LIMIT]

    imported = 0
    for challenge in challenges:
        if not challenge.get("challenge_id"):
            continue
        try:
            # N12-6: this is untrusted external content landing in a store the
            # dashboard renders — MCP sync_lobby_from_peer already scanned each item
            # it imports; this relay-side pull didn't. Rejects the individual item,
            # not the whole batch, same as an id-verification or ownership failure.
            await _assert_payload_safe_counted(challenge)
            _game_registry.import_challenge(challenge, trusted_origin=peer_url)
            imported += 1
        except Exception:
            continue
    sync_record: dict[str, Any] = {
        "ok": True,
        "imported": imported,
        "origin_relay": origin,
        "synced_at": datetime.now(UTC).isoformat(),
    }
    if peer_batch_size > _MAX_LIMIT:
        sync_record["truncated"] = True
        sync_record["peer_batch_size"] = peer_batch_size
    _FEDERATION_LOBBY_LAST_SYNC[peer_url] = sync_record
    return imported


@app.post("/gaming/v1/federate/sync")
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
    caller = _dashboard_agent_id(request)
    _require_operator(caller, "Federation sync")
    results = {}
    lobby_results = {}
    for peer in FEDERATED_RELAYS:
        results[peer] = await _pull_federated_listings(peer)
        lobby_results[peer] = await _pull_federated_lobby(peer)
    return {"synced": results, "synced_lobby": lobby_results}


@app.get("/federation/peers")
async def federation_peers():
    """Configured federation peers + last gossip-sync outcome, for the dashboard."""
    _ensure_federation_pins_loaded()
    with DB_LOCK, get_db() as conn:
        imported_row = conn.execute(
            "SELECT COUNT(*) AS c FROM listings WHERE origin_relay IS NOT NULL "
            "AND origin_relay != ?",
            (RELAY_BASE_URL,),
        ).fetchone()
    return {
        "relay_url": RELAY_BASE_URL,
        "standalone": not FEDERATED_RELAYS,
        "peers": [
            {
                "url": peer,
                "pinned_public_key_hex": _FEDERATION_PEER_PUBKEYS.get(peer),
                **_FEDERATION_LAST_SYNC.get(peer, {"ok": None, "imported": 0}),
                "lobby_sync": _FEDERATION_LOBBY_LAST_SYNC.get(peer, {"ok": None, "imported": 0}),
            }
            for peer in FEDERATED_RELAYS
        ],
        "imported_listing_count": imported_row["c"] if imported_row else 0,
    }


# ============================================================================
# Sessions
# ============================================================================

@app.post("/sessions")
async def create_session(req: CreateSessionRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    if req.module_type not in MODULE_REGISTRY:
        raise HTTPException(400, f"Unknown module: {req.module_type}")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)

    module = get_interaction_module(req.module_type)
    payment_rules = req.payment_rules or module.PAYMENT_RULES.model_dump()
    if req.payment_rules:
        payment_rules = {**module.PAYMENT_RULES.model_dump(), **req.payment_rules}

    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    state = module.initial_state([caller], req.config)

    save_session(
        session_id=session_id,
        module_type=req.module_type,
        config=req.config,
        payment_rules=payment_rules,
        state=state,
        current_agent=state.current_agent,
        step_number=0,
        status="waiting",
        participants=[caller],
        created_at=now,
        updated_at=now,
    )
    add_participant(session_id, caller, stake_paid=False)
    # Creator joins free; challenger pays — intentional design
    open_dual_session(session_id, caller, req.module_type)

    await broadcast_to_agent(caller, {
        "type": "session_created",
        "session_id": session_id,
        "module_type": req.module_type,
    })
    session = load_session(session_id)
    if _session_visibility(session) == "public":
        await broadcast_global({
            "type": "session_created",
            "session_id": session_id,
            "module_type": req.module_type,
            "creator_agent_id": caller,
        })
    return session


@app.get("/sessions")
async def list_sessions(request: Request, status: str | None = None, module_type: str | None = None):
    with DB_LOCK, get_db() as conn:
        q = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if module_type:
            q += " AND module_type = ?"
            params.append(module_type)
        q += " ORDER BY created_at DESC LIMIT 100"
        rows = conn.execute(q, params).fetchall()
    caller = _dashboard_agent_id(request)
    sessions = [_session_row_to_dict(r) for r in rows]
    return {"sessions": [s for s in sessions if _session_viewer_allowed(s, caller)]}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not _session_viewer_allowed(s, _dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    return s


@app.get("/sessions/{session_id}/actions")
async def get_session_actions(session_id: str, request: Request):
    """Return all recorded actions for a session — used for board replay."""
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not _session_viewer_allowed(s, _dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT action_id, agent_id, action_type, payload_json, result_json, created_at "
            "FROM actions WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
    return {
        "session_id": session_id,
        "actions": [
            {
                "action_id": r["action_id"],
                "agent_id": r["agent_id"],
                "action_type": r["action_type"],
                "payload": json.loads(r["payload_json"]),
                "result": json.loads(r["result_json"]) if r["result_json"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


@app.get("/sessions/{session_id}/audit")
async def get_session_audit(session_id: str, request: Request):
    """Public hash-chained receipt log + chain-integrity verification for a session.

    Surfaces the tamper-evident audit trail (session_audit.py) to the dashboard so
    judges can see the "verified" badge, not just trust that hashing happens.

    "Public" is the chain-verification model (anyone with the log can recompute the
    hashes), not the access model — the log itself contains the full action payload
    (session_audit.py's log_public_receipt()), so this endpoint is gated the same as
    /sessions/{id}/actions rather than actually being open to anyone.
    """
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not _session_viewer_allowed(s, _dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    ok, message = verify_chain(session_id)
    return {
        "session_id": session_id,
        "verified": ok,
        "message": message,
        "chain": get_public_log(session_id),
    }


@app.post("/sessions/{session_id}/join")
async def join_session(session_id: str, req: JoinSessionRequest, request: Request):
    identity_verified = _require_caller_unless_mpp_payment(req.agent_id, request)
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != "waiting":
        raise HTTPException(400, f"Session not joinable: {s['status']}")
    if req.agent_id in s["participants"]:
        raise HTTPException(400, "Already joined")
    _require_registered(req.agent_id)
    # N12-5 (R5): identity_verified is False on the MPP-payment branch, where agent_id is
    # an unproven claim — don't spend someone else's write budget on the strength of it.
    if identity_verified:
        _enforce_agent_rate_limit(req.agent_id)

    module = get_interaction_module(s["module_type"])
    valid, err = module.validate_participants(s["participants"] + [req.agent_id])
    if not valid:
        raise HTTPException(400, err)

    # Stripe payment gate (challenger pays; creator is already in free)
    payment_rules = s["payment_rules"]
    mode = payment_rules.get("mode", "free")
    stake = payment_rules.get("stake_per_participant", "0")

    stake_paid = False
    if mode != "free" and stake not in ("0", "0.00", ""):
        amount_cents = int(float(stake) * 100) if stake else 0
        currency = (payment_rules.get("currency") or "usd").lower()

        result = await _verify_and_confirm_payment(
            request,
            agent_id=req.agent_id,
            identity_verified=identity_verified,
            amount_cents=amount_cents,
            currency=currency,
            resource_type="session",
            resource_id=session_id,
            service_type=s["module_type"],
            capture_method="manual",
            allowed_statuses=("requires_capture",),
            req_payment_intent_id=req.payment_intent_id,
            challenge_message=(
                f"Session requires {amount_cents} {currency} cents stake. "
                "Retry with Authorization: Payment <spt_token> "
                "or POST to /payments/create-intent first."
            ),
            challenge_resource=f"emporia:session:{session_id}",
        )
        if isinstance(result, JSONResponse):
            return result
        payment_ref = result
        req.payment_intent_id = payment_ref.reference
        stake_paid = True

        # N4 review R1: record_payment() raises ValueError on a PaymentIntent id already
        # recorded for a different payment (different agent/session/amount) — a replay
        # attempt, not an internal error. Must surface as a rejected join, not a 500.
        try:
            record_payment(
                payment_intent_id=req.payment_intent_id,
                agent_id=req.agent_id,
                amount_cents=amount_cents,
                payment_type="session_stake",
                session_id=session_id,
                currency=currency,
                rail=payment_ref.rail,
            )
        except ValueError as e:
            raise HTTPException(402, str(e))

    participants = s["participants"] + [req.agent_id]
    add_participant(session_id, req.agent_id, stake_paid=stake_paid)

    if len(participants) >= module.MIN_PARTICIPANTS:
        state = module.initial_state(participants, s["config"])
        save_session(
            session_id=session_id,
            module_type=s["module_type"],
            config=s["config"],
            payment_rules=payment_rules,
            state=state,
            current_agent=state.current_agent,
            step_number=0,
            status="active",
            participants=participants,
            created_at=s["created_at"],
            updated_at=datetime.now(UTC).isoformat(),
        )
        for p in participants:
            await broadcast_to_agent(p, {
                "type": "session_started",
                "session_id": session_id,
                "participants": participants,
            })
    else:
        save_session(
            session_id=session_id,
            module_type=s["module_type"],
            config=s["config"],
            payment_rules=payment_rules,
            state=SessionState.from_dict(s["state"]),
            current_agent=s["current_agent"],
            step_number=s["step_number"],
            status="waiting",
            participants=participants,
            created_at=s["created_at"],
            updated_at=datetime.now(UTC).isoformat(),
        )
        await broadcast_to_session(session_id, {
            "type": "participant_joined",
            "agent_id": req.agent_id,
        })

    return load_session(session_id)


@app.post("/sessions/{session_id}/action")
async def submit_action(session_id: str, req: SubmitActionRequest):
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != "active":
        raise HTTPException(400, f"Session not active: {s['status']}")
    if req.agent_id != s["current_agent"]:
        raise HTTPException(400, "Not your turn")
    if req.agent_id not in s["participants"]:
        raise HTTPException(400, "Not a participant")

    full_payload = {
        "session_id": session_id,
        "step_number": s["step_number"],
        "action_type": req.action_type,
        "payload": req.payload,
        "agent_id": req.agent_id,
        "peer_text_rationale": req.peer_text_rationale,
    }

    # 1. Guardrails (regex firewall + optional NVIDIA NIM semantic check)
    try:
        await _assert_payload_safe_counted(full_payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    # 2. Ed25519 signature verify — mandatory. The signature binds the action to
    # this exact session_id + step_number, so a captured signature can't be
    # replayed against a different session or an earlier/later turn.
    if not req.signature:
        SAFETY_STATS["unsigned_actions_rejected"] += 1
        raise HTTPException(401, "Signature required for session actions")
    if not AGENT_REGISTRY.verify_signature(req.agent_id, full_payload, req.signature):
        SAFETY_STATS["unsigned_actions_rejected"] += 1
        raise HTTPException(403, "Invalid signature")
    # Rate-limit after signature verification, not before — keying on req.agent_id
    # pre-signature would let a forged, unsigned request burn any agent's rate-limit
    # budget with no credential at all.
    _enforce_agent_rate_limit(req.agent_id)

    # 3. Stripe payment gate handled at join; per-turn PoR gate next

    # 4. Proof-of-Reasoning density + bot fingerprint
    try:
        validate_proof_of_reasoning(full_payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    # 5. Audit log
    log_private_event(session_id, req.agent_id, "action", full_payload)

    # 6. Module dispatch
    module = get_interaction_module(s["module_type"])
    state = SessionState.from_dict(s["state"])
    action = SessionAction(
        agent_id=req.agent_id,
        action_type=req.action_type,
        payload=req.payload,
    )
    valid, err = module.validate_action(state, action)
    if not valid:
        raise HTTPException(400, err)

    result = module.apply_action(state, action)
    if not result.success:
        raise HTTPException(400, result.error or "Action failed")

    new_state = result.new_state
    is_over, outcome = module.is_terminal(new_state)
    new_status = "completed" if is_over else "active"

    save_session(
        session_id=session_id,
        module_type=s["module_type"],
        config=s["config"],
        payment_rules=s["payment_rules"],
        state=new_state,
        current_agent=new_state.current_agent,
        step_number=s["step_number"] + 1,
        status=new_status,
        participants=s["participants"],
        created_at=s["created_at"],
        updated_at=datetime.now(UTC).isoformat(),
    )
    record_action(session_id, req.agent_id, req.action_type, req.payload, {
        "success": result.success,
        "artifacts": result.artifacts,
        "new_state": result.new_state.data if result.new_state else None,
    })

    # Public audit receipt
    if req.signature:
        log_public_receipt(session_id, req.agent_id, req.action_type, req.payload, req.signature)

    action_event: dict[str, Any] = {
        "type": "action_result",
        "session_id": session_id,
        "agent_id": req.agent_id,
        "action_type": req.action_type,
        "payload": req.payload,
        "success": True,
        "artifacts": result.artifacts,
        "new_state": result.new_state.data if result.new_state else None,
    }
    await broadcast_to_session(session_id, action_event)
    if _session_visibility(s) == "public":
        await broadcast_global(action_event)

    settlement_record: dict[str, Any] | None = None
    if is_over:
        winner_id = outcome.get("winner") if isinstance(outcome, dict) else None
        outcome_type = outcome.get("outcome_type", "won") if isinstance(outcome, dict) else "won"
        payments = get_session_payments(session_id)
        pi_ids = [p["payment_intent_id"] for p in payments]
        total_cents = sum(p["amount_cents"] for p in payments)

        if outcome_type == "refund":
            # Dispute or timeout: cancel all authorized holds — no one is charged.
            from emporia.payments import cancel_payment_hold
            cancel_results = []
            for pi_id in pi_ids:
                if pi_id:
                    try:
                        cancel_results.append(await cancel_payment_hold(pi_id))
                    except Exception as e:
                        cancel_results.append({"payment_intent_id": pi_id, "error": str(e)})
            settlement_record = {
                "session_id": session_id,
                "winner_id": winner_id,
                "outcome_type": "refund",
                "status": "refunded",
                "cancel_results": cancel_results,
                "total_stake_cents": total_cents,
                "platform_fee_cents": 0,
                "winner_payout_cents": 0,
                "payment_intent_ids": pi_ids,
                "transfer_id": None,
                "transfer_status": "refunded",
            }
            record_settlement(settlement_record)
        elif winner_id:
            if total_cents:
                # Paid session: capture all escrowed holds, transfer to winner.
                from emporia.payments import settle
                winner_stripe_account = AGENT_REGISTRY.get_stripe_account(winner_id)
                settlement_record = await settle(
                    session_id, winner_id, total_cents, pi_ids,
                    winner_stripe_account=winner_stripe_account,
                )
            else:
                # Free session: record outcome with zero financials.
                settlement_record = {
                    "session_id": session_id,
                    "winner_id": winner_id,
                    "outcome_type": outcome_type,
                    "total_stake_cents": 0,
                    "platform_fee_cents": 0,
                    "winner_payout_cents": 0,
                    "platform_fee_bps": OPERATOR_FEE_BPS,
                    "payment_intent_ids": [],
                    "capture_results": [],
                    "transfer_id": None,
                    "transfer_status": "no_payment",
                    "status": "settled",
                }
            record_settlement(settlement_record)

        terminal_event: dict[str, Any] = {
            "type": "session_completed",
            "session_id": session_id,
            "outcome": outcome,
            "settlement": settlement_record,
        }
        await broadcast_to_session(session_id, terminal_event)
        if _session_visibility(s) == "public":
            await broadcast_global(terminal_event)

    return {
        "success": True,
        "session_id": session_id,
        "step_number": s["step_number"] + 1,
        "is_terminal": is_over,
        "outcome": outcome if is_over else None,
        "settlement": settlement_record,
        "artifacts": result.artifacts,
    }


@app.get("/sessions/{session_id}/state")
async def get_session_state(session_id: str, request: Request):
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not _session_viewer_allowed(s, _dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    return s


class ConfirmDeliveryRequest(BaseModel):
    agent_id: str
    rationale: str = "Delivery confirmed as satisfactory."
    signature: str | None = None


class DisputeDeliveryRequest(BaseModel):
    agent_id: str
    reason: str
    rationale: str = ""
    signature: str | None = None


@app.post("/sessions/{session_id}/confirm-delivery")
async def confirm_delivery(session_id: str, req: ConfirmDeliveryRequest, request: Request):
    """Buyer confirms service delivery. Triggers settlement: seller receives payment.

    Shorthand for submitting action_type='confirm' to an emporia:service:v1 session.
    Works on any module that accepts a 'confirm' action from the non-current participant.
    """
    return await submit_action(
        session_id=session_id,
        req=SubmitActionRequest(
            agent_id=req.agent_id,
            action_type="confirm",
            payload={},
            peer_text_rationale=req.rationale or "Buyer confirmed delivery.",
            signature=req.signature,
        ),
    )


@app.post("/sessions/{session_id}/dispute-delivery")
async def dispute_delivery(session_id: str, req: DisputeDeliveryRequest, request: Request):
    """Buyer disputes delivery. Triggers refund: all escrowed holds released.

    Shorthand for submitting action_type='dispute' to an emporia:service:v1 session.
    """
    return await submit_action(
        session_id=session_id,
        req=SubmitActionRequest(
            agent_id=req.agent_id,
            action_type="dispute",
            payload={"reason": req.reason},
            peer_text_rationale=req.rationale or f"Dispute: {req.reason}",
            signature=req.signature,
        ),
    )


@app.post("/sessions/{session_id}/abandon")
async def abandon_session(session_id: str, request: Request, req: dict = None):
    """Mark a session abandoned and release all escrowed stake holds.

    Call this when a session cannot complete (both agents disconnected, timeout,
    mutual agreement). The relay cancels all authorized PaymentIntent holds so
    both agents are fully refunded. Already-captured PIs cannot be released here —
    use /payments/refund for those (handled via arbitrate_and_refund).
    """
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    caller = _dashboard_agent_id(request)
    owner = _relay_operator_id()
    if not caller or (caller not in s["participants"] and caller != owner):
        raise HTTPException(403, "Only a participant or the relay operator may abandon this session")
    _enforce_agent_rate_limit(caller)
    if s["status"] == "complete":
        raise HTTPException(400, "Session already complete — cannot abandon")

    # Cancel all authorized holds for this session
    payments = get_session_payments(session_id)
    from emporia.payments import cancel_payment_hold
    cancel_results = []
    for p in payments:
        pi_id = p.get("payment_intent_id")
        if pi_id:
            try:
                result = await cancel_payment_hold(pi_id)
                cancel_results.append(result)
            except Exception as e:
                cancel_results.append({"payment_intent_id": pi_id, "error": str(e)})

    with get_db() as db:
        db.execute(
            "UPDATE sessions SET status='abandoned', updated_at=? WHERE session_id=?",
            (datetime.now(UTC).isoformat(), session_id),
        )

    await broadcast_to_session(session_id, {
        "type": "session_abandoned",
        "session_id": session_id,
        "holds_released": len(cancel_results),
        "cancel_results": cancel_results,
    })

    return {
        "status": "abandoned",
        "session_id": session_id,
        "holds_released": cancel_results,
        "message": "Authorized stakes released. Agents will see refunds within 5–7 days.",
    }


# ============================================================================
# Messaging / Negotiation
# ============================================================================

VALID_MSG_TYPES = {"challenge", "counter_offer", "accept", "reject", "chat", "rpc", "notification"}


@app.post("/messages")
async def send_message(req: SendMessageRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    if not req.signature:
        raise HTTPException(401, "Signature required")
    # N3-8 (R7): bound to the routing envelope (to_agent/type/session_id), not just
    # payload — a payload-only signature was valid for delivery to *any* recipient/type,
    # since those fields sit outside what was signed yet drive routing (:3339 below).
    signed_envelope = {
        "to_agent": req.to_agent, "type": req.type,
        "session_id": req.session_id, "payload": req.payload,
    }
    if not AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid signature")

    try:
        await _assert_payload_safe_counted(req.payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    msg_id = AGENT_REGISTRY.log_message(
        from_agent=caller,
        to_agent=req.to_agent,
        session_id=req.session_id,
        msg_type=req.type,
        payload=req.payload,
        signature=req.signature,
    )
    await broadcast_to_agent(req.to_agent, {
        "type": req.type,
        "from_agent": caller,
        "session_id": req.session_id,
        "payload": req.payload,
        "message_id": msg_id,
    })
    return {"status": "sent", "message_id": msg_id}


@app.get("/messages")
async def get_messages(agent_id: str, request: Request, session_id: str | None = None, limit: int = 50):
    """Deprecated history view — use GET /inbox for live events, GET /dm for peer chat.
    Still returns the raw messages audit table for back-compat.
    """
    _require_caller_is(agent_id, request)
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM messages WHERE (to_agent = ? OR from_agent = ?) "
                "AND session_id = ? ORDER BY created_at DESC LIMIT ?",
                (agent_id, agent_id, session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_agent = ? OR from_agent = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, agent_id, limit),
            ).fetchall()
    return {
        "deprecated": "Use GET /inbox (events) or GET /dm (peer chat). This endpoint reads the raw audit table.",
        "messages": [
            {**dict(r), "payload": json.loads(r["payload_json"])}
            for r in rows
        ],
    }


# ============================================================================
# WebSocket
# ============================================================================
# /ws/events must be registered before /ws/{session_id}: Starlette matches routes in
# registration order, not by specificity, and both patterns have one path segment after
# "/ws/", so /ws/events previously fell through to /ws/{session_id} with session_id="events"
# and was completely unreachable (N3-5 review finding — this is what "no test covers
# /ws/events" actually meant: the dashboard's live global event stream has never worked).

@app.websocket("/ws/events")
async def ws_global_events(ws: WebSocket):
    """Global event stream for the operator dashboard.

    Clients receive a JSON frame for every relay-side event:
      {"type": "session_created", ...}
      {"type": "session_action",  ...}
      {"type": "listing_created", ...}
      {"type": "room_created",    ...}
      {"type": "room_message",    ...}
      {"type": "event_created",   ...}
    Send {"type": "ping"} to keep alive; relay responds {"type": "pong"}.
    """
    await ws.accept()
    ws_connections["__global__"].add(ws)
    try:
        # Send a snapshot on connect
        health_data = {
            "status": "ok",
            "service": "Emporia",
            "version": "0.1.0",
            "listing_count": 0,
            "session_count": 0,
            "modules": list(MODULE_REGISTRY.keys()),
        }
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute("SELECT COUNT(*) AS c FROM listings WHERE status='open'").fetchone()
                health_data["listing_count"] = row["c"] if row else 0
                row2 = conn.execute("SELECT COUNT(*) AS c FROM sessions WHERE status NOT IN ('complete','cancelled')").fetchone()
                health_data["session_count"] = row2["c"] if row2 else 0
        except Exception:
            pass
        await ws.send_json({"type": "connected", "health": health_data})
        while True:
            data = await ws.receive_text()
            try:
                frame = json.loads(data)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections["__global__"].discard(ws)


@app.websocket("/ws/{session_id}")
async def ws_session(ws: WebSocket, session_id: str):
    """Live session feed. Gated the same way as GET /sessions/{id}/actions
    (_session_viewer_allowed()) — public sessions (default, e.g. chess spectator/replay)
    stay open to any connection; participants-only sessions (paid modules by default)
    require the caller to be a participant or the operator.

    N4-5 (N4 review R7): used to fail OPEN on an unknown session_id — `if s and not
    _session_viewer_allowed(...)` skipped the gate entirely when `s` was falsy, so an
    unrecognized id was accepted and registered into ws_connections[session_id], where
    it would receive anything later broadcast to that key. ws_room already rejects an
    unknown room_id; this now matches that fail-closed shape.
    """
    s = load_session(session_id)
    if not s:
        await ws.close(code=1008)
        return
    if not _session_viewer_allowed(s, _ws_agent_id(ws)):
        await ws.close(code=1008)
        return
    await ws.accept()
    ws_connections[session_id].add(ws)
    try:
        await ws.send_json({"type": "init", "session": s})
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections[session_id].discard(ws)


@app.websocket("/ws/agent/{agent_id}")
async def ws_agent(ws: WebSocket, agent_id: str):
    """Per-agent notification channel — private. Caller must be the agent itself (or the
    relay operator); see _ws_agent_id()."""
    caller = _ws_agent_id(ws)
    owner = _relay_operator_id()
    if caller != agent_id and not (owner and caller == owner):
        await ws.close(code=1008)
        return
    await ws.accept()
    key = f"agent:{agent_id}"
    ws_connections[key].add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections[key].discard(ws)


# ============================================================================
# Lobby (gaming challenge discovery)
# ============================================================================

_game_registry = GameRegistry(DATABASE_PATH)


@app.get("/gaming/lobby")
async def gaming_lobby_get(status: str = "open", game_type: str | None = None):
    challenges = [c.to_dict() for c in _game_registry.list_challenges(status)]
    if game_type:
        challenges = [c for c in challenges if c.get("game_type") == game_type]
    return {
        "ok": True,
        "brand": BRAND,
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


@app.post("/gaming/lobby")
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
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    payload = req.model_dump()
    try:
        await _assert_payload_safe_counted(payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        imported = _game_registry.import_challenge(payload)
        return {"ok": True, "challenge": imported.to_dict()}
    except ChallengeOwnershipError as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(422, str(e))


# ============================================================================
# Rooms
# Public: open to all registered agents.
# Private: hidden from listing; gated by invite OR Stripe payment.
# Encryption flag: relay stores opaque ciphertext, skips guardrails scan.
# Negotiation routing: counter_offer in a 2-member private room is brokered.
# Session link: a game session can have an associated room for in-session chat.
# ============================================================================

@app.post("/rooms")
async def create_room_endpoint(req: CreateRoomRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
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
    if req.linked_session_id:
        s = load_session(req.linked_session_id)
        if not s:
            raise HTTPException(404, f"Session not found: {req.linked_session_id}")
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
            access_duration_days=req.access_duration_days,
            db_path=DATABASE_PATH,
        )
        if req.room_type == "public":
            await broadcast_global({
                "type": "room_created",
                "room_id": room.room_id,
                "name": room.name,
                "creator_id": room.creator_id,
            })
        return room.to_dict(viewer_id=caller, db_path=DATABASE_PATH)
    except Exception as e:
        raise HTTPException(422, str(e))


@app.get("/rooms")
async def list_rooms_endpoint(
    request: Request,
    room_type: str | None = None,
    limit: int = 50,
):
    viewer_id = _dashboard_agent_id(request)
    room_list = rooms_db.list_rooms(
        viewer_id=viewer_id,
        room_type=room_type,
        limit=_clamp_limit(limit),
        db_path=DATABASE_PATH,
    )
    return {
        "rooms": [r.to_dict(viewer_id=viewer_id, db_path=DATABASE_PATH) for r in room_list],
        "count": len(room_list),
    }


@app.get("/rooms/{room_id}")
async def get_room_endpoint(room_id: str, request: Request):
    viewer_id = _dashboard_agent_id(request)
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    return room.to_dict(viewer_id=viewer_id, db_path=DATABASE_PATH)


async def _settle_access_payment(
    *,
    resource_type: str,
    resource_id: str,
    outcome_type: str,
    payer_id: str,
    creator_id: str,
    amount_cents: int,
    payment_ref: "PaymentRef",
    access_duration_days: int | None,
) -> None:
    """P3 (item 4): the pay-to-access settlement/payout seam, shared by
    join_room() and subscribe_agora_topic() — before this they were
    near-identical copy-pasted blocks (same fee split, same Connect account
    lookup, same try/except around payout_winner, same record_settlement
    shape; the only differences were cosmetic — kwargs vs. positional args
    into payout_winner — and the resource ids/outcome_type strings).

    Records the entitlement this payment just bought (P2-4), before the
    payout — mirrors record_payment's placement relative to payout for the
    same "commit the durable record before the non-idempotent Stripe call"
    reason both join_room and subscribe_agora_topic already followed.

    The seam is drawn where custody actually splits, not as a dispatch
    table: recording the settlement (rail-agnostic, always runs) is
    structurally separate from the platform→creator transfer hop
    (Stripe-only, skipped via the `if creator_stripe:` guard and the
    "no_stripe_account" sentinel below). Per the non-custodial x402 decision
    (ROADMAP.md § Payments), `payout_winner()` has no counterpart at all on
    a rail where the payment itself is the settlement — a real second rail
    would skip this transfer entirely, not call into a different transfer
    implementation, so there is nothing to dispatch to yet. See
    payments.KNOWN_RAILS's own docstring for why a speculative registry
    entry isn't here either.

    settle() (session escrow, capture-then-transfer) is deliberately not
    routed through here — escrow has no x402 analogue and stays Stripe-only
    (ROADMAP.md § Payments), and its own "pending_connect" initial sentinel
    stays distinct from this function's "no_stripe_account" for the same
    condition; unifying the two is a behavior change, not this pass's job.

    L2: `from emporia.payments import ...` stays function-local here, same
    as the two call sites it replaces — 75 test
    `@patch("emporia.payments.X")` decorators only intercept because nothing
    in this file imports these names at module scope.

    P1b-6: `payment_ref.rail == RAIL_X402` is the "a real second rail would
    skip this transfer entirely" case the docstring above already
    anticipated — x402 is non-custodial (the payment settles straight to
    `payTo`, resolved and asserted in _verify_x402_payment() before this
    function is ever reached), so there is no platform-held balance to take
    a cut from or transfer out of. The platform fee is 0% on x402 for now —
    a provisional decision, not a permanent one (see ROADMAP.md § Payments
    for the revisit trigger: a cross-chain splitter mechanism). The Stripe
    branch below is untouched, byte-for-byte.
    """
    from emporia.payments import (
        RAIL_X402, payout_winner, platform_fee as _platform_fee, settlement_dedupe_key,
    )

    entitlements.grant(
        resource_type, resource_id, payer_id, payment_ref.reference,
        access_duration_days, rail=payment_ref.rail, db_path=DATABASE_PATH,
    )
    if payment_ref.rail == RAIL_X402:
        platform_fee = 0
        creator_payout = amount_cents
        platform_fee_bps = 0
        transfer_id = None
        # "direct": no platform-held balance ever existed to transfer out of
        # — distinct from "no_stripe_account" (Stripe, platform holds funds,
        # simply has nowhere to send them) and "transferred"/"transfer_failed"
        # (Stripe, a transfer was attempted). Not a failure state.
        transfer_status = "direct"
    else:
        platform_fee_bps = OPERATOR_FEE_BPS
        platform_fee = _platform_fee(amount_cents)
        creator_payout = amount_cents - platform_fee
        transfer_id = None
        transfer_status = "no_stripe_account"
        if creator_payout > 0:
            creator_stripe = AGENT_REGISTRY.get_stripe_account(creator_id)
            if creator_stripe:
                try:
                    t = await payout_winner(
                        winner_id=creator_id,
                        stripe_account_id=creator_stripe,
                        amount_cents=creator_payout,
                        session_id=resource_id,
                        # Matches this same settlement's dedupe_key below — same
                        # helper, same inputs, by construction (P1r-2, R1). L1:
                        # only the bare reference reaches the hash, never the rail.
                        idempotency_key="emporia:payout:" + settlement_dedupe_key(
                            resource_id, payer_id, [payment_ref.reference]
                        ),
                    )
                    transfer_id = t["transfer_id"]
                    transfer_status = "transferred"
                except Exception:
                    transfer_status = "transfer_failed"
    record_settlement({
        "session_id": resource_id,
        "winner_id": creator_id,
        "outcome_type": outcome_type,
        "status": "settled",
        "total_stake_cents": amount_cents,
        "platform_fee_cents": platform_fee,
        "winner_payout_cents": creator_payout,
        "platform_fee_bps": platform_fee_bps,
        "payment_intent_ids": [payment_ref.reference],
        "transfer_id": transfer_id,
        "transfer_status": transfer_status,
        "payer_id": payer_id,
        "rail": payment_ref.rail,
    })


@app.post("/rooms/{room_id}/join")
async def join_room(room_id: str, req: JoinRoomRequest, request: Request):
    identity_verified = _require_caller_unless_mpp_payment(req.agent_id, request)
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    _require_registered(req.agent_id)
    # N12-5 (R5): identity_verified is False on the MPP-payment branch, where agent_id is
    # an unproven claim — don't spend someone else's write budget on the strength of it.
    if identity_verified:
        _enforce_agent_rate_limit(req.agent_id)
    if rooms_db.is_member(room_id, req.agent_id, db_path=DATABASE_PATH):
        return {"status": "already_member", "room_id": room_id}
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
                                db_path=DATABASE_PATH):
        raise HTTPException(403, "Removed from this room")

    confirmed_pi = None  # PaymentRef | None — imported function-local, see L2
    amount_cents = 0
    currency = "usd"
    if room.gate_type == "invite":
        if not rooms_db.has_invite(room_id, req.agent_id, db_path=DATABASE_PATH):
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
        if not entitlements.has_access("room", room_id, req.agent_id, db_path=DATABASE_PATH):
            result = await _verify_and_confirm_payment(
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
            )
            if isinstance(result, JSONResponse):
                return result
            confirmed_pi = result

            # N4 review R1: record_payment() raises ValueError on a replayed PaymentIntent
            # id (already recorded for a different agent/room/amount) — surface as a
            # rejected join, not a 500.
            try:
                record_payment(
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
    # record_payment (above) is genuinely idempotent on a replayed PaymentIntent, but
    # payout_winner is a real, non-idempotent Stripe transfer — without this ordering
    # a retry after the payout succeeded but before this response returned would
    # replay record_payment idempotently (no error) and fire a second transfer.
    try:
        rooms_db.add_member(room_id, req.agent_id, db_path=DATABASE_PATH)
    except ValueError as e:
        raise HTTPException(403, str(e))

    if room.gate_type == "paid" and confirmed_pi:
        await _settle_access_payment(
            resource_type="room",
            resource_id=room_id,
            outcome_type="room_entry",
            payer_id=req.agent_id,
            creator_id=room.creator_id,
            amount_cents=amount_cents,
            payment_ref=confirmed_pi,
            access_duration_days=room.access_duration_days,
        )

    await broadcast_to_session(f"room:{room_id}", {
        "type": "member_joined",
        "room_id": room_id,
        "agent_id": req.agent_id,
    })
    return {"status": "joined", "room_id": room_id}


@app.post("/rooms/{room_id}/invite")
async def invite_to_room(room_id: str, req: InviteToRoomRequest, request: Request):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    _require_caller_is(room.creator_id, request)
    _enforce_agent_rate_limit(room.creator_id)
    if room.gate_type == "open":
        raise HTTPException(400, "Open rooms don't use invites")
    _require_registered(req.invitee_id)
    rooms_db.add_invite(room_id, req.invitee_id, room.creator_id, db_path=DATABASE_PATH)
    await broadcast_to_agent(req.invitee_id, {
        "type": "room_invite",
        "room_id": room_id,
        "room_name": room.name,
        "invited_by": room.creator_id,
    })
    return {"status": "invited", "room_id": room_id, "invitee_id": req.invitee_id}


async def _refund_kicked_room_payer(room_id: str, agent_id: str) -> dict[str, Any]:
    """P2-7: reverse-then-refund a kicked payer's room entry fee, when the
    kicker asks for it.

    A room entry fee moves in two hops by the time of a kick: the payer's
    charge landed in the platform balance at join time, then payout_winner()
    already transferred the creator's net share (97.5%) out to their
    connected account. Refunding the original charge without first clawing
    that transfer back would leave the platform eating the creator's share —
    so the order here undoes both hops in reverse: reverse the transfer,
    *then* refund the charge. Net effect: the payer is made whole, the
    creator's payout is reversed, and the platform's 2.5% fee is simply never
    kept — no separate "give back the fee" step, since reversing 100% of the
    transfer plus refunding 100% of the charge already nets to zero for the
    platform.

    Looks up the *newest* settlement row for this (room, payer) pair — after
    P2-4 there can be more than one if the payer was kicked, repurchased, and
    is being kicked again; the most recent purchase is the one this kick is
    actually undoing.

    Returns an outcome dict rather than raising: a kick has already succeeded
    by the time this runs, and a Stripe failure here must not read as the
    kick itself having failed.

    P2r-3 (R3): every real-money outcome here is persisted before returning —
    this used to only ever *read* the settlement row and call Stripe, so
    afterward `transfer_status` still said `'transferred'`,
    `payments.status` still said `'confirmed'`, and neither the refund nor
    the reversal id was stored anywhere. Every settlement read path (the
    payer's own receipt, the creator's, the operator's) served a record that
    contradicted what actually happened at Stripe. A reversal that succeeds
    followed by a refund that fails is its own persisted state
    (`'reversed_pending_refund'`) rather than being indistinguishable from
    "nothing happened" — an operator needs to see that a creator's payout was
    already clawed back even though the payer hasn't been made whole yet.

    S1: the local write above can itself fail (lock contention, disk full)
    after Stripe has already moved real money -- that must not read as the
    kick having failed. Each write is independently wrapped; on write
    failure the real Stripe outcome is still returned, with `persisted:
    False` added so an operator can find and reconcile it, instead of the
    exception propagating into a 500 for an operation that actually
    succeeded.
    """
    from emporia.payments import reverse_transfer, refund_payment_intent, settlement_dedupe_key

    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT settlement_id, payment_intent_ids, transfer_id, transfer_status "
            "FROM settlements WHERE session_id=? AND payer_id=? ORDER BY created_at DESC LIMIT 1",
            (room_id, agent_id),
        ).fetchone()
    pi_ids = json.loads(row["payment_intent_ids"] or "[]") if row else []
    if not row or not pi_ids:
        return {"refunded": False, "reason": "no_payment_found"}

    settlement_id = row["settlement_id"]
    payment_intent_id = pi_ids[0]
    dedupe_key = settlement_dedupe_key(room_id, agent_id, pi_ids)

    reversal_id = None
    if row["transfer_id"] and row["transfer_status"] == "transferred":
        try:
            reversal = await reverse_transfer(
                row["transfer_id"], idempotency_key="emporia:reversal:" + dedupe_key
            )
            reversal_id = reversal["reversal_id"]
        except Exception as e:
            # Nothing changed at Stripe — nothing to persist.
            return {"refunded": False, "reason": f"transfer_reversal_failed: {e}"}

    try:
        refund = await refund_payment_intent(
            payment_intent_id, idempotency_key="emporia:refund:" + dedupe_key
        )
    except Exception as e:
        if reversal_id:
            try:
                with DB_LOCK, get_db() as conn:
                    conn.execute(
                        "UPDATE settlements SET transfer_status='reversed_pending_refund', "
                        "reversal_id=? WHERE settlement_id=?",
                        (reversal_id, settlement_id),
                    )
            except Exception as persist_exc:
                # S1: Stripe's reversal already happened — a local write failure
                # on top of it must still report the reversal, not 500 and hide it.
                print(
                    f"_refund_kicked_room_payer: failed to persist "
                    f"reversed_pending_refund for settlement {settlement_id}: {persist_exc}"
                )
                return {
                    "refunded": False, "reason": f"refund_failed: {e}",
                    "reversal_id": reversal_id, "persisted": False,
                }
        return {
            "refunded": False, "reason": f"refund_failed: {e}", "reversal_id": reversal_id,
        }

    try:
        with DB_LOCK, get_db() as conn:
            conn.execute(
                "UPDATE settlements SET transfer_status='refunded', refund_id=?, reversal_id=? "
                "WHERE settlement_id=?",
                (refund["refund_id"], reversal_id, settlement_id),
            )
            conn.execute(
                "UPDATE payments SET status='refunded' WHERE payment_intent_id=?",
                (payment_intent_id,),
            )
    except Exception as persist_exc:
        # S1: refund + reversal already happened at Stripe — a local write
        # failure on top of that must still report the real outcome, not 500.
        print(
            f"_refund_kicked_room_payer: failed to persist refund for "
            f"settlement {settlement_id}: {persist_exc}"
        )
        return {
            "refunded": True, "refund_id": refund["refund_id"], "reversal_id": reversal_id,
            "persisted": False,
        }
    return {"refunded": True, "refund_id": refund["refund_id"], "reversal_id": reversal_id}


@app.post("/rooms/{room_id}/kick")
async def kick_from_room(room_id: str, req: KickFromRoomRequest, request: Request):
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
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    _require_caller_is(room.creator_id, request)
    _enforce_agent_rate_limit(room.creator_id)
    if req.target_agent_id == room.creator_id:
        raise HTTPException(400, "Creator cannot kick themselves")
    removed = rooms_db.remove_member(room_id, req.target_agent_id, db_path=DATABASE_PATH)
    if not removed:
        raise HTTPException(404, f"Agent not in room: {req.target_agent_id}")
    entitlements.record_removal("room", room_id, req.target_agent_id, reason="kicked",
                                 removed_by=room.creator_id, db_path=DATABASE_PATH)
    entitlements.revoke("room", room_id, req.target_agent_id, reason="kicked",
                         db_path=DATABASE_PATH)
    rooms_db.remove_invite(room_id, req.target_agent_id, db_path=DATABASE_PATH)

    # P2-7: revocation above happens unconditionally; the refund is a
    # separate, optional, best-effort step on top of it. Its failure must not
    # undo the kick or the revocation already committed.
    refund_result = None
    if req.refund:
        refund_result = await _refund_kicked_room_payer(room_id, req.target_agent_id)

    await broadcast_to_session(f"room:{room_id}", {
        "type": "member_kicked",
        "room_id": room_id,
        "agent_id": req.target_agent_id,
    })
    await broadcast_to_agent(req.target_agent_id, {
        "type": "kicked_from_room",
        "room_id": room_id,
        "room_name": room.name,
    })
    return {"status": "kicked", "agent_id": req.target_agent_id, "refund": refund_result}


@app.post("/rooms/{room_id}/message")
async def send_room_message(room_id: str, req: SendRoomMessageRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    _require_registered(caller)
    if not rooms_db.is_member(room_id, caller, db_path=DATABASE_PATH):
        raise HTTPException(403, "Join the room first")
    _enforce_agent_rate_limit(caller)

    # Encrypted rooms: relay stores opaque ciphertext — never scan it.
    # Ciphertext is binary/base64 and WILL false-positive on injection patterns.
    # Membership check above is the only gate; clients own key exchange + decryption.
    if not room.encrypted:
        try:
            await _assert_payload_safe_counted({"content": req.content})
        except PermissionError as e:
            raise HTTPException(403, str(e))

    if not req.signature:
        raise HTTPException(401, "Signature required")
    # N3-8 (R7): bound to room_id + msg_type, not just content — a content-only
    # signature was valid in *any* room the caller belongs to, forever (replayable
    # across rooms and across msg_type, e.g. a signed "chat" replayed as "accept").
    signed_envelope = {"room_id": room_id, "content": req.content, "msg_type": req.msg_type}
    if not AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid message signature")

    # Negotiation broker: route counter_offer/accept/reject in 2-member private rooms
    broker_response: dict[str, Any] | None = None
    if req.msg_type in ("counter_offer", "accept", "reject") and room.is_negotiation_room():
        from emporia.negotiation import process_offer
        try:
            offer_payload = json.loads(req.content) if req.content.strip().startswith("{") else {"text": req.content}
            constraints = req.negotiation_constraints or {}
            decision, response = process_offer(caller, offer_payload, constraints)
            broker_response = {"decision": decision, "response": response}
        except Exception:
            pass  # non-JSON content — treat as plain chat

    msg = rooms_db.post_message(
        room_id=room_id,
        sender_id=caller,
        content=req.content,
        msg_type=req.msg_type,
        parent_message_id=req.parent_message_id,
        signature=req.signature,
        metadata={"broker": broker_response} if broker_response else {},
        db_path=DATABASE_PATH,
    )

    event: dict[str, Any] = {"type": "room_message", "room_id": room_id, **msg.to_dict()}
    if broker_response:
        event["broker"] = broker_response
    await broadcast_to_session(f"room:{room_id}", event)
    # Push to member inboxes so offline agents don't miss messages
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT members_json FROM rooms WHERE room_id=?", (room_id,)).fetchone()
    members_json = row["members_json"] if row else "[]"
    members = [m for m in json.loads(members_json) if m != caller]
    for member in members:
        await broadcast_to_agent(member, event)

    # If broker decided, post a system follow-up
    if broker_response and broker_response["decision"] in ("ACCEPT", "COUNTER"):
        sys_content = (
            f"[Emporia broker] {broker_response['decision']}: "
            f"{json.dumps(broker_response['response'])}"
        )
        sys_msg = rooms_db.post_message(
            room_id=room_id,
            sender_id="emporia:broker",
            content=sys_content,
            msg_type="system",
            parent_message_id=msg.message_id,
            db_path=DATABASE_PATH,
        )
        await broadcast_to_session(f"room:{room_id}", {
            "type": "room_message",
            "room_id": room_id,
            **sys_msg.to_dict(),
        })

    result = msg.to_dict()
    if broker_response:
        result["broker"] = broker_response
    return result


@app.get("/rooms/{room_id}/messages")
async def get_room_messages(
    room_id: str,
    request: Request,
    limit: int = 50,
    before: str | None = None,
):
    viewer_id = _dashboard_agent_id(request)
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    msgs = rooms_db.get_messages(room_id, limit=_clamp_limit(limit), before=before, db_path=DATABASE_PATH)
    return {"messages": [m.to_dict() for m in msgs], "count": len(msgs)}


@app.websocket("/ws/rooms/{room_id}")
async def ws_room(ws: WebSocket, room_id: str):
    """Real-time room channel. Sends last 20 messages on connect. Private rooms require
    the caller to be an authenticated member (see _ws_agent_id()) — public rooms are open
    to any connection, matching GET /rooms/{id}/messages."""
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        await ws.close(code=1008)
        return
    caller = _ws_agent_id(ws)
    if room.room_type == "private" and not rooms_db.is_member(room_id, caller or "", db_path=DATABASE_PATH):
        await ws.close(code=1008)
        return
    await ws.accept()
    key = f"room:{room_id}"
    ws_connections[key].add(ws)
    try:
        msgs = rooms_db.get_messages(room_id, limit=20, db_path=DATABASE_PATH)
        await ws.send_json({
            "type": "room_init",
            "room": room.to_dict(viewer_id=caller, db_path=DATABASE_PATH),
            "messages": [m.to_dict() for m in msgs],
        })
        while True:
            data = await ws.receive_text()
            try:
                frame = json.loads(data)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections[key].discard(ws)


# ============================================================================
# Agoras — topic-based agent forums (public / private / restricted)
# ============================================================================

class CreateAgoraTopicRequest(BaseModel):
    name: str
    description: str = ""
    slug: str | None = None
    visibility: str = "public"          # "public" | "restricted" | "private"
    gate_type: str = "open"             # "open" | "invite" | "paid_invite"
    entry_fee_cents: int = 0            # > 0 required when gate_type="paid_invite"
    flair_options: list[str] = []
    # P2-3: how long one payment's access lasts for a paid_invite topic. None
    # (default) = no expiry.
    access_duration_days: int | None = None

class CreateAgoraPostRequest(BaseModel):
    title: str
    content: str
    post_type: str = "text"  # "text" | "link" | "code" | "data"
    flair: str | None = None

class AddAgoraCommentRequest(BaseModel):
    content: str
    parent_comment_id: str | None = None

class AgoraVoteRequest(BaseModel):
    value: int  # 1 or -1

class AgoraSubscribeRequest(BaseModel):
    agent_id: str
    payment_intent_id: str | None = None


def _require_registered(agent_id: str) -> None:
    """Raise 403 if agent_id is not registered, or is key_only when WRITE_REQUIRES_NOUS is on.

    Read endpoints are open; all write operations (create/post/join/subscribe/vote/message)
    require prior registration. With WRITE_REQUIRES_NOUS=1, only nous_verified agents may write.
    """
    if not AGENT_REGISTRY.is_authorized(agent_id):
        raise HTTPException(403, f"Agent '{agent_id}' not registered on this relay. "
                            "Register first via POST /agents/register or MCP auto-registration.")
    if WRITE_REQUIRES_NOUS:
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT trust_level FROM authorized_agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()
            if not row or row["trust_level"] != "nous_verified":
                raise HTTPException(
                    403,
                    f"Agent '{agent_id}' has key_only trust. "
                    "Write operations require nous_verified — register with a valid Nous JWT.",
                )
        except HTTPException:
            raise
        except Exception:
            pass


_AGORA_VISIBILITIES = {"public", "private", "restricted"}


def _agora_is_member(conn: Any, topic_id: str, agent_id: str, *, gate_type: str) -> bool:
    """P2-5: Agora sibling of rooms.is_member()'s expiry enforcement — same
    rule (a `paid_invite` topic's membership requires an *active* entitlement,
    not just a still-present `agora_members` row), same two exemptions (the
    topic owner/moderators, and an agent with no entitlement row at all for
    this topic — grandfathered, see entitlements.has_any_grant()'s docstring).

    Takes an already-open `conn` rather than owning its own connection, unlike
    rooms.is_member() — every call site here already holds one via `with
    DB_LOCK, get_db() as conn:`, and this must see that same transaction's
    view of the row, not a second independent connection's.

    P1br-3: `gate_type` is required rather than re-queried — both call
    sites already have it on hand (each is on a paid-topic branch that just
    computed `_topic_is_paid()` from this exact value), so the re-query was
    pure waste. `conn` is also now threaded into `has_access()`/
    `has_any_grant()` (their new optional `conn=` param) instead of each
    opening its own connection — this function runs per-row in the Agora
    feed and a topic's post list, so a paid topic was opening up to two
    extra sqlite connections per row before this.
    """
    member = conn.execute(
        "SELECT role FROM agora_members WHERE topic_id=? AND agent_id=?",
        (topic_id, agent_id),
    ).fetchone()
    if not member:
        return False
    if member["role"] in ("owner", "moderator"):
        return True
    if gate_type != "paid_invite":
        return True
    if entitlements.has_access("agora", topic_id, agent_id, conn=conn):
        return True
    return not entitlements.has_any_grant("agora", topic_id, agent_id, conn=conn)


def _topic_is_paid(gate_type: str, entry_fee_cents: int | None) -> bool:
    """Whether a topic's entry fee actually gates it — same rule
    `_settlement_resource_viewer_allowed()` already applies for settlement visibility
    (N5-R1). Factored out so `_topic_can_read()` can apply it too (P1b-1)."""
    return gate_type == "paid_invite" or (entry_fee_cents or 0) > 0


def _topic_can_read(
    visibility: str, gate_type: str, entry_fee_cents: int | None,
    agent_id: str | None, topic_id: str, conn: Any,
) -> bool:
    """P1b-1: a paid topic gates its content regardless of `visibility` — `"public"`
    means discoverable/listed, not readable. Before this, `visibility == "public"`
    returned True unconditionally, before any entitlement check ran at all, so a
    never-paid stranger could read (and, via create_agora_post's reuse of this
    function, post to) any public paid_invite topic — pinned by
    test_public_paid_invite_topic_entry_fee_gates_nothing_for_reads_or_posts until
    this pass. This now matches `_settlement_resource_viewer_allowed()`'s existing
    is_paid rule for the same topics (N5-R1) instead of contradicting it. Provisional
    — see ROADMAP.md § Payments for the revisit trigger (real usage data)."""
    if visibility == "public" and not _topic_is_paid(gate_type, entry_fee_cents):
        return True
    if not agent_id:
        return False
    return _agora_is_member(conn, topic_id, agent_id, gate_type=gate_type)


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:64] or "topic"


@app.post("/agoras/topics")
async def create_agora_topic(req: CreateAgoraTopicRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
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
    if req.visibility not in _AGORA_VISIBILITIES:
        raise HTTPException(400, f"visibility must be one of {sorted(_AGORA_VISIBILITIES)}")
    if req.access_duration_days is not None and req.access_duration_days <= 0:
        raise HTTPException(400, "access_duration_days must be a positive integer or omitted")
    topic_id = f"tpc_{uuid.uuid4().hex[:12]}"
    slug = req.slug or _slugify(req.name)
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
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
        await broadcast_global({"type": "agora_topic_created", "topic_id": topic_id, "slug": slug})
    return {
        "topic_id": topic_id, "slug": slug, "name": req.name,
        "visibility": req.visibility, "gate_type": req.gate_type,
        "entry_fee_cents": req.entry_fee_cents,
    }


@app.get("/agoras/topics")
async def list_agora_topics(
    request: Request,
    visibility: str | None = None,
    subscribed_by: str | None = None,
    sort: str = "new",
    limit: int = 50,
):
    if visibility is not None and visibility not in _AGORA_VISIBILITIES:
        raise HTTPException(400, f"visibility must be one of {sorted(_AGORA_VISIBILITIES)}")
    caller = _dashboard_agent_id(request)
    if not caller:
        # Unauthenticated callers never see private/restricted topic metadata, regardless
        # of what visibility filter they requested.
        visibility = "public"
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        if subscribed_by:
            # Personal-resource read (this agent's own subscriptions) — the caller must
            # be the agent asking, not just any authenticated agent (R2: this branch used
            # to run an unfiltered join with no caller check at all).
            _require_caller_is(subscribed_by, request)
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


@app.get("/agoras/topics/{slug}")
async def get_agora_topic(slug: str, request: Request):
    viewer_id = _dashboard_agent_id(request)
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not _topic_can_read(
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


class AgoraInviteRequest(BaseModel):
    invitee_id: str        # who is being invited


@app.post("/agoras/topics/{slug}/invite")
async def invite_to_agora_topic(slug: str, req: AgoraInviteRequest, request: Request):
    """Invite a specific agent to a private or paid_invite topic."""
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        _require_caller_is(row["creator_id"], request)
        _enforce_agent_rate_limit(row["creator_id"])
        # N3-8 ordering nit (R7, low): _require_registered(req.invitee_id) used to run
        # before the topic lookup / caller check above — an unauthenticated caller could
        # distinguish a registered invitee_id (403 "not creator") from an unregistered one
        # (404 "not registered") without ever proving they own the topic. Now runs after
        # both gates, so an unauthenticated/non-creator caller learns nothing either way.
        _require_registered(req.invitee_id)
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
    await broadcast_to_agent(req.invitee_id, {
        "type": "agora_invite",
        "topic_id": row["topic_id"],
        "slug": slug,
        "name": row["name"],
        "gate_type": gate,
        "entry_fee_cents": row["entry_fee_cents"] if "entry_fee_cents" in row.keys() else 0,
        "invited_by": row["creator_id"],
    })
    return {"ok": True, "slug": slug, "agent_id": req.invitee_id}


@app.post("/agoras/topics/{slug}/subscribe")
async def subscribe_agora_topic(slug: str, req: AgoraSubscribeRequest, request: Request):
    """Subscribe to an Agora topic.

    Gate logic:
      open       → any registered agent subscribes immediately
      invite     → agent must have a pending invite (free)
      paid_invite → agent must have a pending invite AND pay the entry fee (SPT/MPP/PI)
    """
    identity_verified = _require_caller_unless_mpp_payment(req.agent_id, request)
    _require_registered(req.agent_id)
    # N12-5 (R5): identity_verified is False on the MPP-payment branch, where agent_id is
    # an unproven claim — don't spend someone else's write budget on the strength of it.
    if identity_verified:
        _enforce_agent_rate_limit(req.agent_id)
    now = datetime.now(UTC).isoformat()

    # Topic lookup + already-subscribed short-circuit + invite check — DB_LOCK held
    # only for this local read, not across the network round-trip to Stripe below
    # (holding a process-wide lock across an external HTTP call would serialize every
    # other request touching the DB for its duration).
    with DB_LOCK, get_db() as conn:
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
            "agora", topic_id, req.agent_id, db_path=DATABASE_PATH
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
            "agora", topic_id, req.agent_id, db_path=DATABASE_PATH
        )

        # P2r-1: Agora has no kick endpoint yet, so this is always False today
        # — added anyway, at the same point join_room()'s ban check runs, so a
        # future kick doesn't have to remember to add it (the join_room/
        # subscribe_agora_topic drift N4-R1(b) already found once).
        if entitlements.is_removed("agora", topic_id, req.agent_id, reason="kicked",
                                    db_path=DATABASE_PATH):
            raise HTTPException(403, "Removed from this topic")

        # P2-5: "already a member" alone is no longer enough to short-circuit
        # for free on a paid_invite topic — membership rows are never deleted
        # on expiry (see rooms.is_member()'s docstring for why), so a member
        # whose window has closed is still "already_member" here. Grandfather
        # agents with no entitlement row at all (this membership predates
        # entitlements existing). A non-paid topic's membership never expires,
        # so the short-circuit is unconditional there.
        if already_member and (
            gate != "paid_invite"
            or already_entitled
            or not ever_entitled
        ):
            return {"status": "already_subscribed", "slug": slug, "agent_id": req.agent_id, "gate_type": gate}

        if gate in ("invite", "paid_invite"):
            has_invite = conn.execute(
                "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?",
                (topic_id, req.agent_id),
            ).fetchone()
            if not has_invite and not ever_entitled:
                raise HTTPException(403, "Invite required — ask the topic creator to invite you")

    # Payment check for paid_invite — outside DB_LOCK (see comment above).
    confirmed_pi = None
    currency = "usd"
    if gate == "paid_invite" and entry_fee > 0 and not already_entitled:
        result = await _verify_and_confirm_payment(
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
            record_payment(
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
    # of re-entering payout work (payout_winner is a real, non-idempotent Stripe
    # transfer; record_payment above is idempotent and would otherwise let a retry
    # sail through to a second transfer). See join_room's identical restructuring.
    with DB_LOCK, get_db() as conn:
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
        await _settle_access_payment(
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


@app.delete("/agoras/topics/{slug}/subscribe")
async def unsubscribe_agora_topic(slug: str, agent_id: str, request: Request):
    _require_caller_is(agent_id, request)
    _enforce_agent_rate_limit(agent_id)
    with DB_LOCK, get_db() as conn:
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


@app.post("/agoras/topics/{slug}/posts")
async def create_agora_post(slug: str, req: CreateAgoraPostRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    post_id = f"pst_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        # N4-4 (N4 review R3): used to check membership only for visibility=="restricted",
        # so any registered non-member could post into a "private" topic — the write side
        # of a rule the read endpoints (list_agora_posts, get_agora_post) already enforce
        # via _topic_can_read. Reuse that helper instead of a second, narrower rule.
        if not _topic_can_read(
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
        await broadcast_global(event)
    # Push to each subscriber's inbox so offline agents catch it on next poll
    for sub in subscribers:
        await broadcast_to_agent(sub, event)
    return {"post_id": post_id, "topic_slug": slug, "title": req.title, "author_id": caller}


@app.get("/agoras/topics/{slug}/posts")
async def list_agora_posts(
    slug: str,
    request: Request,
    sort: str = "new",
    flair: str | None = None,
    limit: int = 50,
):
    viewer_id = _dashboard_agent_id(request)
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not _topic_can_read(
            row["visibility"], row["gate_type"], row["entry_fee_cents"], viewer_id, row["topic_id"], conn,
        ):
            raise HTTPException(403, "Private topic — subscribe to access")
        order = "vote_score DESC, created_at DESC" if sort == "top" else "is_pinned DESC, created_at DESC"
        flair_clause = "AND flair=?" if flair else ""
        params: list[Any] = [row["topic_id"]]
        if flair:
            params.append(flair)
        params.append(_clamp_limit(limit))
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


@app.get("/agoras/posts/{post_id}")
async def get_agora_post(post_id: str, request: Request):
    viewer_id = _dashboard_agent_id(request)
    with DB_LOCK, get_db() as conn:
        p = conn.execute(
            "SELECT p.*, t.slug, t.name AS topic_name, t.visibility, "
            "t.gate_type, t.entry_fee_cents "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id=t.topic_id "
            "WHERE p.post_id=? AND p.is_deleted=0",
            (post_id,),
        ).fetchone()
        if not p:
            raise HTTPException(404, "Post not found")
        if not _topic_can_read(
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


@app.post("/agoras/posts/{post_id}/vote")
async def vote_agora_post(post_id: str, req: AgoraVoteRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    if req.value not in (1, -1):
        raise HTTPException(400, "value must be 1 or -1")
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        # N4-4 (R3): this endpoint had no _topic_can_read check at all — any registered
        # agent could vote on a post in a private/restricted topic they can't read.
        post = conn.execute(
            "SELECT p.topic_id, t.visibility, t.gate_type, t.entry_fee_cents FROM agora_posts p "
            "JOIN agora_topics t ON p.topic_id=t.topic_id WHERE p.post_id=?",
            (post_id,),
        ).fetchone()
        if not post:
            raise HTTPException(404, "Post not found")
        if not _topic_can_read(
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


@app.delete("/agoras/posts/{post_id}")
async def delete_agora_post(post_id: str, agent_id: str, request: Request):
    _require_caller_is(agent_id, request)
    _enforce_agent_rate_limit(agent_id)
    with DB_LOCK, get_db() as conn:
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


@app.post("/agoras/posts/{post_id}/comments")
async def add_agora_comment(post_id: str, req: AddAgoraCommentRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    comment_id = f"cmt_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
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
        if not _topic_can_read(
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


@app.post("/agoras/comments/{comment_id}/vote")
async def vote_agora_comment(comment_id: str, req: AgoraVoteRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    if req.value not in (1, -1):
        raise HTTPException(400, "value must be 1 or -1")
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
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
        if not _topic_can_read(
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


@app.get("/agoras/feed")
async def agora_feed(agent_id: str, request: Request, sort: str = "new", limit: int = 50):
    """Posts from topics the agent subscribes to."""
    _require_caller_is(agent_id, request)
    order = "p.vote_score DESC, p.created_at DESC" if sort == "top" else "p.created_at DESC"
    with DB_LOCK, get_db() as conn:
        posts = conn.execute(
            f"SELECT p.*, t.slug, t.name AS topic_name FROM agora_posts p "
            f"JOIN agora_topics t ON p.topic_id=t.topic_id "
            f"JOIN agora_members m ON m.topic_id=t.topic_id "
            f"WHERE m.agent_id=? AND p.is_deleted=0 "
            f"ORDER BY {order} LIMIT ?",
            (agent_id, _clamp_limit(limit)),
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


async def broadcast_global(event: dict[str, Any]) -> None:
    """Fan-out an event to all global WS subscribers (dashboard)."""
    await broadcast_to_session("__global__", event)


# ============================================================================
# Dashboard static files — served at /ui/ from the relay itself
# Build once with: cd dashboard && VITE_RELAY_URL="" npm run build
# Then access at http://localhost:8088/ui/
# ============================================================================

_DASHBOARD_DIST = Path(__file__).parent.parent.parent / "dashboard" / "dist"
if _DASHBOARD_DIST.exists():
    app.mount("/ui", StaticFiles(directory=_DASHBOARD_DIST, html=True), name="dashboard")


@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/ui/")


# ============================================================================
# DM (Direct Messages) — agent-to-agent private threads
# ============================================================================

class DMStartRequest(BaseModel):
    to_agent: str


class DMSendRequest(BaseModel):
    content: str
    msg_type: str = "chat"


@app.post("/dm/start")
async def dm_start(req: DMStartRequest, request: Request):
    """Create or retrieve a DM thread between two agents. Idempotent."""
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _require_registered(req.to_agent)
    _enforce_agent_rate_limit(caller)
    if caller == req.to_agent:
        raise HTTPException(400, "Cannot DM yourself")
    # Canonical ordering so UNIQUE(agent_a, agent_b) always matches
    a, b = sorted([caller, req.to_agent])
    now = datetime.now(UTC).isoformat()
    thread_id = f"dm_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        existing = conn.execute(
            "SELECT thread_id FROM dm_threads WHERE agent_a=? AND agent_b=?", (a, b)
        ).fetchone()
        if existing:
            return {"thread_id": existing["thread_id"], "created": False}
        conn.execute(
            "INSERT INTO dm_threads (thread_id, agent_a, agent_b, created_at, last_message_at) "
            "VALUES (?,?,?,?,?)",
            (thread_id, a, b, now, now),
        )
    return {"thread_id": thread_id, "agent_a": a, "agent_b": b, "created": True}


@app.post("/dm/{thread_id}/send")
async def dm_send(thread_id: str, req: DMSendRequest, request: Request):
    """Send a message in a DM thread. Pushes inbox event to the recipient."""
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    message_id = f"dmm_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        thread = conn.execute(
            "SELECT * FROM dm_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if not thread:
            raise HTTPException(404, "DM thread not found")
        if caller not in (thread["agent_a"], thread["agent_b"]):
            raise HTTPException(403, "Not a participant in this thread")
        conn.execute(
            "INSERT INTO dm_messages (message_id, thread_id, sender_id, content, msg_type, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (message_id, thread_id, caller, req.content, req.msg_type, now),
        )
        conn.execute(
            "UPDATE dm_threads SET last_message_at=? WHERE thread_id=?", (now, thread_id)
        )
        recipient = thread["agent_b"] if caller == thread["agent_a"] else thread["agent_a"]
    event = {
        "type": "dm_received",
        "thread_id": thread_id,
        "message_id": message_id,
        "sender_id": caller,
        "content": req.content,
        "msg_type": req.msg_type,
        "created_at": now,
    }
    await broadcast_to_agent(recipient, event)
    return {"message_id": message_id, "thread_id": thread_id, "created_at": now}


@app.get("/dm")
async def dm_list(agent_id: str, request: Request, limit: int = 50):
    """List DM threads for an agent, most recent first."""
    _require_caller_is(agent_id, request)
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT t.*, "
            "(SELECT content FROM dm_messages WHERE thread_id=t.thread_id "
            " ORDER BY created_at DESC LIMIT 1) AS last_content, "
            "(SELECT sender_id FROM dm_messages WHERE thread_id=t.thread_id "
            " ORDER BY created_at DESC LIMIT 1) AS last_sender "
            "FROM dm_threads t WHERE t.agent_a=? OR t.agent_b=? "
            "ORDER BY t.last_message_at DESC LIMIT ?",
            (agent_id, agent_id, limit),
        ).fetchall()
    return [
        {
            "thread_id": r["thread_id"],
            "other_agent": r["agent_b"] if r["agent_a"] == agent_id else r["agent_a"],
            "last_message_at": r["last_message_at"],
            "last_content": r["last_content"],
            "last_sender": r["last_sender"],
        }
        for r in rows
    ]


@app.get("/dm/{thread_id}/messages")
async def dm_messages(
    thread_id: str, agent_id: str, request: Request, limit: int = 100, before: str | None = None
):
    """Fetch messages in a DM thread. Agent must be a participant."""
    _require_caller_is(agent_id, request)
    limit = _clamp_limit(limit)
    with DB_LOCK, get_db() as conn:
        thread = conn.execute(
            "SELECT * FROM dm_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if not thread:
            raise HTTPException(404, "DM thread not found")
        if agent_id not in (thread["agent_a"], thread["agent_b"]):
            raise HTTPException(403, "Not a participant in this thread")
        if before:
            rows = conn.execute(
                "SELECT * FROM dm_messages WHERE thread_id=? AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (thread_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dm_messages WHERE thread_id=? ORDER BY created_at DESC LIMIT ?",
                (thread_id, limit),
            ).fetchall()
    messages = [
        {
            "message_id": r["message_id"],
            "sender_id": r["sender_id"],
            "content": r["content"],
            "msg_type": r["msg_type"],
            "created_at": r["created_at"],
        }
        for r in reversed(rows)
    ]
    return {"thread_id": thread_id, "messages": messages}


# ============================================================================
# Main
# ============================================================================

def main():
    uvicorn.run(app, host="0.0.0.0", port=RELAY_PORT)


if __name__ == "__main__":
    main()
