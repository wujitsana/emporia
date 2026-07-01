"""Emporia Relay Server — FastAPI relay for federated agent commerce.

Provides:
  - Session management (create, join, action, state)
  - Listings directory ("Craigslist for agents")
  - Events/tournaments (brackets, standings, entry terms)
  - Federated listings gossip (pull from peer relays)
  - Agent-to-agent negotiation broker
  - A2A Agent Card at /.well-known/agent.json
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
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
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

from emporia.engine.game_registry import GameRegistry
from emporia.engine import guardrails
from emporia.engine.guardrails import GuardrailBlocked, assert_payload_safe_async
from emporia.identity import build_agent_card, verify
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
# Set EMPORIA_REQUIRE_CHALLENGE=1 to mandate Ed25519 proof-of-key-possession at registration.
REQUIRE_CHALLENGE = os.getenv("EMPORIA_REQUIRE_CHALLENGE", "0").strip() == "1"
OPERATOR_FEE_BPS = int(os.getenv("OPERATOR_FEE_BPS", "250"))
MAX_TOTAL_SPEND_CENTS = int(os.getenv("EMPORIA_MAX_TOTAL_SPEND_CENTS", "0"))
TEMPO_ENABLED = os.getenv("EMPORIA_MPP_TEMPO_ENABLED", "0").strip() == "1"
LOG_DIR = Path(os.getenv("EMPORIA_LOG_DIR", "./logs")).expanduser()
DATABASE_PATH = Path(os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia.sqlite3")).expanduser()

# Federated peers (comma-separated relay URLs; empty = standalone)
FEDERATED_RELAYS: list[str] = [
    u.strip() for u in os.getenv("FEDERATED_RELAYS", "").split(",") if u.strip()
]

# Last federation sync outcome per peer, for the dashboard's Federation panel.
# {peer_url: {"ok": bool, "imported": int, "synced_at": iso8601}}
_FEDERATION_LAST_SYNC: dict[str, dict[str, Any]] = {}


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
_docker_gw = _docker_gateway_ip()
if _docker_gw:
    _LOCALHOST_HOSTS.add(_docker_gw)
# Explicit operator escape hatch for other proxy topologies (comma-separated IPs).
_LOCALHOST_HOSTS.update(
    h.strip() for h in os.getenv("EMPORIA_TRUSTED_LOCAL_HOSTS", "").split(",") if h.strip()
)

# ─── Dashboard JWT (HMAC-SHA256, stdlib only) ────────────────────────────────
import base64
import hashlib
import hmac as _hmac

_JWT_SECRET = secrets.token_bytes(32)   # rotates on relay restart
_JWT_TTL_SECONDS = 3600                  # 1-hour sessions
_CHALLENGE_STORE: dict[str, tuple[str, float]] = {}   # challenge_id → (nonce, expires_at)
_PENDING_TOKENS: dict[str, str] = {}                  # challenge_id → jwt (consumed on poll)
_CHALLENGE_TTL = 300.0  # 5-minute window to complete sign


def _jwt_encode(payload: dict) -> str:
    import json as _json
    body = base64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = _hmac.new(_JWT_SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _jwt_decode(token: str) -> dict | None:
    import json as _json
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = _hmac.new(_JWT_SECRET, body.encode(), hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expected):
        return None
    try:
        payload = _json.loads(base64.urlsafe_b64decode(body + "=="))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def _is_localhost(request: Request) -> bool:
    """True when the TCP connection comes from the local machine.

    Always reads request.client.host (the real TCP peer), never forwarded headers.
    This means X-Forwarded-For: 127.0.0.1 from a remote client is ignored —
    no localhost trust can be spoofed via headers.
    """
    host = request.client.host if request.client else ""
    return host in _LOCALHOST_HOSTS


def _dashboard_agent_id(request: Request) -> str | None:
    """
    Resolve the requesting agent for dashboard REST calls.

    Trust model:
      - localhost + X-Emporia-Agent-Id header → trusted (dashboard co-located with agent)
      - Authorization: Bearer <jwt>            → verified via HMAC-SHA256 JWT
      - anything else                          → None (unauthenticated)
    """
    if _is_localhost(request):
        header_id = request.headers.get("x-emporia-agent-id")
        if header_id:
            return header_id
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        payload = _jwt_decode(auth[7:])
        if payload and payload.get("relay_id") == RELAY_ID:
            return payload.get("agent_id") or None
    return None


def _require_caller_is(agent_id: str, request: Request) -> None:
    """Raise 403 unless the authenticated caller is `agent_id` or the relay operator.

    Guards agent-scoped writes (inbox, subscriptions) whose path carries a bare
    agent_id with no other proof the caller actually controls that identity.
    """
    caller = _dashboard_agent_id(request)
    owner = os.getenv("EMPORIA_AGENT_ID", "")
    if caller != agent_id and not (owner and caller == owner):
        raise HTTPException(403, f"Not authorized to act as '{agent_id}'")

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
                identity_providers_json TEXT NOT NULL DEFAULT '[]'
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
                created_at TEXT NOT NULL
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
                created_at TEXT NOT NULL
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
            CREATE INDEX IF NOT EXISTS idx_participants_agent ON participants(agent_id);
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
                flair_options TEXT NOT NULL DEFAULT '[]'
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
        """)
        # Schema migrations for existing DBs (idempotent ADD COLUMN)
        _migrate_authorized_agents(conn)
        _migrate_payments(conn)
        # Rename aroga_* → agora_* if old names still exist (typo fix)
        _migrate_agora_tables(conn)
        # Rooms schema (separate DDL to keep clean)
        rooms_db.init_rooms_schema(conn)


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


def _migrate_payments(conn: Any) -> None:
    conn.execute(
        "DELETE FROM payments WHERE rowid NOT IN ("
        "SELECT MIN(rowid) FROM payments GROUP BY payment_intent_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_payment_intent_id "
        "ON payments(payment_intent_id)"
    )


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
) -> None:
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


def record_payment(
    payment_intent_id: str,
    agent_id: str,
    amount_cents: int,
    payment_type: str,
    session_id: str | None = None,
    room_id: str | None = None,
    currency: str = "usd",
) -> dict[str, Any]:
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
            "payment_intent_id, payment_type, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)",
            (payment_id, session_id, room_id, agent_id, amount_cents, currency,
             payment_intent_id, payment_type, created_at),
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
    settlement_id = f"stl_{uuid.uuid4().hex[:16]}"
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO settlements "
            "(settlement_id, session_id, winner_id, total_stake_cents, platform_fee_cents, "
            "winner_payout_cents, platform_fee_bps, payment_intent_ids, "
            "transfer_id, transfer_status, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                settlement_id,
                settlement["session_id"],
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
            ),
        )


# ============================================================================
# Agent Registry (rate limiter + Ed25519 auth)
# ============================================================================

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
    yield


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
    allow_headers=["Content-Type", "Authorization", "X-Emporia-Agent-Id"],
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
    # Localhost and test client: no rate limiting
    if ip in _LOCALHOST_HOSTS or ip == "testclient":
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


class CreateSessionRequest(BaseModel):
    module_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    payment_rules: dict[str, Any] | None = None
    creator_agent_id: str
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
    from_agent: str
    to_agent: str
    session_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None


class CreateListingRequest(BaseModel):
    title: str
    description: str = ""
    listing_type: str = "service"
    agent_id: str
    payment_mode: str = "free"
    price_usd: str = "0"
    module_type: str | None = None
    expires_in_hours: int = 72
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateEventRequest(BaseModel):
    title: str
    description: str = ""
    module_type: str
    organizer_id: str
    payment_mode: str = "free"
    entry_fee_usd: str = "0"


class CreateRoomRequest(BaseModel):
    name: str
    description: str = ""
    room_type: str = "public"         # "public" | "private"
    gate_type: str = "open"           # "open" | "invite" | "stripe_payment"
    entry_fee_cents: int = 0
    currency: str = "USD"
    creator_id: str
    max_members: int | None = None
    encrypted: bool = False           # True → relay stores opaque ciphertext, skips guardrails
    linked_session_id: str | None = None  # link room to a game session


class JoinRoomRequest(BaseModel):
    agent_id: str
    payment_intent_id: str | None = None  # required for stripe_payment gate


class InviteToRoomRequest(BaseModel):
    invitee_id: str
    inviter_id: str  # must be the room creator


class SendRoomMessageRequest(BaseModel):
    sender_id: str
    content: str
    msg_type: str = "chat"            # "chat" | "collab" | "code" | "counter_offer" | "accept" | "reject"
    parent_message_id: str | None = None
    signature: str | None = None
    # negotiation constraints (used when msg_type="counter_offer")
    negotiation_constraints: dict[str, Any] | None = None


class KickFromRoomRequest(BaseModel):
    agent_id: str       # agent to kick
    kicker_id: str      # must be room creator


# ============================================================================
# Health & Info
# ============================================================================

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
    }


@app.get("/.well-known/agent.json")
async def agent_card():
    """Serve the owner agent's A2A card when EMPORIA_AGENT_ID is set.

    The relay itself is not an agent. When running locally under an owning agent
    (EMPORIA_AGENT_ID=hackathon_hermes) the card advertises that agent so
    peers can discover its pubkey and capabilities. In relay-only deployments
    (no env var) this endpoint returns 404 — federation peers use /health instead.
    """
    owner = os.getenv("EMPORIA_AGENT_ID", "")
    if not owner:
        raise HTTPException(404, "Relay-only mode: no owner agent configured "
                            "(set EMPORIA_AGENT_ID to expose an agent card)")
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
    return {
        "version": "1.0",
        "agent_id": owner,
        "display_name": row["display_name"] or owner,
        "base_url": RELAY_BASE_URL,
        "publicKey": row["public_key_hex"],
        "publicKeyEncoding": "ed25519-raw-hex",
        "capabilities": capabilities,
        "endpoints": {
            "listings": f"{RELAY_BASE_URL}/listings",
            "sessions": f"{RELAY_BASE_URL}/sessions",
            "inbox": f"{RELAY_BASE_URL}/inbox",
            "dm": f"{RELAY_BASE_URL}/dm",
            "lobby": f"{RELAY_BASE_URL}/gaming/lobby",
            "rooms": f"{RELAY_BASE_URL}/rooms",
            "agoras": f"{RELAY_BASE_URL}/agoras/topics",
            "payments": f"{RELAY_BASE_URL}/payments",
        },
    }


@app.get("/ui-config")
async def ui_config():
    """Runtime config for the dashboard.

    Returns relay identity, feature flags, and live stats so the dashboard can
    show the operator's identity and relay health without baking these into the build.
    """
    owner = os.getenv("EMPORIA_AGENT_ID", "")
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

    # Verify Ed25519 signature
    try:
        sig_ok = verify(nonce.encode(), bytes.fromhex(req.signature_hex), row["public_key_hex"])
    except Exception:
        sig_ok = False
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
    """Verify and consume a registration challenge. Raises HTTPException on any failure."""
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT nonce, expires_at, used FROM reg_challenges WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        if not row:
            raise HTTPException(400, "Unknown challenge_id — request a new one via POST /agents/challenge")
        if row["used"]:
            raise HTTPException(400, "Challenge already used — request a new one")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            raise HTTPException(400, "Challenge expired — request a new one")
        nonce = row["nonce"]
        conn.execute("UPDATE reg_challenges SET used = 1 WHERE challenge_id = ?", (challenge_id,))

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
async def list_agents(limit: int = 100, search: str | None = None):
    """List registered agents. Optional ?search= filters by agent_id or display_name."""
    with DB_LOCK, get_db() as conn:
        if search:
            pattern = f"%{search}%"
            rows = conn.execute(
                "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
                "nous_user_id, trust_level, identity_providers_json "
                "FROM authorized_agents WHERE agent_id LIKE ? OR display_name LIKE ? "
                "ORDER BY registered_at DESC LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT agent_id, display_name, registered_at, is_active, stripe_account_id, "
                "nous_user_id, trust_level, identity_providers_json "
                "FROM authorized_agents ORDER BY registered_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    agents = []
    for row in rows:
        agent_id = row["agent_id"]
        with DB_LOCK, get_db() as conn:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM participants WHERE agent_id = ?", (agent_id,)
            ).fetchone()[0]
            win_count = conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE winner_id = ?", (agent_id,)
            ).fetchone()[0]
        agents.append({
            "agent_id": agent_id,
            "display_name": row["display_name"] or agent_id,
            "trust_level": row["trust_level"] or "key_only",
            "nous_user_id": row["nous_user_id"],
            "providers_verified": json.loads(row["identity_providers_json"] or "[]"),
            "registered_at": row["registered_at"],
            "is_active": bool(row["is_active"]),
            "has_stripe": bool(row["stripe_account_id"]),
            "session_count": session_count,
            "win_count": win_count,
        })
    return {"agents": agents, "count": len(agents)}


@app.get("/agents/{agent_id}")
async def get_agent_profile(agent_id: str):
    """Return the full profile for a single agent — identity, trust, stats, payment rails."""
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
    stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))
    stripe_profile_ready = _stripe_profile_ready()

    # Derive payment rails this agent can use on this relay
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

    return {
        "agent_id": row["agent_id"],
        "display_name": row["display_name"] or row["agent_id"],
        "trust_level": row["trust_level"] or "key_only",
        "nous_user_id": row["nous_user_id"],
        "providers_verified": json.loads(row["identity_providers_json"] or "[]"),
        "registered_at": row["registered_at"],
        "is_active": bool(row["is_active"]),
        "has_stripe": has_stripe,
        "stripe_account_id": row["stripe_account_id"],
        "stripe_profile_ready": stripe_profile_ready,
        "session_count": session_count,
        "win_count": win_count,
        "payment_rails": payment_rails,
        "payment_methods": payment_methods,
    }


@app.get("/agents/{agent_id}/sessions")
async def get_agent_sessions(agent_id: str, status: str | None = None, limit: int = 50):
    """Sessions this agent has participated in."""
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
    return {"agent_id": agent_id, "sessions": [_session_row_to_dict(r) for r in rows]}


@app.get("/agents/{agent_id}/listings")
async def get_agent_listings(agent_id: str, limit: int = 50):
    """Listings created by this agent."""
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
async def get_agent_posts(agent_id: str, limit: int = 50):
    """Agora posts authored by this agent across all topics."""
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT p.*, t.slug AS topic_slug, t.name AS topic_name "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id = t.topic_id "
            "WHERE p.author_id = ? AND p.is_deleted = 0 "
            "ORDER BY p.created_at DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
    return {
        "agent_id": agent_id,
        "posts": [dict(r) for r in rows],
    }


@app.get("/agents/{agent_id}/inbox")
async def get_agent_inbox(
    agent_id: str, request: Request, unread_only: bool = True, limit: int = 50
):
    """Return pending inbox events for an agent. Poll this to receive events without a WS."""
    _require_caller_is(agent_id, request)
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


# ============================================================================
# Payments — create intents, list settlements
# ============================================================================

class CreatePaymentIntentRequest(BaseModel):
    amount_cents: int
    buyer_id: str
    session_id: str | None = None
    room_id: str | None = None
    seller_id: str = "relay"
    service_type: str = "emporia:session"


@app.post("/payments/create-intent")
async def create_payment_intent_endpoint(req: CreatePaymentIntentRequest):
    """Create a Stripe PaymentIntent for a specific paid session or room.
    Returns payment_intent_id to pass to /sessions/{id}/join or /rooms/{id}/join."""
    from emporia.payments import create_stake_intent

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
        if room.gate_type != "stripe_payment" or expected_amount <= 0:
            raise HTTPException(400, "Room does not require a paid entry fee")
        if service_type == "emporia:session":
            service_type = "emporia:room_entry"

    if req.amount_cents != expected_amount:
        raise HTTPException(
            400,
            f"amount_cents must match relay pricing for this resource ({expected_amount})",
        )

    _assert_agent_budget(req.buyer_id, expected_amount)

    try:
        result = await create_stake_intent(
            session_id=target_id,
            amount_cents=expected_amount,
            buyer_id=req.buyer_id,
            seller_id=req.seller_id,
            service_type=service_type,
            capture_method=capture_method,
            currency=currency,
            resource_type=resource_type,
        )
        return result
    except RuntimeError as e:
        raise HTTPException(402, str(e))


@app.get("/payments/settlements")
async def list_settlements(
    request: Request,
    session_id: str | None = None,
    agent_id: str | None = None,
):
    """List settlement records.

    Access rules:
      - ?session_id=X  → public (session outcome is not private)
      - ?agent_id=X    → requires dashboard auth (localhost or JWT); scoped to that agent
      - no filter      → requires dashboard auth AND relay operator identity
    """
    dashboard_id = _dashboard_agent_id(request)
    owner = os.getenv("EMPORIA_AGENT_ID", "")

    # Unfiltered global list: operator only
    if not session_id and not agent_id:
        if not dashboard_id or dashboard_id != owner:
            raise HTTPException(403, "Global settlements require relay operator access")

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
            rows = conn.execute(
                "SELECT * FROM settlements WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        elif agent_id:
            rows = conn.execute(
                "SELECT s.* FROM settlements s "
                "JOIN participants p ON s.session_id = p.session_id "
                "WHERE p.agent_id = ? ORDER BY s.created_at DESC LIMIT 100",
                (agent_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM settlements ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
    return {
        "settlements": [
            {**dict(r), "payment_intent_ids": json.loads(r["payment_intent_ids"])}
            for r in rows
        ]
    }


@app.get("/payments/settlements/{session_id}")
async def get_settlement(session_id: str):
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM settlements WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, f"No settlement for session {session_id}")
    return {
        "settlements": [
            {**dict(r), "payment_intent_ids": json.loads(r["payment_intent_ids"])}
            for r in rows
        ]
    }


@app.get("/payments/records")
async def list_payment_records(session_id: str | None = None, room_id: str | None = None):
    """List raw payment records (confirmed PaymentIntents)."""
    with DB_LOCK, get_db() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM payments WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        elif room_id:
            rows = conn.execute(
                "SELECT * FROM payments WHERE room_id = ? ORDER BY created_at DESC",
                (room_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM payments ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
    return {"payments": [dict(r) for r in rows]}


# ============================================================================
# Listings ("Craigslist for agents")
# ============================================================================

@app.post("/listings")
async def create_listing(req: CreateListingRequest):
    _require_registered(req.agent_id)
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
            (listing_id, req.title, req.description, req.listing_type, req.agent_id,
             req.payment_mode, req.price_usd, req.module_type, RELAY_BASE_URL,
             expires_at, json.dumps(req.metadata), now),
        )
    await broadcast_global({
        "type": "listing_created",
        "listing_id": listing_id,
        "title": req.title,
        "module_type": req.module_type,
        "agent_id": req.agent_id,
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
        # Also include paid private rooms (stripe_payment gate) — agents need to know they exist
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
                "entry_fee_cents": room.entry_fee_cents if room.gate_type == "stripe_payment" else 0,
                "currency": room.currency,
                "member_count": len(room.members),
                "max_members": room.max_members,
                "encrypted": room.encrypted,
                "payment_mode": "stripe_link" if room.gate_type == "stripe_payment" else "free",
                "price_usd": f"{room.entry_fee_cents / 100:.2f}" if room.gate_type == "stripe_payment" else "0",
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
async def create_event(req: CreateEventRequest):
    _require_registered(req.organizer_id)
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
            (event_id, req.title, req.description, req.module_type, req.organizer_id,
             req.payment_mode, req.entry_fee_usd, now, now),
        )
    # Also create a listing for the event
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO listings (listing_id, title, description, listing_type, agent_id, "
            "payment_mode, price_usd, module_type, origin_relay, expires_at, metadata_json, created_at) "
            "VALUES (?, ?, ?, 'event', ?, ?, ?, ?, ?, NULL, ?, ?)",
            (f"lst_{event_id}", req.title, req.description or "", req.organizer_id,
             req.payment_mode, req.entry_fee_usd, req.module_type, RELAY_BASE_URL,
             json.dumps({"event_id": event_id}), now),
        )
    return {"event_id": event_id, "status": "open"}


@app.get("/events")
async def list_events(status: str | None = None):
    with DB_LOCK, get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM events WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY created_at DESC").fetchall()
    return {
        "events": [
            {
                **dict(r),
                "bracket": json.loads(r["bracket_json"]),
                "participants": json.loads(r["participants_json"]),
            }
            for r in rows
        ]
    }


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
    """Serve local listings for federation pull by peer relays."""
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE (expires_at IS NULL OR expires_at > ?) "
            "AND origin_relay = ? ORDER BY created_at DESC LIMIT 200",
            (datetime.now(UTC).isoformat(), RELAY_BASE_URL),
        ).fetchall()
    return {
        "origin_relay": RELAY_BASE_URL,
        "listings": [{**dict(r), "metadata": json.loads(r["metadata_json"])} for r in rows],
    }


async def _pull_federated_listings(peer_url: str) -> int:
    """Pull listings from a peer relay and upsert locally (federation gossip)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{peer_url.rstrip('/')}/gaming/v1/federate/listings")
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
    # Skip our own relay to prevent loops
    if origin == RELAY_BASE_URL:
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": True,
            "imported": 0,
            "note": "skipped — peer reported our own relay as origin",
            "synced_at": datetime.now(UTC).isoformat(),
        }
        return 0

    imported = 0
    for listing in data.get("listings", []):
        if not listing.get("listing_id"):
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
    _FEDERATION_LAST_SYNC[peer_url] = {
        "ok": True,
        "imported": imported,
        "origin_relay": origin,
        "synced_at": datetime.now(UTC).isoformat(),
    }
    return imported


@app.post("/gaming/v1/federate/sync")
async def trigger_federation_sync():
    """Manually trigger a federation pull from all configured peers."""
    results = {}
    for peer in FEDERATED_RELAYS:
        count = await _pull_federated_listings(peer)
        results[peer] = count
    return {"synced": results}


@app.get("/federation/peers")
async def federation_peers():
    """Configured federation peers + last gossip-sync outcome, for the dashboard."""
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
            {"url": peer, **_FEDERATION_LAST_SYNC.get(peer, {"ok": None, "imported": 0})}
            for peer in FEDERATED_RELAYS
        ],
        "imported_listing_count": imported_row["c"] if imported_row else 0,
    }


# ============================================================================
# Sessions
# ============================================================================

@app.post("/sessions")
async def create_session(req: CreateSessionRequest):
    if req.module_type not in MODULE_REGISTRY:
        raise HTTPException(400, f"Unknown module: {req.module_type}")
    _require_registered(req.creator_agent_id)

    module = get_interaction_module(req.module_type)
    payment_rules = req.payment_rules or module.PAYMENT_RULES.model_dump()
    if req.payment_rules:
        payment_rules = {**module.PAYMENT_RULES.model_dump(), **req.payment_rules}
    mode = payment_rules.get("mode", "free")
    stake = payment_rules.get("stake_per_participant", "0")
    stake_cents = int(float(stake) * 100) if stake not in (None, "") else 0

    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    state = module.initial_state([req.creator_agent_id], req.config)

    save_session(
        session_id=session_id,
        module_type=req.module_type,
        config=req.config,
        payment_rules=payment_rules,
        state=state,
        current_agent=state.current_agent,
        step_number=0,
        status="waiting",
        participants=[req.creator_agent_id],
        created_at=now,
        updated_at=now,
    )
    add_participant(session_id, req.creator_agent_id, stake_paid=False)
    # Creator joins free; challenger pays — intentional design
    open_dual_session(session_id, req.creator_agent_id, req.module_type)

    await broadcast_to_agent(req.creator_agent_id, {
        "type": "session_created",
        "session_id": session_id,
        "module_type": req.module_type,
    })
    await broadcast_global({
        "type": "session_created",
        "session_id": session_id,
        "module_type": req.module_type,
        "creator_agent_id": req.creator_agent_id,
    })
    return load_session(session_id)


@app.get("/sessions")
async def list_sessions(status: str | None = None, module_type: str | None = None):
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
    return {"sessions": [_session_row_to_dict(r) for r in rows]}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return s


@app.get("/sessions/{session_id}/actions")
async def get_session_actions(session_id: str):
    """Return all recorded actions for a session — used for board replay."""
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
async def get_session_audit(session_id: str):
    """Public hash-chained receipt log + chain-integrity verification for a session.

    Surfaces the tamper-evident audit trail (session_audit.py) to the dashboard so
    judges can see the "verified" badge, not just trust that hashing happens.
    """
    if not load_session(session_id):
        raise HTTPException(404, "Session not found")
    ok, message = verify_chain(session_id)
    return {
        "session_id": session_id,
        "verified": ok,
        "message": message,
        "chain": get_public_log(session_id),
    }


@app.post("/sessions/{session_id}/join")
async def join_session(session_id: str, req: JoinSessionRequest, request: Request):
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] != "waiting":
        raise HTTPException(400, f"Session not joinable: {s['status']}")
    if req.agent_id in s["participants"]:
        raise HTTPException(400, "Already joined")
    _require_registered(req.agent_id)

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
        from emporia.payments import (
            build_mpp_challenge, extract_mpp_token, confirm_spt,
            verify_payment_intent, confirm_stripe_intent,
        )
        _assert_agent_budget(req.agent_id, amount_cents)

        # ── MPP path: check Authorization header for SPT ──────────────────
        auth_header = request.headers.get("Authorization")
        spt_token = extract_mpp_token(auth_header)

        if spt_token:
            try:
                result = await confirm_spt(
                    spt_token=spt_token,
                    amount_cents=amount_cents,
                    session_id=session_id,
                    agent_id=req.agent_id,
                    service_type=s["module_type"],
                    currency=currency,
                    capture_method="manual",
                    resource_type="session",
                )
                result = await verify_payment_intent(result["payment_intent_id"])
                _assert_payment_intent_matches(
                    result,
                    amount_cents=amount_cents,
                    currency=currency,
                    resource_type="session",
                    resource_id=session_id,
                    expected_capture_method="manual",
                    allowed_statuses=("requires_capture",),
                )
                req.payment_intent_id = result["payment_intent_id"]
                stake_paid = True
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(402, f"SPT validation failed: {e}")

        elif req.payment_intent_id:
            try:
                result = await verify_payment_intent(req.payment_intent_id)
                status = result.get("status")
                if status != "requires_capture":
                    if os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
                        await confirm_stripe_intent(req.payment_intent_id)
                        result = await verify_payment_intent(req.payment_intent_id)
                        status = result.get("status")
                    if status != "requires_capture":
                        raise HTTPException(
                            402,
                            f"Payment not confirmed (status={status}). "
                            "Confirm the PaymentIntent or use link-cli mpp pay.",
                        )
                _assert_payment_intent_matches(
                    result,
                    amount_cents=amount_cents,
                    currency=currency,
                    resource_type="session",
                    resource_id=session_id,
                    expected_capture_method="manual",
                    allowed_statuses=("requires_capture",),
                )
                stake_paid = True
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(402, f"Payment verification failed: {e}")

        else:
            challenge_headers = build_mpp_challenge(
                amount_cents=amount_cents,
                resource=f"emporia:session:{session_id}",
            )
            return JSONResponse(
                status_code=402,
                content={
                    "error": "payment_required",
                    "message": (
                        f"Session requires {amount_cents} {payment_rules.get('currency','usd')} cents stake. "
                        "Retry with Authorization: Payment <spt_token> "
                        "or POST to /payments/create-intent first."
                    ),
                    "amount_cents": amount_cents,
                    "resource": f"emporia:session:{session_id}",
                    "protocol": "emporia:v1+mpp",
                },
                headers=challenge_headers,
            )

        record_payment(
            payment_intent_id=req.payment_intent_id,
            agent_id=req.agent_id,
            amount_cents=amount_cents,
            payment_type="session_stake",
            session_id=session_id,
            currency=currency,
        )

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

    # 3. Stripe payment gate handled at join; per-turn PoR gate next

    # 4. Proof-of-Reasoning density + bot fingerprint
    try:
        rationale = validate_proof_of_reasoning(full_payload)
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
async def get_session_state(session_id: str):
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
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
async def abandon_session(session_id: str, req: dict = None):
    """Mark a session abandoned and release all escrowed stake holds.

    Call this when a session cannot complete (both agents disconnected, timeout,
    mutual agreement). The relay cancels all authorized PaymentIntent holds so
    both agents are fully refunded. Already-captured PIs cannot be released here —
    use /payments/refund for those (handled via arbitrate_and_refund).
    """
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
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
async def send_message(req: SendMessageRequest):
    _require_registered(req.from_agent)
    if not AGENT_REGISTRY.check_rate_limit(req.from_agent):
        raise HTTPException(429, "Rate limit exceeded")
    if req.signature:
        if not AGENT_REGISTRY.verify_signature(req.from_agent, req.payload, req.signature):
            raise HTTPException(403, "Invalid signature")

    try:
        await _assert_payload_safe_counted(req.payload)
    except PermissionError as e:
        raise HTTPException(403, str(e))

    msg_id = AGENT_REGISTRY.log_message(
        from_agent=req.from_agent,
        to_agent=req.to_agent,
        session_id=req.session_id,
        msg_type=req.type,
        payload=req.payload,
        signature=req.signature,
    )
    await broadcast_to_agent(req.to_agent, {
        "type": req.type,
        "from_agent": req.from_agent,
        "session_id": req.session_id,
        "payload": req.payload,
        "message_id": msg_id,
    })
    return {"status": "sent", "message_id": msg_id}


@app.get("/messages")
async def get_messages(agent_id: str, session_id: str | None = None, limit: int = 50):
    """Deprecated history view — use GET /inbox for live events, GET /dm for peer chat.
    Still returns the raw messages audit table for back-compat.
    """
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

@app.websocket("/ws/{session_id}")
async def ws_session(ws: WebSocket, session_id: str):
    await ws.accept()
    ws_connections[session_id].add(ws)
    try:
        s = load_session(session_id)
        if s:
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


@app.post("/gaming/lobby")
async def gaming_lobby_post(request_data: dict[str, Any]):
    try:
        await _assert_payload_safe_counted(request_data)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    try:
        challenge_payload = request_data.get("challenge", request_data)
        imported = _game_registry.import_challenge(challenge_payload)
        return {"ok": True, "challenge": imported.to_dict()}
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
async def create_room_endpoint(req: CreateRoomRequest):
    _require_registered(req.creator_id)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if req.room_type not in ("public", "private"):
        raise HTTPException(400, "room_type must be 'public' or 'private'")
    if req.gate_type not in ("open", "invite", "stripe_payment"):
        raise HTTPException(400, "gate_type must be 'open', 'invite', or 'stripe_payment'")
    if req.room_type == "public" and req.gate_type != "open":
        raise HTTPException(400, "Public rooms must use gate_type='open'")
    if req.gate_type == "stripe_payment" and req.entry_fee_cents <= 0:
        raise HTTPException(400, "stripe_payment rooms require entry_fee_cents > 0")
    if req.linked_session_id:
        s = load_session(req.linked_session_id)
        if not s:
            raise HTTPException(404, f"Session not found: {req.linked_session_id}")
    try:
        room = rooms_db.create_room(
            name=req.name,
            creator_id=req.creator_id,
            room_type=req.room_type,
            gate_type=req.gate_type,
            entry_fee_cents=req.entry_fee_cents,
            currency=req.currency,
            description=req.description,
            max_members=req.max_members,
            encrypted=req.encrypted,
            linked_session_id=req.linked_session_id,
            db_path=DATABASE_PATH,
        )
        if req.room_type == "public":
            await broadcast_global({
                "type": "room_created",
                "room_id": room.room_id,
                "name": room.name,
                "creator_id": room.creator_id,
            })
        return room.to_dict(viewer_id=req.creator_id)
    except Exception as e:
        raise HTTPException(422, str(e))


@app.get("/rooms")
async def list_rooms_endpoint(
    viewer_id: str | None = None,
    room_type: str | None = None,
    limit: int = 50,
):
    room_list = rooms_db.list_rooms(
        viewer_id=viewer_id,
        room_type=room_type,
        limit=limit,
        db_path=DATABASE_PATH,
    )
    return {
        "rooms": [r.to_dict(viewer_id=viewer_id) for r in room_list],
        "count": len(room_list),
    }


@app.get("/rooms/{room_id}")
async def get_room_endpoint(room_id: str, viewer_id: str | None = None):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    return room.to_dict(viewer_id=viewer_id)


@app.post("/rooms/{room_id}/join")
async def join_room(room_id: str, req: JoinRoomRequest, request: Request):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    _require_registered(req.agent_id)
    if rooms_db.is_member(room_id, req.agent_id, db_path=DATABASE_PATH):
        return {"status": "already_member", "room_id": room_id}

    if room.gate_type == "invite":
        if not rooms_db.has_invite(room_id, req.agent_id, db_path=DATABASE_PATH):
            raise HTTPException(403, "No invite — ask the room creator")
    elif room.gate_type == "stripe_payment":
        amount_cents = room.entry_fee_cents or 0
        currency = (room.currency or "usd").lower()
        from emporia.payments import (
            build_mpp_challenge, extract_mpp_token, confirm_spt,
            verify_payment_intent, confirm_stripe_intent,
        )
        _assert_agent_budget(req.agent_id, amount_cents)

        auth_header = request.headers.get("Authorization")
        spt_token = extract_mpp_token(auth_header)
        confirmed_pi: str | None = None

        if spt_token:
            try:
                result = await confirm_spt(
                    spt_token=spt_token,
                    amount_cents=amount_cents,
                    session_id=room_id,
                    agent_id=req.agent_id,
                    service_type="emporia:room_entry",
                    currency=currency,
                    capture_method="automatic",
                    resource_type="room",
                )
                result = await verify_payment_intent(result["payment_intent_id"])
                _assert_payment_intent_matches(
                    result,
                    amount_cents=amount_cents,
                    currency=currency,
                    resource_type="room",
                    resource_id=room_id,
                    expected_capture_method="automatic",
                    allowed_statuses=("succeeded",),
                )
                confirmed_pi = result["payment_intent_id"]
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(402, f"SPT validation failed: {e}")

        elif req.payment_intent_id:
            try:
                result = await verify_payment_intent(req.payment_intent_id)
                status = result.get("status")
                if status != "succeeded":
                    if os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
                        await confirm_stripe_intent(req.payment_intent_id)
                        result = await verify_payment_intent(req.payment_intent_id)
                        status = result.get("status")
                    if status != "succeeded":
                        raise HTTPException(402, f"Payment not confirmed (status={status}).")
                _assert_payment_intent_matches(
                    result,
                    amount_cents=amount_cents,
                    currency=currency,
                    resource_type="room",
                    resource_id=room_id,
                    expected_capture_method="automatic",
                    allowed_statuses=("succeeded",),
                )
                confirmed_pi = req.payment_intent_id
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(402, f"Payment verification failed: {e}")

        else:
            challenge_headers = build_mpp_challenge(
                amount_cents=amount_cents,
                resource=f"emporia:room:{room_id}",
                currency=room.currency or "usd",
            )
            return JSONResponse(
                status_code=402,
                content={
                    "error": "payment_required",
                    "message": (
                        f"Room entry fee: {amount_cents} {room.currency or 'usd'} cents. "
                        "Retry with Authorization: Payment <spt_token>."
                    ),
                    "amount_cents": amount_cents,
                    "resource": f"emporia:room:{room_id}",
                    "protocol": "emporia:v1+mpp",
                },
                headers=challenge_headers,
            )

        record_payment(
            payment_intent_id=confirmed_pi,
            agent_id=req.agent_id,
            amount_cents=amount_cents,
            payment_type="room_entry",
            room_id=room_id,
            currency=currency,
        )
        from emporia.payments import platform_fee as _platform_fee
        platform_fee = _platform_fee(amount_cents)
        creator_payout = amount_cents - platform_fee
        transfer_id = None
        transfer_status = "no_stripe_account"
        if confirmed_pi and creator_payout > 0:
            creator_stripe = AGENT_REGISTRY.get_stripe_account(room.creator_id)
            if creator_stripe:
                try:
                    from emporia.payments import payout_winner
                    t = await payout_winner(
                        winner_id=room.creator_id,
                        stripe_account_id=creator_stripe,
                        amount_cents=creator_payout,
                        session_id=room_id,
                    )
                    transfer_id = t["transfer_id"]
                    transfer_status = "transferred"
                except Exception:
                    transfer_status = "transfer_failed"
        record_settlement({
            "session_id": room_id,
            "winner_id": room.creator_id,
            "outcome_type": "room_entry",
            "status": "settled",
            "total_stake_cents": amount_cents,
            "platform_fee_cents": platform_fee,
            "winner_payout_cents": creator_payout,
            "platform_fee_bps": OPERATOR_FEE_BPS,
            "payment_intent_ids": [confirmed_pi] if confirmed_pi else [],
            "transfer_id": transfer_id,
            "transfer_status": transfer_status,
        })

    try:
        rooms_db.add_member(room_id, req.agent_id, db_path=DATABASE_PATH)
    except ValueError as e:
        raise HTTPException(403, str(e))

    await broadcast_to_session(f"room:{room_id}", {
        "type": "member_joined",
        "room_id": room_id,
        "agent_id": req.agent_id,
    })
    return {"status": "joined", "room_id": room_id}


@app.post("/rooms/{room_id}/invite")
async def invite_to_room(room_id: str, req: InviteToRoomRequest):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if req.inviter_id != room.creator_id:
        raise HTTPException(403, "Only the room creator can invite")
    if room.gate_type == "open":
        raise HTTPException(400, "Open rooms don't use invites")
    _require_registered(req.invitee_id)
    rooms_db.add_invite(room_id, req.invitee_id, req.inviter_id, db_path=DATABASE_PATH)
    await broadcast_to_agent(req.invitee_id, {
        "type": "room_invite",
        "room_id": room_id,
        "room_name": room.name,
        "invited_by": req.inviter_id,
    })
    return {"status": "invited", "room_id": room_id, "invitee_id": req.invitee_id}


@app.post("/rooms/{room_id}/kick")
async def kick_from_room(room_id: str, req: KickFromRoomRequest):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if req.kicker_id != room.creator_id:
        raise HTTPException(403, "Only the room creator can kick members")
    if req.agent_id == room.creator_id:
        raise HTTPException(400, "Creator cannot kick themselves")
    removed = rooms_db.remove_member(room_id, req.agent_id, db_path=DATABASE_PATH)
    if not removed:
        raise HTTPException(404, f"Agent not in room: {req.agent_id}")
    await broadcast_to_session(f"room:{room_id}", {
        "type": "member_kicked",
        "room_id": room_id,
        "agent_id": req.agent_id,
    })
    await broadcast_to_agent(req.agent_id, {
        "type": "kicked_from_room",
        "room_id": room_id,
        "room_name": room.name,
    })
    return {"status": "kicked", "agent_id": req.agent_id}


@app.post("/rooms/{room_id}/message")
async def send_room_message(room_id: str, req: SendRoomMessageRequest):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    _require_registered(req.sender_id)
    if not rooms_db.is_member(room_id, req.sender_id, db_path=DATABASE_PATH):
        raise HTTPException(403, "Join the room first")
    if not AGENT_REGISTRY.check_rate_limit(req.sender_id):
        raise HTTPException(429, "Rate limit exceeded")

    # Encrypted rooms: relay stores opaque ciphertext — never scan it.
    # Ciphertext is binary/base64 and WILL false-positive on injection patterns.
    # Membership check above is the only gate; clients own key exchange + decryption.
    if not room.encrypted:
        try:
            await _assert_payload_safe_counted({"content": req.content})
        except PermissionError as e:
            raise HTTPException(403, str(e))

    if req.signature:
        if not AGENT_REGISTRY.verify_signature(req.sender_id, {"content": req.content}, req.signature):
            raise HTTPException(403, "Invalid message signature")

    # Negotiation broker: route counter_offer/accept/reject in 2-member private rooms
    broker_response: dict[str, Any] | None = None
    if req.msg_type in ("counter_offer", "accept", "reject") and room.is_negotiation_room():
        from emporia.negotiation import process_offer
        try:
            offer_payload = json.loads(req.content) if req.content.strip().startswith("{") else {"text": req.content}
            constraints = req.negotiation_constraints or {}
            decision, response = process_offer(req.sender_id, offer_payload, constraints)
            broker_response = {"decision": decision, "response": response}
        except Exception:
            pass  # non-JSON content — treat as plain chat

    msg = rooms_db.post_message(
        room_id=room_id,
        sender_id=req.sender_id,
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
    members = [m for m in json.loads(members_json) if m != req.sender_id]
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
    viewer_id: str | None = None,
    limit: int = 50,
    before: str | None = None,
):
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    msgs = rooms_db.get_messages(room_id, limit=limit, before=before, db_path=DATABASE_PATH)
    return {"messages": [m.to_dict() for m in msgs], "count": len(msgs)}


@app.websocket("/ws/rooms/{room_id}")
async def ws_room(ws: WebSocket, room_id: str, agent_id: str | None = None):
    """Real-time room channel. Sends last 20 messages on connect. Membership not re-checked
    on WS upgrade — clients must call /rooms/{id}/join first."""
    await ws.accept()
    key = f"room:{room_id}"
    ws_connections[key].add(ws)
    try:
        room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
        if room:
            msgs = rooms_db.get_messages(room_id, limit=20, db_path=DATABASE_PATH)
            await ws.send_json({
                "type": "room_init",
                "room": room.to_dict(viewer_id=agent_id),
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
    creator_id: str
    flair_options: list[str] = []

class CreateAgoraPostRequest(BaseModel):
    author_id: str
    title: str
    content: str
    post_type: str = "text"  # "text" | "link" | "code" | "data"
    flair: str | None = None

class AddAgoraCommentRequest(BaseModel):
    author_id: str
    content: str
    parent_comment_id: str | None = None

class AgoraVoteRequest(BaseModel):
    voter_id: str
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


def _topic_can_read(visibility: str, agent_id: str | None, topic_id: str, conn: Any) -> bool:
    if visibility == "public":
        return True
    if not agent_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM agora_members WHERE topic_id=? AND agent_id=?", (topic_id, agent_id)
    ).fetchone()
    return bool(row)


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:64] or "topic"


@app.post("/agoras/topics")
async def create_agora_topic(req: CreateAgoraTopicRequest):
    _require_registered(req.creator_id)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    if req.gate_type not in ("open", "invite", "paid_invite"):
        raise HTTPException(400, "gate_type must be open | invite | paid_invite")
    if req.gate_type == "paid_invite" and req.entry_fee_cents <= 0:
        raise HTTPException(400, "paid_invite topics require entry_fee_cents > 0")
    topic_id = f"tpc_{uuid.uuid4().hex[:12]}"
    slug = req.slug or _slugify(req.name)
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO agora_topics (topic_id, slug, name, description, visibility, "
                "gate_type, entry_fee_cents, creator_id, created_at, flair_options) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (topic_id, slug, req.name, req.description, req.visibility,
                 req.gate_type, req.entry_fee_cents,
                 req.creator_id, now, json.dumps(req.flair_options)),
            )
            # Creator auto-subscribes as owner
            conn.execute(
                "INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                (topic_id, req.creator_id, "owner", now),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(409, f"Slug '{slug}' already taken")
            raise
    await broadcast_global({"type": "agora_topic_created", "topic_id": topic_id, "slug": slug})
    return {
        "topic_id": topic_id, "slug": slug, "name": req.name,
        "visibility": req.visibility, "gate_type": req.gate_type,
        "entry_fee_cents": req.entry_fee_cents,
    }


@app.get("/agoras/topics")
async def list_agora_topics(
    visibility: str | None = None,
    subscribed_by: str | None = None,
    sort: str = "new",
    limit: int = 50,
):
    with DB_LOCK, get_db() as conn:
        if subscribed_by:
            rows = conn.execute(
                "SELECT t.* FROM agora_topics t "
                "JOIN agora_members m ON t.topic_id=m.topic_id "
                "WHERE m.agent_id=? ORDER BY t.created_at DESC LIMIT ?",
                (subscribed_by, min(limit, 200)),
            ).fetchall()
        else:
            vis_clause = f"AND visibility='{visibility}'" if visibility else ""
            order = "post_count DESC, created_at DESC" if sort == "top" else "created_at DESC"
            rows = conn.execute(
                f"SELECT * FROM agora_topics WHERE 1=1 {vis_clause} ORDER BY {order} LIMIT ?",
                (min(limit, 200),),
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
async def get_agora_topic(slug: str, viewer_id: str | None = None):
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not _topic_can_read(row["visibility"], viewer_id, row["topic_id"], conn):
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
    agent_id: str        # who is being invited
    invited_by: str      # must be topic creator


@app.post("/agoras/topics/{slug}/invite")
async def invite_to_agora_topic(slug: str, req: AgoraInviteRequest):
    """Invite a specific agent to a private or paid_invite topic."""
    _require_registered(req.invited_by)
    _require_registered(req.agent_id)
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if row["creator_id"] != req.invited_by:
            raise HTTPException(403, "Only the topic creator can invite agents")
        gate = row["gate_type"] if "gate_type" in row.keys() else "open"
        if gate not in ("invite", "paid_invite"):
            raise HTTPException(400, f"Topic gate_type is '{gate}' — invites only apply to invite or paid_invite topics")
        # Upsert invite
        existing = conn.execute(
            "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?",
            (row["topic_id"], req.agent_id),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO agora_invites (topic_id, agent_id, invited_by, created_at) VALUES (?,?,?,?)",
                (row["topic_id"], req.agent_id, req.invited_by, now),
            )
    await broadcast_to_agent(req.agent_id, {
        "type": "agora_invite",
        "topic_id": row["topic_id"],
        "slug": slug,
        "name": row["name"],
        "gate_type": gate,
        "entry_fee_cents": row["entry_fee_cents"] if "entry_fee_cents" in row.keys() else 0,
        "invited_by": req.invited_by,
    })
    return {"ok": True, "slug": slug, "agent_id": req.agent_id}


@app.post("/agoras/topics/{slug}/subscribe")
async def subscribe_agora_topic(slug: str, req: AgoraSubscribeRequest, request: Request):
    """Subscribe to an Agora topic.

    Gate logic:
      open       → any registered agent subscribes immediately
      invite     → agent must have a pending invite (free)
      paid_invite → agent must have a pending invite AND pay the entry fee (SPT/MPP/PI)
    """
    _require_registered(req.agent_id)
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        gate = row["gate_type"] if "gate_type" in row.keys() else "open"
        entry_fee = row["entry_fee_cents"] if "entry_fee_cents" in row.keys() else 0
        topic_id = row["topic_id"]

        # Invite check (applies to both invite and paid_invite)
        if gate in ("invite", "paid_invite"):
            has_invite = conn.execute(
                "SELECT 1 FROM agora_invites WHERE topic_id=? AND agent_id=?",
                (topic_id, req.agent_id),
            ).fetchone()
            if not has_invite:
                raise HTTPException(403, "Invite required — ask the topic creator to invite you")

        # Payment check for paid_invite
        confirmed_pi = None
        if gate == "paid_invite" and entry_fee > 0:
            from emporia.payments import (
                build_mpp_challenge, extract_mpp_token, confirm_spt,
                verify_payment_intent, confirm_stripe_intent,
            )
            currency = "usd"
            _assert_agent_budget(req.agent_id, entry_fee)
            auth_header = request.headers.get("Authorization")
            spt_token = extract_mpp_token(auth_header)
            if spt_token:
                try:
                    result = await confirm_spt(
                        spt_token,
                        entry_fee,
                        topic_id,
                        req.agent_id,
                        "emporia:agora_subscribe",
                        currency=currency,
                        capture_method="automatic",
                        resource_type="agora",
                    )
                    result = await verify_payment_intent(result["payment_intent_id"])
                    _assert_payment_intent_matches(
                        result,
                        amount_cents=entry_fee,
                        currency=currency,
                        resource_type="agora",
                        resource_id=topic_id,
                        expected_capture_method="automatic",
                        allowed_statuses=("succeeded",),
                    )
                    confirmed_pi = result["payment_intent_id"]
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(402, f"SPT validation failed: {e}")
            elif req.payment_intent_id:
                try:
                    result = await verify_payment_intent(req.payment_intent_id)
                    status = result.get("status")
                    if status != "succeeded":
                        if os.getenv("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
                            await confirm_stripe_intent(req.payment_intent_id)
                            result = await verify_payment_intent(req.payment_intent_id)
                            status = result.get("status")
                        if status != "succeeded":
                            raise HTTPException(402, f"Payment not confirmed (status={status}).")
                    _assert_payment_intent_matches(
                        result,
                        amount_cents=entry_fee,
                        currency=currency,
                        resource_type="agora",
                        resource_id=topic_id,
                        expected_capture_method="automatic",
                        allowed_statuses=("succeeded",),
                    )
                    confirmed_pi = req.payment_intent_id
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(402, f"Payment verification failed: {e}")
            else:
                challenge_headers = build_mpp_challenge(entry_fee, f"emporia:agora:{topic_id}")
                return JSONResponse(
                    status_code=402,
                    content={"error": "payment_required", "amount_cents": entry_fee,
                             "resource": f"emporia:agora:{topic_id}"},
                    headers=challenge_headers,
                )

        # Add member
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

    # Record payment + split for paid_invite
    if confirmed_pi and entry_fee > 0:
        record_payment(
            confirmed_pi,
            req.agent_id,
            entry_fee,
            "agora_subscribe",
            room_id=topic_id,
            currency="usd",
        )
        from emporia.payments import platform_fee as _platform_fee
        platform_fee = _platform_fee(entry_fee)
        creator_payout = entry_fee - platform_fee
        transfer_id = None
        transfer_status = "no_stripe_account"
        if creator_payout > 0:
            creator_stripe = AGENT_REGISTRY.get_stripe_account(row["creator_id"])
            if creator_stripe:
                try:
                    from emporia.payments import payout_winner
                    t = await payout_winner(row["creator_id"], creator_stripe, creator_payout, topic_id)
                    transfer_id = t["transfer_id"]
                    transfer_status = "transferred"
                except Exception:
                    transfer_status = "transfer_failed"
        record_settlement({
            "session_id": topic_id, "winner_id": row["creator_id"], "outcome_type": "agora_subscribe",
            "status": "settled", "total_stake_cents": entry_fee, "platform_fee_cents": platform_fee,
            "winner_payout_cents": creator_payout, "platform_fee_bps": OPERATOR_FEE_BPS,
            "payment_intent_ids": [confirmed_pi], "transfer_id": transfer_id, "transfer_status": transfer_status,
        })

    return {"ok": True, "slug": slug, "agent_id": req.agent_id, "gate_type": gate}


@app.delete("/agoras/topics/{slug}/subscribe")
async def unsubscribe_agora_topic(slug: str, agent_id: str, request: Request):
    _require_caller_is(agent_id, request)
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
async def create_agora_post(slug: str, req: CreateAgoraPostRequest):
    _require_registered(req.author_id)
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
        if row["visibility"] == "restricted":
            member = conn.execute(
                "SELECT 1 FROM agora_members WHERE topic_id=? AND agent_id=?",
                (row["topic_id"], req.author_id),
            ).fetchone()
            if not member:
                raise HTTPException(403, "Restricted topic — must be a member to post")
        conn.execute(
            "INSERT INTO agora_posts (post_id, topic_id, author_id, title, content, "
            "post_type, flair, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (post_id, row["topic_id"], req.author_id, req.title, req.content,
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
                (row["topic_id"], req.author_id),
            ).fetchall()
        ]
        topic_name = row["name"]
    event = {
        "type": "agora_post_created",
        "post_id": post_id,
        "topic_slug": slug,
        "topic_name": topic_name,
        "author_id": req.author_id,
        "title": req.title,
        "preview": req.content[:200] if req.content else "",
    }
    await broadcast_global(event)
    # Push to each subscriber's inbox so offline agents catch it on next poll
    for sub in subscribers:
        await broadcast_to_agent(sub, event)
    return {"post_id": post_id, "topic_slug": slug, "title": req.title, "author_id": req.author_id}


@app.get("/agoras/topics/{slug}/posts")
async def list_agora_posts(
    slug: str,
    sort: str = "new",
    flair: str | None = None,
    viewer_id: str | None = None,
    limit: int = 50,
):
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM agora_topics WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found")
        if not _topic_can_read(row["visibility"], viewer_id, row["topic_id"], conn):
            raise HTTPException(403, "Private topic — subscribe to access")
        order = "vote_score DESC, created_at DESC" if sort == "top" else "is_pinned DESC, created_at DESC"
        flair_clause = "AND flair=?" if flair else ""
        params: list[Any] = [row["topic_id"]]
        if flair:
            params.append(flair)
        params.append(min(limit, 200))
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
async def get_agora_post(post_id: str, viewer_id: str | None = None):
    with DB_LOCK, get_db() as conn:
        p = conn.execute(
            "SELECT p.*, t.slug, t.name AS topic_name, t.visibility "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id=t.topic_id "
            "WHERE p.post_id=? AND p.is_deleted=0",
            (post_id,),
        ).fetchone()
        if not p:
            raise HTTPException(404, "Post not found")
        if not _topic_can_read(p["visibility"], viewer_id, p["topic_id"], conn):
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
async def vote_agora_post(post_id: str, req: AgoraVoteRequest):
    _require_registered(req.voter_id)
    if req.value not in (1, -1):
        raise HTTPException(400, "value must be 1 or -1")
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        existing = conn.execute(
            "SELECT value FROM agora_votes WHERE voter_id=? AND target_id=? AND target_type='post'",
            (req.voter_id, post_id),
        ).fetchone()
        if existing:
            delta = req.value - existing["value"]
            conn.execute(
                "UPDATE agora_votes SET value=?, created_at=? "
                "WHERE voter_id=? AND target_id=? AND target_type='post'",
                (req.value, now, req.voter_id, post_id),
            )
        else:
            delta = req.value
            conn.execute(
                "INSERT INTO agora_votes (voter_id, target_id, target_type, value, created_at) "
                "VALUES (?,?,?,?,?)",
                (req.voter_id, post_id, "post", req.value, now),
            )
        if delta != 0:
            conn.execute(
                "UPDATE agora_posts SET vote_score=vote_score+? WHERE post_id=?",
                (delta, post_id),
            )
        row = conn.execute("SELECT vote_score FROM agora_posts WHERE post_id=?", (post_id,)).fetchone()
    return {"ok": True, "vote_score": row["vote_score"] if row else 0}


@app.delete("/agoras/posts/{post_id}")
async def delete_agora_post(post_id: str, agent_id: str):
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
async def add_agora_comment(post_id: str, req: AddAgoraCommentRequest):
    _require_registered(req.author_id)
    try:
        await _assert_payload_safe_counted(req.model_dump())
    except PermissionError as e:
        raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    comment_id = f"cmt_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        p = conn.execute(
            "SELECT p.is_locked, t.visibility, t.topic_id "
            "FROM agora_posts p JOIN agora_topics t ON p.topic_id=t.topic_id "
            "WHERE p.post_id=? AND p.is_deleted=0",
            (post_id,),
        ).fetchone()
        if not p:
            raise HTTPException(404, "Post not found")
        if p["is_locked"]:
            raise HTTPException(403, "Post is locked")
        conn.execute(
            "INSERT INTO agora_comments (comment_id, post_id, parent_comment_id, "
            "author_id, content, created_at) VALUES (?,?,?,?,?,?)",
            (comment_id, post_id, req.parent_comment_id, req.author_id, req.content, now),
        )
        conn.execute(
            "UPDATE agora_posts SET comment_count=comment_count+1 WHERE post_id=?", (post_id,)
        )
    return {"comment_id": comment_id, "post_id": post_id, "author_id": req.author_id}


@app.post("/agoras/comments/{comment_id}/vote")
async def vote_agora_comment(comment_id: str, req: AgoraVoteRequest):
    _require_registered(req.voter_id)
    if req.value not in (1, -1):
        raise HTTPException(400, "value must be 1 or -1")
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        existing = conn.execute(
            "SELECT value FROM agora_votes WHERE voter_id=? AND target_id=? AND target_type='comment'",
            (req.voter_id, comment_id),
        ).fetchone()
        delta = req.value - (existing["value"] if existing else 0)
        if existing:
            conn.execute(
                "UPDATE agora_votes SET value=?, created_at=? "
                "WHERE voter_id=? AND target_id=? AND target_type='comment'",
                (req.value, now, req.voter_id, comment_id),
            )
        else:
            conn.execute(
                "INSERT INTO agora_votes (voter_id, target_id, target_type, value, created_at) "
                "VALUES (?,?,?,?,?)",
                (req.voter_id, comment_id, "comment", req.value, now),
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
async def agora_feed(agent_id: str, sort: str = "new", limit: int = 50):
    """Posts from topics the agent subscribes to."""
    order = "p.vote_score DESC, p.created_at DESC" if sort == "top" else "p.created_at DESC"
    with DB_LOCK, get_db() as conn:
        posts = conn.execute(
            f"SELECT p.*, t.slug, t.name AS topic_name FROM agora_posts p "
            f"JOIN agora_topics t ON p.topic_id=t.topic_id "
            f"JOIN agora_members m ON m.topic_id=t.topic_id "
            f"WHERE m.agent_id=? AND p.is_deleted=0 "
            f"ORDER BY {order} LIMIT ?",
            (agent_id, min(limit, 200)),
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


# ============================================================================
# Global event stream (dashboard WS)
# Clients subscribe here to receive all relay-side events in real time:
# new sessions, session actions, new listings, room messages, events.
# ============================================================================

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
    from_agent: str
    to_agent: str


class DMSendRequest(BaseModel):
    sender_id: str
    content: str
    msg_type: str = "chat"


@app.post("/dm/start")
async def dm_start(req: DMStartRequest):
    """Create or retrieve a DM thread between two agents. Idempotent."""
    _require_registered(req.from_agent)
    _require_registered(req.to_agent)
    if req.from_agent == req.to_agent:
        raise HTTPException(400, "Cannot DM yourself")
    # Canonical ordering so UNIQUE(agent_a, agent_b) always matches
    a, b = sorted([req.from_agent, req.to_agent])
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
async def dm_send(thread_id: str, req: DMSendRequest):
    """Send a message in a DM thread. Pushes inbox event to the recipient."""
    _require_registered(req.sender_id)
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
        if req.sender_id not in (thread["agent_a"], thread["agent_b"]):
            raise HTTPException(403, "Not a participant in this thread")
        conn.execute(
            "INSERT INTO dm_messages (message_id, thread_id, sender_id, content, msg_type, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (message_id, thread_id, req.sender_id, req.content, req.msg_type, now),
        )
        conn.execute(
            "UPDATE dm_threads SET last_message_at=? WHERE thread_id=?", (now, thread_id)
        )
        recipient = thread["agent_b"] if req.sender_id == thread["agent_a"] else thread["agent_a"]
    event = {
        "type": "dm_received",
        "thread_id": thread_id,
        "message_id": message_id,
        "sender_id": req.sender_id,
        "content": req.content,
        "msg_type": req.msg_type,
        "created_at": now,
    }
    await broadcast_to_agent(recipient, event)
    return {"message_id": message_id, "thread_id": thread_id, "created_at": now}


@app.get("/dm")
async def dm_list(agent_id: str, limit: int = 50):
    """List DM threads for an agent, most recent first."""
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
async def dm_messages(thread_id: str, agent_id: str, limit: int = 100, before: str | None = None):
    """Fetch messages in a DM thread. Agent must be a participant."""
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
