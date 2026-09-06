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
import logging
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
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
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
    verify_chain_entries,
)
from emporia import __version__
from emporia import rooms as rooms_db
from emporia import entitlements
from emporia.env_config import env_int
from emporia.logging_config import configure_logging, configure_sentry, request_id_ctx

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

# P22: module-import-time, not inside main() -- `python -m uvicorn
# emporia.relay_server:app` (the documented local-dev path, scripts/local_relay.py)
# imports this module and serves `app` directly, never calling main() at all.
# Configuring here is the only way that covers every real invocation path.
configure_logging()
configure_sentry()
logger = logging.getLogger("emporia.relay")

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


def _stripe_key_mode() -> str:
    """P26-4: which Stripe mode this relay's configured key talks to —
    `"test"` | `"live"` | `""` (not configured / unrecognizable).

    Connected Account ids don't encode their own mode, and the two key spaces
    are completely disjoint: an `acct_...` created under a test key does not
    exist under a live key. That makes the mode a thing the relay has to track
    itself rather than infer from an id — see the `stripe_account_mode` column.

    Delegates to `payments.stripe_key_mode()` rather than re-deriving the
    prefix rule: `settle()` applies the same rule and can't import this module,
    so a second copy here is precisely the hand-copied-logic drift this
    codebase has found in four separate passes.
    """
    from emporia.payments import stripe_key_mode

    return stripe_key_mode()


def _stripe_account_mode_mismatch(account_mode: str) -> bool:
    """Thin re-export of `payments.stripe_account_mode_mismatch()` so both
    payout call sites (that one, and `_settle_access_payment` here) apply one
    rule rather than two hand-copied ones."""
    from emporia.payments import stripe_account_mode_mismatch

    return stripe_account_mode_mismatch(account_mode)


def _verify_stripe_webhook_signature(
    payload: bytes, sig_header: str, secret: str, *, tolerance_seconds: int = 300,
) -> bool:
    """Hand-rolled verification of Stripe's `Stripe-Signature` header scheme
    (`t=<unix-ts>,v1=<hex-hmac-sha256>[,v1=...]`) — matches this codebase's
    established raw-httpx/no-SDK posture toward Stripe (`payments.py` never
    imports the `stripe` package; P25-2 stays consistent). A payload can carry
    more than one `v1` value during Stripe's own secret-rotation window, so any
    match is accepted. Rejects a missing/malformed header, a wrong signature,
    and a timestamp outside Stripe's documented ~5-minute replay-tolerance
    window. Never fails open: an empty secret or any parse error is a reject,
    not a skip.

    P26-2: that last sentence was the *intent* in P25 but not the behavior —
    two inputs raised instead of returning False, and since this app registers
    no exception handler anywhere, both surfaced as an unauthenticated 500 with
    a traceback rather than a 403: a `t` of a few hundred digits overflowed
    `abs(time.time() - ts)` (`int` parses fine, the float subtraction doesn't),
    and any non-ASCII byte in a `v1` value raised `TypeError` out of
    `hmac.compare_digest`. Not a bypass — neither produces a valid signature —
    but it handed any anonymous caller a log-flood primitive and broke the
    stated contract. The whole body is now total: anything unexpected is a
    reject. Deliberately a blanket `except Exception`, since this function's
    only correct answer to *any* input it can't fully parse is False.
    """
    try:
        if not secret or not sig_header:
            return False
        parts: dict[str, list[str]] = {}
        for item in sig_header.split(","):
            if "=" not in item:
                continue
            k, _, v = item.partition("=")
            parts.setdefault(k.strip(), []).append(v.strip())
        ts_values = parts.get("t")
        v1_values = parts.get("v1")
        if not ts_values or not v1_values:
            return False
        ts = int(ts_values[0])
        # Bound before the float subtraction: int() happily parses a 400-digit
        # timestamp that abs(time.time() - ts) then can't convert (P26-2).
        if abs(ts) > 10**12:
            return False
        if abs(time.time() - ts) > tolerance_seconds:
            return False
        signed_payload = f"{ts_values[0]}.".encode() + payload
        expected = _hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return any(_hmac.compare_digest(expected, v1) for v1 in v1_values)
    except Exception:
        return False


def _require_stripe_webhook_secret_if_needed() -> None:
    """Fail loud at startup (P25-2) rather than silently booting into a state
    where live-mode Connected Accounts could never be marked payable —
    POST /webhooks/stripe is the *only* thing that ever sets an account's
    status to 'active' in live mode, and it refuses every request without a
    secret to verify against (never fails open). Test-mode key: warn only —
    the route existing there is harmless (test-mode accounts are already
    marked 'active' at registration, see register_agent) unless someone is
    deliberately exercising live-mode onboarding locally.
    """
    sk = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    if sk.startswith("sk_live_") and not secret:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is a live key but STRIPE_WEBHOOK_SECRET is unset — "
            "POST /webhooks/stripe cannot verify Stripe's account.updated events "
            "without it, so live-mode Connected Accounts could never be marked "
            "payable. Set STRIPE_WEBHOOK_SECRET (from the Stripe Dashboard's "
            "webhook endpoint configuration) before starting in live mode."
        )
    if sk.startswith("sk_test_") and not secret:
        logger.warning(
            "STRIPE_WEBHOOK_SECRET is unset — POST /webhooks/stripe will reject "
            "every request until it's configured. Harmless in test mode unless "
            "you're exercising live-mode onboarding locally (e.g. Stripe CLI "
            "webhook forwarding)."
        )


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

# P21-1 (B1): a federation peer's response is streamed and capped at this many bytes
# *before* being buffered for signature verification or JSON parsing — see
# _fetch_federation_batch(). Default generous enough for a full _MAX_LIMIT-sized batch
# of listings/challenges with room to spare, small enough that a malicious or hijacked
# peer can't OOM the relay by returning an arbitrarily large body.
MAX_FEDERATION_RESPONSE_BYTES = env_int("MAX_FEDERATION_RESPONSE_BYTES", 8 * 1024 * 1024)


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

# P44: how many messages the chain-verification routes recompute per call.
# `rooms.verify_chain()` itself still defaults to the whole history — the cap is
# a policy about untrusted callers, so it lives at the route boundary where the
# untrusted caller is. GET /rooms/{id}/chain is unauthenticated for a public
# room, and an uncapped walk measured at 70ms for 20,000 messages; at the
# default 120 GET/min/IP that is 8.4 seconds of relay CPU per minute per IP, and
# the walk ran on the event loop, so it was 8.4 seconds of *everyone's* latency.
# The cap costs nothing that matters: `chain_head` and `message_count` — the two
# facts the response signs and a client pins — are indexed lookups, exact at any
# cap, and the relay's own recompute was never the load-bearing guarantee (see
# get_room_chain()'s docstring: client-side head pinning is).
CHAIN_VERIFY_LIMIT = int(os.getenv("EMPORIA_CHAIN_VERIFY_LIMIT", "1000"))


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
                x402_pay_to_json TEXT NOT NULL DEFAULT '{}',
                encryption_public_key TEXT NOT NULL DEFAULT '',
                encryption_key_signature TEXT NOT NULL DEFAULT ''
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

            -- P48: the pre-settlement intent record that closes x402's
            -- double-charge window. One row per (payer, resource, amount)
            -- purchase, written and committed BEFORE the facilitator's
            -- /settle is called and retired once the entitlement it bought
            -- exists. See _open_x402_intent() for the state machine and why
            -- `valid_before` is what makes an abandoned attempt safe to retry.
            CREATE TABLE IF NOT EXISTS x402_settle_intents (
                intent_key TEXT PRIMARY KEY,
                payer_id TEXT NOT NULL,
                payer_stable_id TEXT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                network TEXT NOT NULL DEFAULT '',
                asset TEXT NOT NULL DEFAULT '',
                authorizer TEXT NOT NULL DEFAULT '',
                nonce TEXT NOT NULL DEFAULT '',
                valid_before INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'settling',
                transaction_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

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
        """)
        # DM schema (direct agent-to-agent threads)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dm_threads (
                thread_id TEXT PRIMARY KEY,
                agent_a TEXT NOT NULL,
                agent_b TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_message_at TEXT NOT NULL,
                encrypted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(agent_a, agent_b)
            );
            CREATE TABLE IF NOT EXISTS dm_messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT NOT NULL DEFAULT 'chat',
                created_at TEXT NOT NULL,
                signature TEXT,
                nonce TEXT
            );

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
        _migrate_dm_threads(conn)
        _migrate_payments(conn)
        _migrate_settlements(conn)
        _migrate_settlement_dedupe_key(conn)
        _migrate_settlement_refund_columns(conn)
        # Rename aroga_* → agora_* if old names still exist (typo fix)
        _migrate_agora_tables(conn)
        # Rooms schema (separate DDL to keep clean)
        rooms_db.init_rooms_schema(conn)
        _migrate_rooms(conn)

        # P36-1: indexes come AFTER the column migrations, always. They used to
        # sit in the same executescript as the CREATE TABLE statements, which is
        # indistinguishable from correct on a fresh database -- the CREATE TABLE
        # carries every column -- and fatal on a pre-existing one, where CREATE
        # TABLE IF NOT EXISTS is a no-op and only the ALTERs below add the newer
        # columns. `idx_settlements_payer ON settlements(payer_id)` was the live
        # instance: it crashed startup on any relay older than N7.
        conn.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_inbox_agent ON agent_inbox(agent_id, is_read);
            CREATE INDEX IF NOT EXISTS idx_agora_posts_topic ON agora_posts(topic_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agora_posts_author ON agora_posts(author_id);
            CREATE INDEX IF NOT EXISTS idx_agora_comments_post ON agora_comments(post_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agora_members_agent ON agora_members(agent_id);
            CREATE INDEX IF NOT EXISTS idx_agora_topics_visibility ON agora_topics(visibility, created_at);
            CREATE INDEX IF NOT EXISTS idx_inbox_created ON agent_inbox(is_read, created_at);
            CREATE INDEX IF NOT EXISTS idx_dm_threads_a ON dm_threads(agent_a, last_message_at);
            CREATE INDEX IF NOT EXISTS idx_dm_threads_b ON dm_threads(agent_b, last_message_at);
            CREATE INDEX IF NOT EXISTS idx_dm_messages_thread ON dm_messages(thread_id, created_at);
        """)
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
    # P19-5: the sender-chosen per-message nonce that closes room replay, mirroring
    # dm_messages.nonce (P18-2, migrated the identical way here). Before this,
    # signed_room_message_payload() and the room AAD bound nothing unique to a
    # message, so a relay could copy one row's content+signature onto another and
    # both would verify and decrypt — the exact hole P18-2 closed for DMs, open
    # here because P18-4 shipped room E2E pointing at the pre-existing chain_hash
    # instead, which is relay-computed and verified by nothing client-side. NULL
    # allowed so pre-P19-5 rows survive; SQLite treats NULLs as distinct in a
    # UNIQUE index, so old rows never collide with each other or a new one.
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(room_messages)").fetchall()}
    if "nonce" not in msg_cols:
        conn.execute("ALTER TABLE room_messages ADD COLUMN nonce TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_room_messages_nonce "
        "ON room_messages(room_id, nonce) WHERE nonce IS NOT NULL"
    )


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
    P1a-2 fixed) are deleted, keeping the earliest by rowid, logged first so an
    unexpected hit is auditable.

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
        logger.info(
            "migrate_settlements_dedupe", extra={
                "dedupe_key": row["dedupe_key"], "duplicate_count": row["c"],
                "settlement_ids": row["ids"],
            },
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


def _migrate_dm_threads(conn: Any) -> None:
    """P16: whether a DM thread is end-to-end encrypted.

    `0` is correct for every pre-existing thread — none of them had any concept of
    encryption, and the flag is opt-in — so this is the column and nothing else, no
    backfill. Note there is deliberately no migration *off* this: the flag is
    one-way by design (see `dm_encrypt()`), and a schema that could clear it would
    be the force-plaintext path the route refuses to provide.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dm_threads)").fetchall()}
    if "encrypted" not in cols:
        conn.execute("ALTER TABLE dm_threads ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0")
    # P17-2: the sender's Ed25519 signature over the message, kept rather than
    # discarded after write-time verification — `room_messages` has stored one all
    # along (rooms.py), and DMs had no counterpart, so nothing could re-verify a DM
    # once it was in the table. NULL (not '') for every pre-P17 row: nothing was
    # stored, so nothing can be claimed about it, and clients report that as
    # `verified: None` — "unknown", not "bad".
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(dm_messages)").fetchall()}
    if "signature" not in msg_cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN signature TEXT")
    # P18-2: the sender-chosen per-message nonce that closes replay. The UNIQUE
    # index is the load-bearing half — it binds the relay's *own SQL*, not just its
    # HTTP handlers, so an operator copying a row's content+signature onto another
    # row hits an IntegrityError rather than succeeding silently. NULL is allowed so
    # pre-P18 rows survive; SQLite treats NULLs as distinct in a UNIQUE index, which
    # is what we want (many old rows, no nonce, no false collisions).
    if "nonce" not in msg_cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN nonce TEXT")
    # P28-4: the hash chain rooms have had since P18. `signature` and `nonce`
    # above give per-message authenticity and replay protection; neither can
    # detect a DELETED or REORDERED message, because every surviving row still
    # verifies on its own. Binding message N to N-1 is what does.
    #
    # NULL for every pre-P28 row, and deliberately no backfill: a chain computed
    # now over rows written before the chain existed would be indistinguishable
    # from one that had been maintained all along, which is a claim the relay
    # cannot honestly make about its own history. Same posture as `signature`
    # above — nothing was stored, so nothing is asserted. `_verify_dm_chain()`
    # reports the size of that unchained prefix rather than vouching for it.
    if "chain_hash" not in msg_cols:
        conn.execute("ALTER TABLE dm_messages ADD COLUMN chain_hash TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dm_messages_nonce "
        "ON dm_messages(thread_id, nonce) WHERE nonce IS NOT NULL"
    )


def _backfill_stripe_account_state(conn: Any) -> None:
    """P26-1: repair the state P25's migration left behind (see the column's own
    comment above for the money-stranding failure this prevents).

    P27-1: P26 gated this on `stripe_account_status` being newly added, which
    meant it only ever fired on a P24 -> P26 jump — and therefore missed every
    deployment that had actually run P25, i.e. exactly the ones H1 was about.
    Those relays already have the column, populated with the wrong value,
    because that mis-defaulting *is* H1. The gate now also fires when
    `stripe_account_mode` is newly added (the P25 -> P26 upgrade moment).

    That widens when this runs, so the safety no longer rests on the gate
    alone: the `stripe_account_status = 'none'` predicate makes the statement
    structurally incapable of touching a row that is already 'pending' or
    'active'. A live account mid-KYC keeps its 'pending', and a P26-era
    deauthorized account — which the P26 handler leaves as (id present, status
    'none'), the same shape repaired here — is protected by the gate, since a
    table that already has both columns triggers neither branch.

    Rows matched by the predicate can be dated precisely. Before P26 nothing
    wrote 'none' over an existing account id: auto-provisioning set 'active',
    live onboarding set 'pending', and the webhook only ever set 'active' or
    'pending'. So (id present, status 'none') can only have been produced by
    a column default landing on a row auto-provisioning had already filled —
    and that block was gated on `sk_test_`. 'test'/'active' is not a guess;
    it is the only state that path could have produced. That inference holds
    here and must not be assumed anywhere else.
    """
    conn.execute(
        "UPDATE authorized_agents SET stripe_account_status = 'active', "
        "stripe_account_mode = 'test' "
        "WHERE COALESCE(stripe_account_id, '') != '' AND stripe_account_status = 'none'"
    )


def _backfill_stable_ids(conn: Any) -> int:
    """Give every pre-P47 row a `stable_id`, derived from the key on file.

    A relay upgraded in place has rows registered before this column existed,
    and their **genesis** key is not recoverable — nothing ever recorded key
    history. So the key on file becomes the genesis key for those rows. That is
    a real approximation and is written down rather than glossed: an agent that
    rotated its key before P47 gets an id derived from its current key, not its
    first one. Every agent registered after P47 has a true genesis anchor.

    The alternative — leaving NULL — pushes a branch into every reader forever
    for the sake of a distinction no caller can act on.

    Returns how many rows it touched, so a caller can log it and so the tests
    can tell "backfilled nothing" from "backfilled correctly".
    """
    rows = conn.execute(
        "SELECT agent_id, public_key_hex FROM authorized_agents WHERE stable_id IS NULL"
    ).fetchall()
    touched = 0
    for row in rows:
        key = row["public_key_hex"] if not isinstance(row, tuple) else row[1]
        agent_id = row["agent_id"] if not isinstance(row, tuple) else row[0]
        if not key:
            continue
        try:
            derived = stable_id_for(key)
        except ValueError:
            # A malformed key on file can't produce an id. Left NULL rather
            # than crashing the migration for every other agent on the relay.
            logger.warning("stable_id backfill skipped %s: unparseable public key", agent_id)
            continue
        conn.execute(
            "UPDATE authorized_agents SET stable_id = ? WHERE agent_id = ? AND stable_id IS NULL",
            (derived, agent_id),
        )
        touched += 1
    return touched


def _migrate_authorized_agents(conn: Any) -> None:
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(authorized_agents)").fetchall()
    }
    migrations = [
        ("nous_user_id", "TEXT"),
        ("trust_level", "TEXT NOT NULL DEFAULT 'key_only'"),
        ("identity_providers_json", "TEXT NOT NULL DEFAULT '[]'"),
        # P16: the agent's X25519 public key for E2E DMs, plus the Ed25519
        # signature binding it to this agent_id. '' is the correct value for every
        # pre-existing row — "has not published one" is a normal state clients
        # branch on, not a missing migration — so no backfill, only the columns.
        ("encryption_public_key", "TEXT NOT NULL DEFAULT ''"),
        ("encryption_key_signature", "TEXT NOT NULL DEFAULT ''"),
        # P25-2: 'none' (no Connected Account) / 'pending' (created, capabilities
        # not yet active) / 'active' (transfers capability active).
        #
        # P26-1: P25 shipped this claiming 'none' was "already correct for every
        # pre-existing row." **That was wrong, and it stranded money.**
        # register_agent() only sets 'active' at *registration* time, so every
        # agent that registered before P25 kept a working Connected Account while
        # reading 'none' — and both of P25's new pre-checks skip a payout on
        # exactly that value. The charge still captures into the platform
        # balance; the payee's share is never transferred and nothing retries it.
        # The backfill below is the fix; see _backfill_stripe_account_state().
        ("stripe_account_status", "TEXT NOT NULL DEFAULT 'none'"),
        # P26-4: which Stripe mode created this account. '' = unknown. Account
        # ids don't encode their own mode, and a test-mode acct id is worthless
        # (worse: dangerous) under a live key — see _stripe_key_mode().
        ("stripe_account_mode", "TEXT NOT NULL DEFAULT ''"),
        # P47: the agent's permanent identity, distinct from its handle. NULL
        # (not '') for a row that predates this column, so the backfill below
        # can tell "never allocated" from "allocated" without a sentinel — see
        # _backfill_stable_ids(). Nullable rather than NOT NULL DEFAULT '': a
        # default would make every legacy row collide on the unique index.
        ("stable_id", "TEXT"),
    ]
    newly_added = set()
    for col, defn in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE authorized_agents ADD COLUMN {col} {defn}")
            newly_added.add(col)
    # P27-1: either column appearing marks an un-repaired generation --
    # `stripe_account_status` for a P24-era table, `stripe_account_mode` for a
    # relay that already ran P25 (and so already has the mis-defaulted status
    # column). A table that has both is fully migrated and triggers neither.
    if newly_added & {"stripe_account_status", "stripe_account_mode"}:
        _backfill_stripe_account_state(conn)
    if "stable_id" in newly_added:
        _backfill_stable_ids(conn)
    # P47: unique, and partial so legacy rows the backfill hasn't reached yet
    # (or rows a future migration deliberately leaves NULL) don't all collide
    # on NULL. Unique because two agents sharing an identity would be
    # indistinguishable to everything downstream — the mirror of the
    # taken-handle-under-a-different-key check `register()` already makes.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_stable_id "
        "ON authorized_agents(stable_id) WHERE stable_id IS NOT NULL"
    )
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
    from emporia.payments import _x402_networks, x402_network_family

    wallets = AGENT_REGISTRY.get_x402_pay_to_all(creator_id)
    result: dict[str, str] = {}
    for network in _x402_networks():
        family = x402_network_family(network)
        if not family:
            continue
        pay_to = wallets.get(family)
        if pay_to:
            result[network] = pay_to
    return result


# ---------------------------------------------------------------------------
# P48: the x402 settlement intent
#
# The gap this closes, recorded in _verify_x402_payment()'s docstring since
# P1b and made reachable by P45's payer: /settle moves real money and x402
# has no Idempotency-Key, so a relay that dies between a successful
# settlement and record_payment() leaves a payer who was charged, holds no
# entitlement and no membership, and whose retry signs a *fresh* nonce. Every
# defence in place caught the wrong thing — record_payment() dedupes on the
# transaction hash, and the chain's own nonce guard rejects a replay of the
# same authorization. Neither sees a second, genuinely valid settlement for
# one purchase.
#
# The fix is a durable record written before the money moves, which is what
# "an intent" means everywhere else in payments. It is keyed on the purchase
# (payer, resource, amount), not on the credential, precisely because the
# retry arrives with a different credential.
# ---------------------------------------------------------------------------

X402_INTENT_SETTLING = "settling"
X402_INTENT_SETTLED = "settled"
X402_INTENT_FAILED = "failed"


def _x402_intent_key(
    *, payer_id: str, resource_type: str, resource_id: str, amount_cents: int
) -> str:
    """The identity of a purchase, stable across the credential used to pay
    for it and across a rename mid-flight.

    Keyed on the payer's `stable_id` (P47) rather than the handle: a rename
    between the settlement and the retry would otherwise compute a different
    key for the same purchase and re-charge — exactly the case P47 built
    `stable_id` for. Falls back to the handle only for an agent with no
    stable_id at all, which after P47's backfill means an agent that isn't
    registered.
    """
    identity = AGENT_REGISTRY.get_stable_id(payer_id) or payer_id
    return hashlib.sha256(
        "|".join([
            "emporia:x402intent:v1", identity, resource_type, resource_id, str(amount_cents),
        ]).encode()
    ).hexdigest()


def _open_x402_intent(
    *,
    intent_key: str,
    payer_id: str,
    resource_type: str,
    resource_id: str,
    amount_cents: int,
    network: str,
    asset: str,
    authorizer: str,
    nonce: str,
    valid_before: int,
) -> dict[str, Any] | None:
    """Claim the right to settle this purchase, or report who already has it.

    Returns None when the claim is ours and settlement may proceed. Returns
    the existing row when someone got here first — the caller decides what
    that means, because the three cases are genuinely different:

      settled  — the money already moved for this purchase. Replay the
                 recorded transaction; never settle again.
      settling — an attempt is in flight or was abandoned mid-flight. Only
                 the chain can say which, and only after the authorization
                 it holds has expired (see `valid_before` below).
      failed   — a previous attempt was refused. Free to try again.

    `valid_before` is the abandoned-attempt safety interlock and the reason
    this is provable rather than merely likely. A stuck `settling` row could
    always be reconciled against the chain, but a reconciliation that says
    "not spent" races the in-flight settle it is asking about: the facilitator
    may land it a second later, after we have already decided to settle again.
    The EIP-3009 authorization carries its own deadline, so past
    `valid_before` that race cannot exist — the stored authorization is
    expired and the chain will reject it no matter who submits it. Waiting for
    that instant costs at most `maxTimeoutSeconds` and removes the window
    entirely, instead of narrowing it.
    """
    now = datetime.now(UTC).isoformat()
    payer_stable_id = AGENT_REGISTRY.get_stable_id(payer_id)
    with DB_LOCK, get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM x402_settle_intents WHERE intent_key = ?", (intent_key,)
        ).fetchone()
        if existing:
            return dict(existing)
        conn.execute(
            "INSERT INTO x402_settle_intents "
            "(intent_key, payer_id, payer_stable_id, resource_type, resource_id, "
            "amount_cents, network, asset, authorizer, nonce, valid_before, status, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent_key, payer_id, payer_stable_id,
                resource_type, resource_id, amount_cents, network, asset,
                authorizer, nonce, valid_before, X402_INTENT_SETTLING, now, now,
            ),
        )
    return None


def _reopen_x402_intent(
    *, intent_key: str, network: str, asset: str, authorizer: str,
    nonce: str, valid_before: int,
) -> None:
    """Take over an intent whose previous attempt is known not to have landed,
    re-pointing it at the credential we are about to settle."""
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "UPDATE x402_settle_intents SET status = ?, network = ?, asset = ?, "
            "authorizer = ?, nonce = ?, valid_before = ?, transaction_hash = NULL, "
            "updated_at = ? WHERE intent_key = ?",
            (
                X402_INTENT_SETTLING, network, asset, authorizer, nonce, valid_before,
                datetime.now(UTC).isoformat(), intent_key,
            ),
        )


def _close_x402_intent(
    intent_key: str, status: str, transaction_hash: str | None = None
) -> None:
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "UPDATE x402_settle_intents SET status = ?, transaction_hash = ?, "
            "updated_at = ? WHERE intent_key = ?",
            (status, transaction_hash, datetime.now(UTC).isoformat(), intent_key),
        )


def _retire_x402_intent(
    *, payer_id: str, resource_type: str, resource_id: str, amount_cents: int
) -> None:
    """Delete the intent for a purchase that is now durably recorded.

    Called once the entitlement exists, which is what makes a *later*
    legitimate re-purchase of the same resource — after the first
    entitlement expires — open a fresh intent instead of colliding with the
    settled one and being handed free access. The entitlement is the right
    retirement trigger because it is the thing the payment bought: while it
    is absent the purchase is unfinished and the intent must keep guarding
    it; once it exists the payment path isn't entered at all.
    """
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "DELETE FROM x402_settle_intents WHERE intent_key = ?",
            (_x402_intent_key(
                payer_id=payer_id, resource_type=resource_type,
                resource_id=resource_id, amount_cents=amount_cents,
            ),),
        )


async def _resolve_open_x402_intent(
    intent: dict[str, Any], challenge_resource: str
) -> str | None:
    """Decide what an already-open intent means for the request in hand.

    Returns a transaction reference when this purchase is already paid for —
    the caller returns it and never settles. Returns None when the previous
    attempt provably did not land and settling again is safe. Raises 402 when
    neither can be established, which is the only honest third answer: the
    payer may or may not have been charged, and both "charge them again" and
    "let them in free" are wrong.

    The chain is the idempotency record x402's /settle contract doesn't
    provide. EIP-3009 marks an authorization's nonce spent in a permanent
    per-authorizer map, so `authorizationState` answers "was this payer
    charged?" for as long as the token exists — no log retention window, no
    facilitator cooperation, and no trust in the facilitator's own account of
    what it did. `x402_authorization_used()` is deliberately tri-state; None
    means "couldn't ask", and this function must never round that down to
    False.
    """
    from emporia.payments import (
        x402_authorization_used, x402_authorization_transaction,
    )

    intent_key = intent["intent_key"]
    status = intent["status"]
    recorded_tx = intent["transaction_hash"]

    if status == X402_INTENT_SETTLED and recorded_tx:
        # The interrupted purchase, resumed. record_payment() is idempotent on
        # this reference, and _settle_access_payment() is exactly-once per
        # payment (P50), so replaying it finishes the join the crash cut short.
        #
        # P50 correction: this comment used to claim `entitlements.grant()` is
        # itself idempotent. It is not — it always inserts, deliberately, one
        # receipt row per payment. Nothing depended on the false half here
        # (the replay reaches grant at most once on this path), but it was the
        # premise P50 had to check before building a resume path on top of it.
        return recorded_tx
    if status == X402_INTENT_FAILED:
        return None

    used = await x402_authorization_used(
        intent["network"], intent["asset"], intent["authorizer"], intent["nonce"],
    )
    if used is True:
        # Charged. Recover the transaction hash if the log is still in range;
        # if it isn't, the authorization id is a true and unique reference for
        # the payment, which matters more than it being a tx hash — refusing
        # to record a payment we know happened would be the worse answer.
        tx = await x402_authorization_transaction(
            intent["network"], intent["asset"], intent["authorizer"], intent["nonce"],
        ) or f"x402-auth:{intent['authorizer']}:{intent['nonce']}"
        _close_x402_intent(intent_key, X402_INTENT_SETTLED, tx)
        return tx

    valid_before = int(intent["valid_before"] or 0)
    expired = valid_before > 0 and time.time() > valid_before
    if used is False and expired:
        # Not spent, and now unspendable — the previous attempt can never land,
        # so there is no in-flight settle left to race. This is the interlock
        # that makes retrying safe rather than probably safe.
        return None

    if used is False:
        raise HTTPException(402, (
            f"an x402 settlement for {challenge_resource} is still in flight "
            f"(intent {intent_key[:16]}); retry after the authorization expires"
        ))
    raise HTTPException(402, (
        f"an x402 settlement for {challenge_resource} was interrupted and cannot be "
        f"resolved (intent {intent_key[:16]}): no chain RPC is configured for "
        f"{intent['network'] or 'this network'}, so the relay cannot tell whether the "
        "payer was already charged. Set EMPORIA_X402_RPC_<NETWORK> to let this "
        "resolve itself."
    ))


async def _verify_x402_payment(
    x402_payload: dict[str, Any],
    *,
    creator_id: str | None,
    amount_cents: int,
    currency: str,
    challenge_resource: str,
    payer_id: str,
    resource_type: str,
    resource_id: str,
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

    P48 closed the double-charge gap this docstring recorded from P1b to
    P47, and the record of it is kept because the reasoning that left it open
    was wrong in an instructive way. The gap was real: unlike the Stripe
    branches there is no pre-created PaymentIntent to re-verify against, so a
    crash between a successful x402_settle_payment() and record_payment()
    left a retry that re-signs a *fresh* authorization indistinguishable from
    a first attempt — record_payment() dedupes on the transaction hash and
    the chain's nonce guard rejects only a replay of the same authorization,
    so neither sees a second genuinely valid settlement for one purchase.
    What was wrong was the conclusion: "x402 has no Idempotency-Key
    equivalent in its /settle contract to close this with" is true of the
    protocol and irrelevant to the relay, which can write its own intent
    record before the money moves and ask the chain what happened afterwards.
    See _open_x402_intent() for the state machine.
    """
    from emporia.payments import (
        RAIL_X402, PaymentRef, x402_verify_payment, x402_settle_payment,
        x402_requirement, _x402_asset_for, _x402_extra_for, x402_network_family,
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
    family = x402_network_family(network) if network else None
    pay_to = (
        AGENT_REGISTRY.get_x402_pay_to(creator_id, family)
        if creator_id and family else None
    )
    asset = _x402_asset_for(network) if network else None
    extra = _x402_extra_for(network) if network else None
    # P1br-2: same x402_requirement() build_x402_challenge() uses to build
    # what this relay OFFERS — a single definition, so the two can never
    # compute two different `scheme`/`maxTimeoutSeconds` for the same
    # request. Tolerant of None network/asset/payTo/extra (a missing or
    # unresolvable field) so every mismatch is listed at once below, not
    # just the first one that would otherwise raise. P45: `extra` is read
    # from config here rather than taken from the client's `accepted`, for
    # the same reason as every other field — the requirement is this
    # relay's assertion about the resource, not an echo of the claim.
    expected = x402_requirement(network, amount_cents, asset, pay_to, extra)
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(402, f"x402 verification failed: {e}")

    # P48: everything from here to the intent's close is the money path. The
    # /verify above is free and non-mutating, so the intent is claimed after
    # it — a credential the facilitator already rejected should not be able to
    # occupy a purchase's intent slot and lock out the payer's real retry.
    authorization = (x402_payload.get("payload") or {}).get("authorization") or {}
    authorizer = str(authorization.get("from") or "")
    nonce = str(authorization.get("nonce") or "")
    try:
        valid_before = int(authorization.get("validBefore") or 0)
    except (TypeError, ValueError):
        valid_before = 0

    intent_key = _x402_intent_key(
        payer_id=payer_id, resource_type=resource_type,
        resource_id=resource_id, amount_cents=amount_cents,
    )
    existing = _open_x402_intent(
        intent_key=intent_key, payer_id=payer_id, resource_type=resource_type,
        resource_id=resource_id, amount_cents=amount_cents, network=network or "",
        asset=asset or "", authorizer=authorizer, nonce=nonce, valid_before=valid_before,
    )
    if existing is not None:
        settled = await _resolve_open_x402_intent(existing, challenge_resource)
        if settled is not None:
            return PaymentRef(RAIL_X402, settled)
        _reopen_x402_intent(
            intent_key=intent_key, network=network or "", asset=asset or "",
            authorizer=authorizer, nonce=nonce, valid_before=valid_before,
        )

    try:
        settle_result = await x402_settle_payment(x402_payload, expected)
        if not settle_result.get("success"):
            _close_x402_intent(intent_key, X402_INTENT_FAILED)
            raise HTTPException(
                402, f"x402 settlement failed: {settle_result.get('errorReason', 'unknown')}"
            )
    except HTTPException:
        raise
    except Exception as e:
        # The settle request itself failed — a timeout or a dropped connection
        # says nothing about whether the transaction landed, so the intent
        # stays `settling` and is resolved against the chain on the retry.
        # Marking it failed here is the one shortcut that would re-open the
        # double charge.
        raise HTTPException(402, f"x402 settlement failed: {e}")

    transaction = settle_result.get("transaction")
    if not transaction:
        raise HTTPException(402, "x402 settlement returned no transaction id")
    _close_x402_intent(intent_key, X402_INTENT_SETTLED, transaction)
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
                payer_id=agent_id, resource_type=resource_type, resource_id=resource_id,
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


# ─── P47b: the agent-reference map ──────────────────────────────────────────
# `agent_id` is the primary key of `authorized_agents` and is referenced by
# name — not by foreign key — from 30 columns across 14 other tables, plus
# three JSON arrays. Renaming an agent therefore means rewriting all of them in
# one transaction; a rename that touched only `authorized_agents` would orphan
# the agent's rooms, entitlements, payments and DMs while appearing to succeed.
#
# The plan for this pass put the figure at "22 agent-shaped columns" and
# proposed leaving them un-re-keyed. Both halves were wrong. The count came
# from a name-pattern heuristic that missed `removed_by`, `invited_by`,
# `agent_a`/`agent_b`, `organizer_id`, `from_agent`/`to_agent` and
# `current_agent`; and leaving references behind is not an option once rename
# exists at all.
#
# `_NON_AGENT_COLUMNS` is not documentation. Together with the two maps below
# it must partition the entire schema (231 columns), which a test asserts — so
# a column added later belongs to neither set and fails that test, at the one
# moment someone is thinking about that column and can answer correctly. A map
# maintained any other way is a snapshot that rots silently, and the symptom is
# orphaned rows nobody notices for months.
_RENAME_COLUMNS: tuple[tuple[str, str], ...] = (
    ("access_entitlements", "agent_id"), ("access_removals", "agent_id"),
    ("access_removals", "removed_by"), ("actions", "agent_id"),
    ("agent_inbox", "agent_id"), ("agora_comments", "author_id"),
    ("agora_invites", "agent_id"), ("agora_invites", "invited_by"),
    ("agora_members", "agent_id"), ("agora_posts", "author_id"),
    ("agora_topics", "creator_id"), ("agora_votes", "voter_id"),
    ("challenges", "creator_agent_id"), ("dm_messages", "sender_id"),
    ("dm_threads", "agent_a"), ("dm_threads", "agent_b"),
    ("events", "organizer_id"), ("listings", "agent_id"),
    ("messages", "from_agent"), ("messages", "to_agent"),
    ("participants", "agent_id"), ("payments", "agent_id"),
    ("room_invites", "invitee_id"), ("room_invites", "invited_by"),
    ("room_messages", "sender_id"), ("rooms", "creator_id"),
    ("sessions", "current_agent"), ("settlements", "winner_id"),
    ("settlements", "payer_id"),
    # The row being renamed. Last, so the UPDATE below rewrites references
    # while the old row still exists — SQLite has no deferred constraints here,
    # and doing it first would leave every other statement pointing at a handle
    # that no longer resolves if the transaction were inspected mid-flight.
    # P48: the settle intent's payer. The intent *key* hashes the payer's
    # stable_id, so a rename can't invalidate an in-flight purchase — this
    # column is the human-readable handle beside it and is rewritten like
    # every other reference.
    ("x402_settle_intents", "payer_id"),
    ("authorized_agents", "agent_id"),
)

# Agent ids inside JSON arrays. Not rewritable in SQL, and deliberately not
# done with a string replace: `"alice"` is a substring of `"alice_bot"`, so a
# textual swap would corrupt a neighbour's membership. Parsed, mapped, re-dumped.
_RENAME_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("events", "participants_json"),
    ("rooms", "members_json"),
    ("sessions", "participants_json"),
)

# Every remaining column, declared not to hold an agent handle. Notable
# judgements: `authorized_agents.nous_user_id` is an identity-provider subject
# id, not a handle; `agora_votes.target_id` is a post or comment id;
# `access_*.resource_id` is a room or topic id.
_NON_AGENT_COLUMNS: frozenset[tuple[str, str]] = frozenset({
    # x402_settle_intents (P48) — payer_id is an agent reference and lives in
    # _RENAME_COLUMNS; payer_stable_id deliberately is NOT one (it is P47's
    # identity, which a rename must leave alone — rewriting it here would
    # defeat the entire point of having it).
    ("x402_settle_intents", "intent_key"), ("x402_settle_intents", "payer_stable_id"),
    ("x402_settle_intents", "resource_type"), ("x402_settle_intents", "resource_id"),
    ("x402_settle_intents", "amount_cents"), ("x402_settle_intents", "network"),
    ("x402_settle_intents", "asset"), ("x402_settle_intents", "authorizer"),
    ("x402_settle_intents", "nonce"), ("x402_settle_intents", "valid_before"),
    ("x402_settle_intents", "status"), ("x402_settle_intents", "transaction_hash"),
    ("x402_settle_intents", "created_at"), ("x402_settle_intents", "updated_at"),
    # access_entitlements
    ("access_entitlements", "entitlement_id"), ("access_entitlements", "resource_type"),
    ("access_entitlements", "resource_id"), ("access_entitlements", "payment_intent_id"),
    ("access_entitlements", "granted_at"), ("access_entitlements", "expires_at"),
    ("access_entitlements", "revoked_at"), ("access_entitlements", "revoked_reason"),
    ("access_entitlements", "rail"),
    # access_removals
    ("access_removals", "removal_id"), ("access_removals", "resource_type"),
    ("access_removals", "resource_id"), ("access_removals", "removed_at"),
    ("access_removals", "reason"),
    # actions
    ("actions", "action_id"), ("actions", "session_id"), ("actions", "action_type"),
    ("actions", "payload_json"), ("actions", "result_json"), ("actions", "created_at"),
    # agent_inbox
    ("agent_inbox", "inbox_id"), ("agent_inbox", "event_type"), ("agent_inbox", "payload_json"),
    ("agent_inbox", "is_read"), ("agent_inbox", "created_at"),
    # agora_comments
    ("agora_comments", "comment_id"), ("agora_comments", "post_id"),
    ("agora_comments", "parent_comment_id"), ("agora_comments", "content"),
    ("agora_comments", "vote_score"), ("agora_comments", "is_deleted"),
    ("agora_comments", "created_at"),
    # agora_invites
    ("agora_invites", "topic_id"), ("agora_invites", "created_at"),
    # agora_members
    ("agora_members", "topic_id"), ("agora_members", "role"), ("agora_members", "joined_at"),
    # agora_posts
    ("agora_posts", "post_id"), ("agora_posts", "topic_id"), ("agora_posts", "title"),
    ("agora_posts", "content"), ("agora_posts", "post_type"), ("agora_posts", "flair"),
    ("agora_posts", "vote_score"), ("agora_posts", "comment_count"),
    ("agora_posts", "is_pinned"), ("agora_posts", "is_locked"), ("agora_posts", "is_deleted"),
    ("agora_posts", "created_at"),
    # agora_topics
    ("agora_topics", "topic_id"), ("agora_topics", "slug"), ("agora_topics", "name"),
    ("agora_topics", "description"), ("agora_topics", "visibility"),
    ("agora_topics", "gate_type"), ("agora_topics", "entry_fee_cents"),
    ("agora_topics", "created_at"), ("agora_topics", "post_count"),
    ("agora_topics", "subscriber_count"), ("agora_topics", "flair_options"),
    ("agora_topics", "access_duration_days"),
    # agora_votes
    ("agora_votes", "target_id"), ("agora_votes", "target_type"), ("agora_votes", "value"),
    ("agora_votes", "created_at"),
    # authorized_agents
    ("authorized_agents", "public_key_hex"), ("authorized_agents", "display_name"),
    ("authorized_agents", "registered_at"), ("authorized_agents", "is_active"),
    ("authorized_agents", "stripe_account_id"), ("authorized_agents", "nous_user_id"),
    ("authorized_agents", "trust_level"), ("authorized_agents", "identity_providers_json"),
    ("authorized_agents", "x402_pay_to_json"), ("authorized_agents", "encryption_public_key"),
    ("authorized_agents", "encryption_key_signature"),
    ("authorized_agents", "stripe_account_status"),
    ("authorized_agents", "stripe_account_mode"), ("authorized_agents", "stable_id"),
    # challenges
    ("challenges", "challenge_id"), ("challenges", "game_type"),
    ("challenges", "creator_gateway_url"), ("challenges", "payment_mode"),
    ("challenges", "stake_amount"), ("challenges", "currency"), ("challenges", "status"),
    ("challenges", "created_at"), ("challenges", "expires_at"), ("challenges", "origin_relay"),
    ("challenges", "metadata_json"),
    # dm_messages
    ("dm_messages", "message_id"), ("dm_messages", "thread_id"), ("dm_messages", "content"),
    ("dm_messages", "msg_type"), ("dm_messages", "created_at"), ("dm_messages", "signature"),
    ("dm_messages", "nonce"), ("dm_messages", "chain_hash"),
    # dm_threads
    ("dm_threads", "thread_id"), ("dm_threads", "created_at"),
    ("dm_threads", "last_message_at"), ("dm_threads", "encrypted"),
    # events
    ("events", "event_id"), ("events", "title"), ("events", "description"),
    ("events", "module_type"), ("events", "payment_mode"), ("events", "entry_fee_usd"),
    ("events", "status"), ("events", "bracket_json"), ("events", "created_at"),
    ("events", "updated_at"),
    # listings
    ("listings", "listing_id"), ("listings", "title"), ("listings", "description"),
    ("listings", "listing_type"), ("listings", "payment_mode"), ("listings", "price_usd"),
    ("listings", "module_type"), ("listings", "origin_relay"), ("listings", "expires_at"),
    ("listings", "metadata_json"), ("listings", "created_at"),
    # messages
    ("messages", "message_id"), ("messages", "session_id"), ("messages", "type"),
    ("messages", "payload_json"), ("messages", "signature"), ("messages", "created_at"),
    # participants
    ("participants", "session_id"), ("participants", "joined_at"),
    ("participants", "stake_paid"),
    # payments
    ("payments", "payment_id"), ("payments", "session_id"), ("payments", "room_id"),
    ("payments", "amount_cents"), ("payments", "currency"), ("payments", "payment_intent_id"),
    ("payments", "payment_type"), ("payments", "status"), ("payments", "created_at"),
    ("payments", "rail"),
    # reg_challenges
    ("reg_challenges", "challenge_id"), ("reg_challenges", "nonce"),
    ("reg_challenges", "created_at"), ("reg_challenges", "expires_at"),
    ("reg_challenges", "used"),
    # room_invites
    ("room_invites", "room_id"), ("room_invites", "created_at"),
    # room_messages
    ("room_messages", "message_id"), ("room_messages", "room_id"),
    ("room_messages", "msg_type"), ("room_messages", "content"),
    ("room_messages", "parent_message_id"), ("room_messages", "chain_hash"),
    ("room_messages", "signature"), ("room_messages", "nonce"),
    ("room_messages", "metadata_json"), ("room_messages", "created_at"),
    # rooms
    ("rooms", "room_id"), ("rooms", "name"), ("rooms", "description"), ("rooms", "room_type"),
    ("rooms", "gate_type"), ("rooms", "entry_fee_cents"), ("rooms", "currency"),
    ("rooms", "max_members"), ("rooms", "encrypted"), ("rooms", "linked_session_id"),
    ("rooms", "created_at"), ("rooms", "updated_at"), ("rooms", "access_duration_days"),
    # schema_migrations
    ("schema_migrations", "key"), ("schema_migrations", "applied_at"),
    # sessions
    ("sessions", "session_id"), ("sessions", "module_type"), ("sessions", "config_json"),
    ("sessions", "payment_rules_json"), ("sessions", "state_json"), ("sessions", "step_number"),
    ("sessions", "status"), ("sessions", "created_at"), ("sessions", "updated_at"),
    # settlements
    ("settlements", "settlement_id"), ("settlements", "session_id"),
    ("settlements", "total_stake_cents"), ("settlements", "platform_fee_cents"),
    ("settlements", "winner_payout_cents"), ("settlements", "platform_fee_bps"),
    ("settlements", "payment_intent_ids"), ("settlements", "transfer_id"),
    ("settlements", "transfer_status"), ("settlements", "status"),
    ("settlements", "created_at"), ("settlements", "rail"), ("settlements", "dedupe_key"),
    ("settlements", "refund_id"), ("settlements", "reversal_id"),
})

# Handles the namespace keeps back. A first-come namespace has no other way to
# protect names that are load-bearing elsewhere — `_relay_identity` is already
# guarded by `_AGENT_ID_RE`'s leading-alnum rule, but nothing stopped an agent
# from registering `relay` or `admin` and trading on it. Enforced at both doors
# (registration and rename); a denylist on one of them is decoration.
RESERVED_AGENT_IDS: frozenset[str] = frozenset({
    "relay", "emporia", "admin", "administrator", "system", "root", "operator",
    "support", "security", "official", "api", "www", "null", "none", "undefined",
    "me", "self", "everyone", "anonymous",
})


def _rename_in_json_column(
    conn: Any, table: str, column: str, old_id: str, new_id: str,
) -> int:
    """Rewrite one agent handle inside a JSON array column.

    Parsed and re-dumped rather than string-replaced: `"alice"` is a substring
    of `"alice_bot"`, so a textual swap on `["alice_bot", "bob"]` would corrupt
    a neighbour's membership while looking like it worked. Rows whose JSON is
    unparseable are left exactly as they are — that is pre-existing corruption,
    and a rename is not the place to discover or "fix" it.

    Only rows that actually contain the id are written back, so a rename does
    not rewrite every room on the relay to change one membership.
    """
    changed = 0
    rows = conn.execute(
        f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE ?", (f'%"{old_id}"%',)
    ).fetchall()
    for row in rows:
        raw = row[1] if isinstance(row, tuple) else row[column]
        rowid = row[0] if isinstance(row, tuple) else row["rowid"]
        try:
            members = json.loads(raw or "[]")
        except ValueError:
            continue
        if not isinstance(members, list) or old_id not in members:
            continue
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
            (json.dumps([new_id if m == old_id else m for m in members]), rowid),
        )
        changed += 1
    return changed


def stable_id_for(public_key_hex: str) -> str:
    """This agent's permanent identity, derived from its **genesis** Ed25519
    public key (P47).

    `agent_id` was three things at once — primary key, display handle, and
    identity — which works right up until any one of them has to change. This
    separates the third out.

    Why the pubkey and not the alternatives ROADMAP.md proposed:

    * `identity.content_address_for(name, nous_user_id)` puts the **name inside
      the digest** (`sha3_256(f"emporia:v1:{normalized}:{material}")`), so it
      changes on rename. It is a content address for a named thing, which is a
      different job; its only caller is the games lobby.
    * `nous_user_id` is one-to-many by design — the index on it is deliberately
      non-unique ("one Nous user may operate multiple agent personas"), so it
      cannot identify an agent. And `register()` UPSERTs `trust_level`, so a
      key_only agent presenting a Nous claim on an ordinary re-register would
      *acquire* a Nous-derived id: identity changing at the exact moment it
      became better attested.

    The pubkey was already the ownership proof — registration requires one and
    refuses a taken handle presented under a different key. This writes it down.

    Derived so it is independently verifiable from public material; **stored**
    thereafter (see `AgentRegistry.register`) so a key rotation preserves
    identity rather than renaming the agent for doing the right thing. Hex is
    lowercased before hashing: the same key written two ways is one key.
    """
    return hashlib.sha3_256(
        b"emporia:agent:v1:" + bytes.fromhex(public_key_hex.strip().lower())
    ).hexdigest()


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
        # P47b: the denylist has to live at both doors. An agent refused `admin`
        # on rename could otherwise simply register it.
        if agent_id in RESERVED_AGENT_IDS:
            raise ValueError(f"'{agent_id}' is a reserved handle")
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
                # P47: the mirror of the check above. That one refuses a taken
                # handle under a new key; this refuses a new handle under a key
                # that already holds one. Both together are what make the handle
                # ownable: without this, one key could hold two identities that
                # nothing downstream could tell apart. Checked here rather than
                # left to the unique index so the message names the actual
                # conflict instead of surfacing an IntegrityError.
                derived_stable_id = stable_id_for(public_key_hex)
                if not existing_row:
                    twin = conn.execute(
                        "SELECT agent_id FROM authorized_agents WHERE stable_id = ?",
                        (derived_stable_id,),
                    ).fetchone()
                    if twin:
                        raise ValueError(
                            f"That key is already registered as '{twin['agent_id']}'. "
                            f"One key is one identity — rename it rather than "
                            f"registering it twice."
                        )
                # P16-3: an UPSERT, not INSERT OR REPLACE. SQLite implements
                # INSERT OR REPLACE as DELETE-then-INSERT, so every column *not*
                # listed here silently reverted to its schema DEFAULT on a
                # re-register. That was a live bug for `x402_pay_to_json`: a
                # creator who re-registered lost their wallet, and
                # `_x402_pay_to_by_network()` then found none, so the 402 challenge
                # stopped offering the x402 rail entirely — silent, with no error
                # anywhere. P16 would have reproduced it exactly for
                # `encryption_public_key`, where the blast radius is worse (peers
                # find no key to encrypt to, so DMs quietly stop being sendable).
                # DO UPDATE names precisely the columns registration owns; anything
                # an agent set through another endpoint survives by construction
                # rather than by remembering to add it to a list.
                conn.execute(
                    "INSERT INTO authorized_agents "
                    "(agent_id, public_key_hex, display_name, registered_at, is_active, "
                    "stripe_account_id, nous_user_id, trust_level, identity_providers_json, "
                    "stable_id) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?) "
                    # P47: `stable_id` is deliberately ABSENT from DO UPDATE.
                    # Allocated once, on the row's first insert, and never
                    # recomputed — that is the whole mechanism by which a key
                    # rotation preserves identity. Putting it in this list would
                    # silently make the id follow the key again, which is the
                    # design this pass rejected.
                    "ON CONFLICT(agent_id) DO UPDATE SET "
                    "public_key_hex=excluded.public_key_hex, "
                    "display_name=excluded.display_name, "
                    "registered_at=excluded.registered_at, "
                    "is_active=1, "
                    # P26-5: was `stripe_account_id=excluded.stripe_account_id`,
                    # which writes NULL on any re-register that isn't
                    # auto-provisioning (every live-mode one). stripe_account_status
                    # is correctly not in this list, so it *survived* — leaving
                    # 'active' with no account id. The next start_stripe_onboarding
                    # then created a SECOND real Connected Account: a second real
                    # KYC, orphaning the first and any balance on it. Registration
                    # is documented as "safe to call on every start", so this was
                    # reachable on an ordinary restart. COALESCE keeps an existing
                    # id whenever this registration didn't mint one.
                    "stripe_account_id=COALESCE(excluded.stripe_account_id, authorized_agents.stripe_account_id), "
                    "nous_user_id=excluded.nous_user_id, "
                    "trust_level=excluded.trust_level, "
                    "identity_providers_json=excluded.identity_providers_json",
                    (agent_id, public_key_hex, verified_display_name,
                     datetime.now(UTC).isoformat(), stripe_account_id,
                     nous_user_id, trust_level, json.dumps(providers_verified),
                     derived_stable_id),
                )
                stable_id = conn.execute(
                    "SELECT stable_id FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()["stable_id"]
            return {
                "agent_id": agent_id,
                # P47: read back rather than returned from `derived_stable_id`.
                # On a re-register those two differ whenever the row was created
                # before this key — which is exactly the case (rotation, an
                # upgraded legacy row) where returning the freshly derived value
                # would report an identity the database does not hold.
                "stable_id": stable_id,
                "display_name": verified_display_name,
                "trust_level": trust_level,
                "nous_user_id": nous_user_id,
                "providers_verified": providers_verified,
            }
        except ValueError:
            raise  # key-conflict or validation errors — preserve message for HTTP status mapping
        except Exception as e:
            raise ValueError(f"Registration failed: {e}") from e

    def rotate_key(self, agent_id: str, new_public_key_hex: str) -> dict:
        """Replace an agent's Ed25519 signing key, keeping its identity (P47).

        Not reachable through `register()` by design: that path refuses a taken
        handle presented under a different key, and it has to — that refusal is
        what makes a handle ownable at all. Rotation is therefore its own
        operation, and the caller is responsible for having proved control of
        the *current* key first (the route does; see `rotate_agent_key`).

        `stable_id` is untouched. That is the point: an agent that rotates its
        key for good reasons must not be renamed by the relay for doing so.
        Note what this means downstream — signatures made under the old key stop
        verifying immediately, which is the correct behaviour for a rotation and
        the wrong behaviour for a compromise recovery that wants to keep them.
        """
        try:
            if len(bytes.fromhex(new_public_key_hex)) != 32:
                raise ValueError()
        except ValueError:
            raise ValueError("public_key_hex must be a 64-char hex string (Ed25519 public key)")

        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT stable_id, public_key_hex FROM authorized_agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Agent not found: {agent_id}")
            # The new key must not already belong to someone else — same
            # one-key-one-identity rule registration enforces, checked here so
            # rotation can't be used to walk around it.
            twin = conn.execute(
                "SELECT agent_id FROM authorized_agents WHERE stable_id = ? AND agent_id != ?",
                (stable_id_for(new_public_key_hex), agent_id),
            ).fetchone()
            if twin:
                raise ValueError(f"That key is already registered as '{twin['agent_id']}'")
            conn.execute(
                "UPDATE authorized_agents SET public_key_hex = ? WHERE agent_id = ?",
                (new_public_key_hex, agent_id),
            )
        return {"agent_id": agent_id, "stable_id": row["stable_id"],
                "public_key_hex": new_public_key_hex}

    def rename(self, agent_id: str, new_agent_id: str) -> dict:
        """Move an agent to a new handle, carrying every reference with it (P47b).

        The handle is the only thing that changes. `stable_id` is untouched —
        that is what makes rename safe to offer at all: anything that recorded
        *who* this agent is still resolves, and only things that recorded *what
        it was called* have to move. Which is all of them, hence
        `_RENAME_COLUMNS`.

        One transaction, under DB_LOCK. A partial rename is the worst possible
        outcome — an agent whose rooms answer to the new handle and whose
        payments answer to the old one — so this either lands completely or not
        at all.

        The old handle is **released, not aliased**. Someone else may claim it,
        and gets a fresh row with their own `stable_id` and none of the previous
        holder's entitlements, payout wallet, or history: those all moved with
        the rename, by construction rather than by a cleanup step that could be
        forgotten. That is also the honest reading of the no-backcompat stance —
        a redirect would keep a dead name resolving forever and make "available"
        mean something weaker than it says.

        Two things this deliberately does not move, recorded rather than
        silently accepted:

        * The append-only session audit log. It is a record of what happened
          under a name at the time, and rewriting history to match a later
          rename would defeat the point of having it. `stable_id` is the join
          key for anyone correlating across a rename.
        * In-memory state — open WebSocket connections and rate-limit buckets
          keyed by handle. The agent reconnects; the stale bucket ages out.

        Outstanding bearer tokens for the old handle stop resolving, because the
        row they name is gone. That is correct: the token asserts a handle, and
        the handle now belongs to nobody.
        """
        new_agent_id = _normalize_agent_id(new_agent_id)
        if new_agent_id in RESERVED_AGENT_IDS:
            raise ValueError(f"'{new_agent_id}' is a reserved handle")
        if new_agent_id == agent_id:
            raise ValueError("that is already this agent's handle")

        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Agent not found: {agent_id}")
            taken = conn.execute(
                "SELECT 1 FROM authorized_agents WHERE agent_id = ?", (new_agent_id,)
            ).fetchone()
            if taken:
                raise KeyError(f"'{new_agent_id}' is already taken")

            for table, column in _RENAME_COLUMNS:
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                    (new_agent_id, agent_id),
                )
            for table, column in _RENAME_JSON_COLUMNS:
                _rename_in_json_column(conn, table, column, agent_id, new_agent_id)

        return {
            "agent_id": new_agent_id,
            "previous_agent_id": agent_id,
            "stable_id": row["stable_id"],
        }

    def get_stable_id(self, agent_id: str) -> str | None:
        """P47's identity for a handle, or None if the handle isn't registered.

        The lookup itself predates this accessor in three places inside this
        class; P48 needed a fourth from outside it and single-sourced the
        read rather than inlining a fifth.
        """
        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return row["stable_id"] if row else None

    def backfill_stable_ids(self) -> int:
        """Run `_backfill_stable_ids()` against the live database.

        The migration calls it once, when the column first appears. This is the
        same operation as an explicitly invocable repair — a relay whose backfill
        was interrupted, or whose rows were written by an older process mid-upgrade,
        has NULLs the migration will never look at again (it only fires on the
        column being newly added). Returns the number of rows touched.
        """
        with DB_LOCK, get_db() as conn:
            return _backfill_stable_ids(conn)

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

    def set_stripe_account(self, agent_id: str, stripe_account_id: str, mode: str = "") -> None:
        """`mode` (P26-4) records which Stripe key space created this account —
        "test"/"live", or "" to leave whatever is stored (so a caller that
        genuinely doesn't know can't erase a known value)."""
        with DB_LOCK, get_db() as conn:
            if mode:
                conn.execute(
                    "UPDATE authorized_agents SET stripe_account_id = ?, "
                    "stripe_account_mode = ? WHERE agent_id = ?",
                    (stripe_account_id, mode, agent_id),
                )
            else:
                conn.execute(
                    "UPDATE authorized_agents SET stripe_account_id = ? WHERE agent_id = ?",
                    (stripe_account_id, agent_id),
                )

    def get_stripe_account_mode(self, agent_id: str) -> str:
        """P26-4: "test" | "live" | "" (unknown). Never raises; unknown on any
        error, which `payments.stripe_account_mode_mismatch()` treats as
        "don't block" rather than "refuse" — see that function for why."""
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT stripe_account_mode FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            return (row["stripe_account_mode"] if row else None) or ""
        except Exception:
            return ""

    def get_stripe_account_status(self, agent_id: str) -> str:
        """P25-2: 'none' | 'pending' | 'active' — see the migration comment
        above. Never raises; an unknown agent or a DB error reads as 'none'
        (can't be paid), the same fail-closed default a brand-new row has."""
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT stripe_account_status FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            return (row["stripe_account_status"] if row else None) or "none"
        except Exception:
            return "none"

    def set_stripe_account_status(
        self, stripe_account_id: str, status: str, mode: str = "",
    ) -> bool:
        """Looked up by stripe_account_id, not agent_id — that shape is set by
        POST /webhooks/stripe, which learns the Stripe account id from the
        event payload and has no agent_id in hand. Returns False when no row
        matches, so the caller can log an unrecognized/stale webhook instead
        of assuming it took effect.

        P27-4: this said "the only caller is POST /webhooks/stripe". It has not
        been since P25 — register_agent() and start_stripe_onboarding() both
        call it too, and the webhook itself calls it from two branches. The
        by-account-id lookup is still the webhook's constraint, but a reader
        checking "who can move this column" was being told the wrong answer."""
        with DB_LOCK, get_db() as conn:
            if mode:
                # P27-5: same "" means leave-it-alone contract as
                # set_stripe_account()'s own `mode`, so a caller that hasn't
                # established the key space can't erase one that has.
                cur = conn.execute(
                    "UPDATE authorized_agents SET stripe_account_status = ?, "
                    "stripe_account_mode = ? WHERE stripe_account_id = ?",
                    (status, mode, stripe_account_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE authorized_agents SET stripe_account_status = ? "
                    "WHERE stripe_account_id = ?",
                    (status, stripe_account_id),
                )
            return cur.rowcount > 0

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
        failure means "offer nothing," fails closed.

        P4-2: also fails closed on a stored blob that is valid JSON but not
        an object (`set_x402_pay_to()` never writes one, but DB corruption
        or a manual edit could) — `get_x402_pay_to()` used to guard its own
        `.get(network_family)` call inside this same try/except, and lost
        that guarantee when it was rewritten in terms of this function
        (P1brr-3), since only the read and `json.loads` stayed inside the
        try here. Restored at the source so both callers keep it."""
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT x402_pay_to_json FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            if not row:
                return {}
            data = json.loads(row["x402_pay_to_json"] or "{}")
            return data if isinstance(data, dict) else {}
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

    def get_encryption_key(self, agent_id: str) -> tuple[str, str]:
        """P16: this agent's published X25519 public key and the Ed25519 signature
        binding it to their id, or `("", "")` if they never published one.

        Not published is a normal state, not an error — an agent may simply not do
        encrypted DMs — so callers branch on the empty string rather than handling
        an exception.
        """
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT encryption_public_key, encryption_key_signature "
                    "FROM authorized_agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
            if not row:
                return "", ""
            return row["encryption_public_key"] or "", row["encryption_key_signature"] or ""
        except Exception:
            return "", ""

    def set_encryption_key(self, agent_id: str, public_key_hex: str, signature: str) -> None:
        with DB_LOCK, get_db() as conn:
            conn.execute(
                "UPDATE authorized_agents SET encryption_public_key = ?, "
                "encryption_key_signature = ? WHERE agent_id = ?",
                (public_key_hex, signature, agent_id),
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


def _require_valid_msg_type(msg_type: str) -> None:
    """P15: enforce the msg_type domain both message routes already declare but never
    checked. rooms.py has declared `MsgType` as a closed Literal since rooms shipped,
    and rooms.post_message() is annotated with it — but annotations aren't runtime
    checks and both request models type the field as bare `str`, so any string reached
    storage. Call with the *resolved* value (after `or DEFAULT_MSG_TYPE`), so an omitted
    field still resolves normally rather than tripping this.

    "system" is rejected here specifically: it's the relay's own type for
    negotiation-broker output (posted with sender_id="emporia:broker"), so a client
    supplying it is minting a server-owned label. Not an impersonation primitive — the
    one consumer that distinguishes broker messages keys off sender, not type — but the
    type isn't the client's to set.
    """
    from emporia.wire import CLIENT_MSG_TYPES
    if msg_type not in CLIENT_MSG_TYPES:
        raise HTTPException(
            400,
            f"Invalid msg_type '{msg_type}' — must be one of "
            f"{', '.join(sorted(CLIENT_MSG_TYPES))}",
        )


async def _assert_payload_safe_counted(payload: dict[str, Any]) -> None:
    """Wrap guardrails.assert_payload_safe_async to track block counts for the
    dashboard, distinguishing the always-on regex layer from the optional
    NVIDIA NIM semantic layer via the structured GuardrailResult attached to
    GuardrailBlocked (not by parsing the exception's message text — that broke
    once already when the message wording changed).

    P21-5 (B5): also the one chokepoint every scanning route already passes its
    whole payload through, so the per-field content-size bound
    (_require_payload_content_within_limit, checked before the scan — cheap
    should reject before expensive) now lives here instead of being enumerated
    by hand at up to 17 call sites, three of which had no bound at all."""
    _require_payload_content_within_limit(payload)
    try:
        await assert_payload_safe_async(payload)
    except GuardrailBlocked as e:
        if e.result.matched_pattern == "nemo:semantic":
            SAFETY_STATS["nemo_guardrail_blocks"] += 1
        else:
            SAFETY_STATS["guardrail_blocks"] += 1
        logger.warning(
            "guardrail_blocked", extra={"matched_pattern": e.result.matched_pattern},
        )
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
        logger.warning("por_rejected", extra={"reason": "missing_rationale"})
        raise PermissionError(
            f"REJECTED_INFRACTION: Missing Proof-of-Reasoning "
            f"(need >= {MIN_RATIONALE_CHARS} chars)"
        )
    lowered = rationale.casefold()
    for fp in BOT_FINGERPRINTS:
        if fp.casefold() in lowered:
            SAFETY_STATS["por_rejections"] += 1
            logger.warning("por_rejected", extra={"reason": "bot_fingerprint"})
            raise PermissionError(f"REJECTED_INFRACTION: Bot fingerprint detected: {fp}")
    return rationale


# ============================================================================
# Session Management
# ============================================================================

# P5-1: the single source for sessions.status — real values written are
# "waiting" (create_session), "active" (session join / a turn taken),
# "completed" (a module's is_over), "abandoned" (abandon_session). Before
# this pass, /health, the dashboard WS, and abandon_session's own guard each
# independently hardcoded a DIFFERENT, WRONG pair of literals
# ("complete"/"cancelled" — neither is ever written) as their "terminal"
# check, so all three silently no-op'd: /health's session_count counted
# completed/abandoned sessions as active, and abandon_session's "already
# complete" guard never fired, letting a completed session's captured
# payments be cancelled a second time.
_SESSION_STATUSES = {"waiting", "active", "completed", "abandoned"}
_SESSION_TERMINAL_STATUSES = {"completed", "abandoned"}


def _session_status_not_in_terminal_sql(column: str = "status") -> str:
    """`WHERE {column} NOT IN (?, ?)` built from `_SESSION_TERMINAL_STATUSES`
    rather than hand-copied literals a third time — two independently
    hand-copied literal lists (both wrong) is how this bug happened."""
    placeholders = ", ".join("?" for _ in _SESSION_TERMINAL_STATUSES)
    return f"{column} NOT IN ({placeholders})"


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
        logger.warning("%s", notice)
    _require_stripe_webhook_secret_if_needed()
    global _SWEEP_TASK, _FEDERATION_SYNC_TASK, _PAYOUT_RECONCILE_TASK
    _SWEEP_TASK = asyncio.create_task(_sweep_loop())
    # P51: money the relay owes a creator but never moved. Runs unconditionally
    # — unlike the federation loop, which is gated on peers being configured,
    # there is no deployment where "a payout could be owed" is knowably false.
    _PAYOUT_RECONCILE_TASK = asyncio.create_task(_payout_reconcile_loop())
    # P23-3: only started when peers are actually configured -- a standalone
    # relay (the common case) shouldn't spin a task that does nothing every
    # EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS forever.
    if FEDERATED_RELAYS:
        _FEDERATION_SYNC_TASK = asyncio.create_task(_federation_sync_loop())
    try:
        yield
    finally:
        _SWEEP_TASK.cancel()
        with suppress(asyncio.CancelledError):
            await _SWEEP_TASK
        _SWEEP_TASK = None
        if _PAYOUT_RECONCILE_TASK is not None:
            _PAYOUT_RECONCILE_TASK.cancel()
            with suppress(asyncio.CancelledError):
                await _PAYOUT_RECONCILE_TASK
            _PAYOUT_RECONCILE_TASK = None
        if _FEDERATION_SYNC_TASK is not None:
            _FEDERATION_SYNC_TASK.cancel()
            with suppress(asyncio.CancelledError):
                await _FEDERATION_SYNC_TASK
            _FEDERATION_SYNC_TASK = None


app = FastAPI(title="Emporia Relay", version=__version__, lifespan=lifespan)

# ─── Middleware registration order (P19-7, M6) ────────────────────────────────
# Starlette's add_middleware() inserts each new middleware at the *front* of its
# list, and the stack is built by wrapping in reverse — so the middleware
# registered LAST ends up OUTERMOST (runs first on the way in, last on the way
# out), and the middleware registered FIRST ends up innermost, right next to
# the router. Registration order below is therefore deliberately: rate_limit
# first (innermost), limit_request_size second, CORS last (outermost) — so a
# request hits CORS, then the cheap Content-Length check, then the rate
# limiter, then the router. Two things this fixes, verified live pre-fix:
#   - a 413/429 short-circuit returned by an inner middleware never reached
#     CORSMiddleware's wrapped `send`, so it carried no CORS headers — a
#     browser's fetch() saw an opaque network-level CORS failure, not the
#     413/429 body and status the relay actually sent.
#   - the size check ran *after* rate limiting, so an oversized request (cheap
#     to reject on the Content-Length header alone) still consumed a slot in
#     the sender's rate-limit budget before being rejected.

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


def _check_rate_limit(request: Request) -> JSONResponse | None:
    """The shared core of `rate_limit()` below — a `JSONResponse` if the caller is
    over budget, `None` if the request should proceed. Pulled out so
    `preflight_guard` (P20-4) can apply the same accounting to a CORS preflight,
    which `CORSMiddleware` answers itself without ever reaching this middleware."""
    # Always use the real TCP peer address — never trust X-Forwarded-For for bypass decisions.
    # If behind a trusted reverse proxy (nginx, Caddy), set TRUSTED_PROXY=1 and the proxy
    # must rewrite the source IP at TCP level or strip/replace the X-Real-IP header server-side.
    ip = (request.client.host if request.client else "unknown")
    # Localhost, operator-listed proxy IPs, and test client: no rate limiting. Separate from
    # _HEADER_AUTH_TRUSTED_HOSTS — exempting an IP from throttling is a much lower-stakes
    # bypass than trusting it to claim any agent identity.
    if ip in _RATE_LIMIT_EXEMPT_HOSTS or ip == "testclient":
        return None
    path = request.url.path
    bucket = "auth" if path in _AUTH_PATHS else ("write" if request.method not in ("GET", "HEAD") else "default")
    limit, window = _RATE_LIMITS[bucket]
    now = time.time()
    key = (ip, bucket)
    q = _RATE_WINDOWS[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        logger.warning(
            "rate_limit_exceeded", extra={"client_ip": ip, "bucket": bucket, "path": path},
        )
        return JSONResponse(
            {"detail": f"Rate limit exceeded — {limit} requests per {int(window)}s per IP"},
            status_code=429,
            headers={"Retry-After": str(int(window - (now - q[0])))},
        )
    q.append(now)
    return None


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    denied = _check_rate_limit(request)
    if denied is not None:
        return denied
    return await call_next(request)


# P18-1: the general request-body ceiling. Deliberately larger than
# wire.MAX_CONTENT_BYTES — a legitimate request is one content field plus signature,
# metadata and framing, and some routes (registration with identity claims, x402
# payment payloads) carry more structure than a message does.
# P19-7 (L2): env_int() over a raw int(os.getenv(...)) — the raw form crashes the
# whole process at import time on a malformed value instead of falling back to
# the default, and every other EMPORIA_* env var in this codebase already goes
# through this module (see mcp_server.py).
MAX_REQUEST_BYTES = env_int("MAX_REQUEST_BYTES", 1024 * 1024)


def _check_request_size(request: Request) -> JSONResponse | None:
    """The shared core of `limit_request_size()` below — see `_check_rate_limit()`
    just above for why this is split out (P20-4: `preflight_guard` needs it too).

    Content-Length only. A chunked request without that header isn't rejected here
    — Starlette streams the body and a size check would mean buffering it, which is
    the very thing being avoided. That residual case is what a real reverse proxy
    is still for, and it is written down rather than implied: this closes the
    ordinary path, not every path.
    """
    raw_len = request.headers.get("content-length")
    if raw_len:
        try:
            if int(raw_len) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    {"detail": f"Request body too large — limit is {MAX_REQUEST_BYTES} bytes"},
                    status_code=413,
                )
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length header"}, status_code=400)
    return None


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Reject an oversized body with 413 before any handler, guardrails scan, or DB
    write sees it.

    This is the "it belongs at the proxy layer" fix, implemented in the app —
    because the reasoning was right about the general case and wrong as a reason to
    do nothing: this relay runs with nothing in front of it, so until now a
    multi-MB POST to any route was simply accepted.
    """
    denied = _check_request_size(request)
    if denied is not None:
        return denied
    return await call_next(request)


def _require_content_within_limit(content: str, field: str = "content") -> None:
    """P18-1: bound one text field, in bytes. Separate from the request-size
    middleware because a body can sit under the general ceiling while its content
    field is still far past what a message should be — and because a 413 that names
    nothing is a poor answer to "why was my message rejected"."""
    from emporia.wire import MAX_CONTENT_BYTES

    size = len(content.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise HTTPException(
            400,
            f"{field} is too large — {size} bytes, limit is {MAX_CONTENT_BYTES}. "
            f"On an encrypted thread this budget covers the ciphertext, which runs "
            f"larger than the plaintext it carries.",
        )


def _require_payload_content_within_limit(payload: dict[str, Any]) -> None:
    """P21-5 (B5): bound every top-level field of `payload` the same way
    `_require_content_within_limit` bounds one — a str field as itself, anything
    else (a dict/list, e.g. `metadata`/`payload`) as its `json.dumps()`'d size,
    exactly matching what each of the ~17 enumerated call sites this replaces
    did by hand for one named field at a time. Called from
    `_assert_payload_safe_counted` itself (below), so every route that already
    unconditionally scans its whole payload gets this for free — including the
    three that had no enumerated call at all (both federation imports,
    `import_challenge`) and were wholly unbounded until now.

    Deliberately does **not** replace every enumerated call: a route that scans
    a *sub*-value directly rather than a dict containing it under that field's
    own name (`send_message`'s `_assert_payload_safe_counted(req.payload)` — the
    scanned dict IS `req.payload`, not `{"payload": req.payload}`) would have
    this walk `req.payload`'s own inner keys instead of bounding it as the one
    blob the enumerated call did; that call site keeps its explicit check. Same
    reasoning already carved out the two message routes, which skip the scan
    entirely for encrypted content and so have nothing here to piggyback on.
    """
    from emporia.wire import MAX_CONTENT_BYTES

    if not isinstance(payload, dict):
        return
    for field, value in payload.items():
        text = value if isinstance(value, str) else json.dumps(value)
        size = len(text.encode("utf-8"))
        if size > MAX_CONTENT_BYTES:
            raise HTTPException(
                400,
                f"{field} is too large — {size} bytes, limit is {MAX_CONTENT_BYTES}. "
                f"On an encrypted thread this budget covers the ciphertext, which runs "
                f"larger than the plaintext it carries.",
            )


class GuardedCORSMiddleware(CORSMiddleware):
    """CORSMiddleware answers a genuine preflight (OPTIONS + Origin +
    Access-Control-Request-Method) itself and never calls the wrapped app — see
    CORSMiddleware.__call__ — so every request-size/rate-limit middleware
    registered *inside* it (below) never sees a preflight at all.

    P20-4 fixed that by adding a separate `preflight_guard` middleware registered
    *outside* CORS, so it would see the preflight before CORSMiddleware could
    short-circuit it. That worked for catching the preflight, but its own 413/429
    denial then sat outside CORSMiddleware too — exactly the problem the ordering
    note above the rate limiter says CORS-outermost exists to prevent, just for a
    different code path. A rate-limited browser client got an opaque CORS failure
    instead of the 429 + Retry-After the relay actually sent.

    P21-2 (B2): subclassing instead of adding a middleware is the only way to
    apply the checks to a preflight *and* have the denial still carry CORS
    headers, since CORSMiddleware's own `__call__` is the one place that both
    recognizes a preflight and controls what headers a response gets. This
    intercepts the exact condition CORSMiddleware itself uses to decide "this is
    a preflight", runs the same two checks `rate_limit`/`limit_request_size`
    apply to every other request, and on denial takes the CORS headers from
    `self.preflight_response()` — the same call CORSMiddleware would have made —
    rather than reconstructing them by hand, so the CORS header logic stays
    single-sourced in Starlette's own implementation.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        headers = Headers(scope=scope)
        is_preflight = (
            scope["method"] == "OPTIONS"
            and headers.get("origin") is not None
            and "access-control-request-method" in headers
        )
        if is_preflight:
            request = Request(scope)
            denied = _check_request_size(request) or _check_rate_limit(request)
            if denied is not None:
                cors_headers = self.preflight_response(request_headers=headers).headers
                for key, value in cors_headers.items():
                    if key.lower() == "vary" or key.lower().startswith("access-control-"):
                        denied.headers[key] = value
                await denied(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


# CORS allowlist: relay's own URL + local dev dashboard ports by default, extendable
# via EMPORIA_CORS_ORIGINS (comma-separated) for a remote-hosted dashboard.
# allow_credentials=True + allow_origins=["*"] would let any site replay a browser's
# stored dashboard JWT/cookie against this relay — keep this an explicit allowlist.
# Registered last so it wraps outermost — see the ordering note above the rate
# limiter: this is what makes its headers land on a 413/429 short-circuit too.
_CORS_ORIGINS = {RELAY_BASE_URL, "http://localhost:8088", "http://localhost:5173"}
_CORS_ORIGINS.update(
    o.strip() for o in os.getenv("EMPORIA_CORS_ORIGINS", "").split(",") if o.strip()
)
app.add_middleware(
    GuardedCORSMiddleware,
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
    # P22: X-Request-Id is set by request_context (below), registered *after*
    # this middleware so it wraps outside CORS and adds the header once CORS
    # has already finished deciding what to expose — without listing it here
    # too, a cross-origin browser caller (e.g. a remote-hosted dashboard)
    # could see the header in devtools but couldn't read it via fetch().
    expose_headers=["PAYMENT-RESPONSE", "X-Request-Id"],
)


# P22: registered last, so it wraps outside CORS (and every other app-level
# middleware) and still sits inside Starlette's own ServerErrorMiddleware,
# which wraps *everything* app-registered (confirmed against this repo's own
# starlette/middleware/errors.py) -- an unhandled exception anywhere below
# this point propagates through call_next() as a real Python exception,
# catchable here, loggable, and then re-raised so ServerErrorMiddleware still
# produces the exact same 500 it always has. No @app.exception_handler is
# added or needed.
#
# Known, structural limitation (verified live, not just reasoned about): the
# X-Request-Id header set below never reaches a genuine unhandled-exception
# 500. ServerErrorMiddleware is prepended by Starlette *outside* every
# app.add_middleware()-registered layer -- the same structural fact behind
# A8's recorded "500s still bypass CORS" finding -- so once an exception
# propagates past this middleware, there is no way to attach anything to the
# response it builds without a custom @app.exception_handler, which this
# deliberately avoids (see above). The request_id still reaches the log line
# (correlatable by timestamp/path/traceback) -- it just can't reach the
# client-visible response body on this one path.
#
# P23-2: when SENTRY_DSN is configured, this same re-raise also propagates to
# sentry_sdk's own Starlette/FastAPI ASGI integration, which independently
# captures the exception -- so one unhandled exception could in principle
# produce two Sentry events (one via the logger.exception() call below, via
# sentry_sdk's default LoggingIntegration; one via the ASGI integration
# catching the re-raised exception itself). It doesn't, because both captures
# see the *same* exception object, and sentry_sdk's default DedupeIntegration
# drops a second capture of an already-seen exception by identity -- verified
# empirically with a recording transport, not just read about. This relies on
# the except-block re-raising the bare caught exception unchanged; wrapping it
# in a new exception before re-raising would defeat the dedupe and silently
# double-report.
_LOW_SIGNAL_PATHS = {"/health"}


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    token = request_id_ctx.set(rid)
    start = time.monotonic()
    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled_exception",
                extra={"method": request.method, "path": request.url.path},
            )
            raise
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        # P23-2: the dashboard polls /health every 5-10s per open tab (App.tsx,
        # PaymentsFeesSection.tsx) -- at INFO that's a near-constant stream of
        # near-identical lines drowning out everything else. DEBUG keeps it
        # available (EMPORIA_LOG_LEVEL=DEBUG) without polluting the default.
        # P24-5 (W5, F7): normalized before the set lookup -- a bare path match
        # missed the trailing-slash variant (Starlette 307-redirects /health/
        # to /health, and that redirect's own line matched nothing, logging at
        # INFO).
        normalized_path = request.url.path.rstrip("/") or "/"
        level = logging.DEBUG if normalized_path in _LOW_SIGNAL_PATHS else logging.INFO
        logger.log(
            level, "request", extra={
                "method": request.method, "path": request.url.path,
                "status_code": response.status_code, "duration_ms": duration_ms,
                "client_ip": request.client.host if request.client else "unknown",
            },
        )
        response.headers["X-Request-Id"] = rid
        return response
    finally:
        request_id_ctx.reset(token)


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
_PAYOUT_RECONCILE_TASK: asyncio.Task | None = None  # P51; same lifecycle as _SWEEP_TASK

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


# ─── P51: the payouts nothing was ever going to look at again ────────────────
# `_settle_access_payment()` and `payments.settle()` between them record five
# distinct "the creator's money did not move" states, each written correctly and
# on purpose (P25-2 and P26-4 exist precisely to skip a doomed Stripe call
# rather than fail against it). Until this loop, nothing read any of them back.
# The ordinary marketplace sequence — register, sell, *then* finish onboarding —
# therefore stranded the first sale's money on the platform balance forever.
#
# Prefix-matched, not compared for equality: `payments.settle()` writes
# `f"transfer_failed: {e}"` while `_settle_access_payment()` writes a bare
# `"transfer_failed"`, and an `==` check would silently reconcile the room/Agora
# lane while skipping every session-escrow row.
_PAYOUT_OWED_STATUS_PREFIXES = (
    "pending_connect", "no_stripe_account", "transfer_failed",
)

_PAYOUT_RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("EMPORIA_PAYOUT_RECONCILE_INTERVAL_SECONDS", "300")
)

PAYOUT_RECONCILE_STATS: dict[str, Any] = {
    "runs": 0,
    "last_run_at": None,
    "last_error_at": None,
    "consecutive_failures": 0,
    "outcomes": {"owed": 0, "recovered": 0, "paid": 0, "unknown": 0, "still_blocked": 0},
}


def _owed_payout_rows() -> list[dict[str, Any]]:
    """Settlements where a Stripe payout is owed and no transfer id was ever
    recorded. `transfer_id IS NULL` is the load-bearing half: a reversed or
    refunded row keeps its transfer id and must never be re-paid.
    """
    from emporia.payments import RAIL_STRIPE

    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT settlement_id, session_id, winner_id, winner_payout_cents, "
            "transfer_status, dedupe_key FROM settlements "
            "WHERE transfer_id IS NULL AND winner_payout_cents > 0 AND rail = ? "
            "ORDER BY created_at",
            (RAIL_STRIPE,),
        ).fetchall()
    return [
        dict(r) for r in rows
        if (r["transfer_status"] or "").startswith(_PAYOUT_OWED_STATUS_PREFIXES)
        and r["dedupe_key"]
    ]


def _record_payout_outcome(settlement_id: str, transfer_status: str,
                           transfer_id: str | None) -> None:
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "UPDATE settlements SET transfer_status = ?, transfer_id = ? "
            "WHERE settlement_id = ?",
            (transfer_status, transfer_id, settlement_id),
        )


async def _reconcile_owed_payouts() -> dict[str, int]:
    """Re-drive the payouts that never happened, asking Stripe first.

    "Ask the ledger, don't reason about it" — P48's move, pointed at Stripe
    instead of the chain. `transfer_failed` in particular means only that the
    API call raised: the transfer may well have been created and the response
    lost, so re-posting on the strength of our own record is how one crash
    becomes two payouts. `find_settlement_transfer()` is tri-state for that
    reason, and an unknown answer leaves the row exactly as it was.

    Stripe's `Idempotency-Key` would cover a same-day replay on its own; it
    expires after 24 hours, and every row this loop exists for is older than
    that by construction, so the key is not the guard here — the lookup is.
    """
    from emporia.payments import (
        find_settlement_transfer, payout_winner, stripe_account_mode_mismatch,
    )

    counts = {"owed": 0, "recovered": 0, "paid": 0, "unknown": 0, "still_blocked": 0}
    for row in _owed_payout_rows():
        counts["owed"] += 1
        known, transfer_id = await find_settlement_transfer(
            transfer_group=row["session_id"], dedupe_key=row["dedupe_key"],
        )
        if not known:
            # Could not ask. Never rounded down to "not paid".
            counts["unknown"] += 1
            continue
        if transfer_id:
            # It landed and we lost the answer. No money moves here.
            _record_payout_outcome(row["settlement_id"], "transferred", transfer_id)
            counts["recovered"] += 1
            continue

        creator = row["winner_id"]
        account = AGENT_REGISTRY.get_stripe_account(creator) if creator else None
        if not account:
            blocked = "no_stripe_account"
        elif stripe_account_mode_mismatch(AGENT_REGISTRY.get_stripe_account_mode(creator)):
            blocked = "pending_connect_wrong_mode"
        elif AGENT_REGISTRY.get_stripe_account_status(creator) != "active":
            blocked = "pending_connect_not_active"
        else:
            blocked = ""
        if blocked:
            # Still not payable — refresh the recorded reason (it may have
            # changed) and leave it owed for the next pass.
            if blocked != row["transfer_status"]:
                _record_payout_outcome(row["settlement_id"], blocked, None)
            counts["still_blocked"] += 1
            continue

        try:
            transfer = await payout_winner(
                winner_id=creator,
                stripe_account_id=account,
                amount_cents=row["winner_payout_cents"],
                session_id=row["session_id"],
                idempotency_key="emporia:payout:" + row["dedupe_key"],
            )
        except Exception as e:
            logger.warning(
                "Payout reconcile failed for settlement %s: %s", row["settlement_id"], e
            )
            _record_payout_outcome(row["settlement_id"], "transfer_failed", None)
            continue
        _record_payout_outcome(row["settlement_id"], "transferred", transfer["transfer_id"])
        counts["paid"] += 1
    return counts


async def _payout_reconcile_loop() -> None:
    """Runs _reconcile_owed_payouts() on EMPORIA_PAYOUT_RECONCILE_INTERVAL_SECONDS
    (default 300s). Same shape as _sweep_loop(), including N12-1's rule that one
    raise must not end the loop for the life of the process — this one moves
    money, so a loop that died silently would be indistinguishable from the
    defect it exists to fix.
    """
    while True:
        await asyncio.sleep(_PAYOUT_RECONCILE_INTERVAL_SECONDS)
        try:
            counts = await _reconcile_owed_payouts()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payout reconcile failed — will retry next interval")
            PAYOUT_RECONCILE_STATS["last_error_at"] = datetime.now(UTC).isoformat()
            PAYOUT_RECONCILE_STATS["consecutive_failures"] += 1
            continue
        PAYOUT_RECONCILE_STATS["runs"] += 1
        PAYOUT_RECONCILE_STATS["last_run_at"] = datetime.now(UTC).isoformat()
        PAYOUT_RECONCILE_STATS["consecutive_failures"] = 0
        for key, count in counts.items():
            PAYOUT_RECONCILE_STATS["outcomes"][key] += count


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
    # P16: publish the X25519 encryption key in the same round trip. Optional —
    # an agent that doesn't do encrypted DMs sends neither field.
    encryption_public_key_hex: str = ""
    encryption_key_signature: str = ""


class SetX402WalletRequest(BaseModel):
    network_family: str  # "evm" | "svm"
    address: str


class SetEncryptionKeyRequest(BaseModel):
    encryption_public_key_hex: str
    signature: str  # Ed25519 over wire.signed_encryption_key_payload()


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
    msg_type: str = ""                # "chat" | "collab" | "code" | "counter_offer" | "accept" | "reject"
    parent_message_id: str | None = None
    signature: str | None = None
    # negotiation constraints (used when msg_type="counter_offer")
    negotiation_constraints: dict[str, Any] | None = None
    # P19-5: sender-chosen, inside the signature and (on an encrypted room) the
    # AAD — the room analogue of DMSendRequest.nonce. Empty is rejected rather
    # than defaulted, same reasoning as the DM field: a silent fallback would
    # leave replay open to any client that just omitted it.
    nonce: str = ""


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
            # P5-1: was `NOT IN ('complete','cancelled')` — neither literal is
            # ever written to sessions.status, so this counted completed and
            # abandoned sessions as active. Built from _SESSION_TERMINAL_STATUSES
            # now instead of a second hand-copied literal list.
            row2 = conn.execute(
                f"SELECT COUNT(*) AS c FROM sessions WHERE {_session_status_not_in_terminal_sql()}",
                tuple(_SESSION_TERMINAL_STATUSES),
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
        "version": __version__,
        "modules": list(MODULE_REGISTRY.keys()),
        "listing_count": listing_count,
        "session_count": session_count,
        "guardrails_mode": guardrails.DEFAULT_MODE,
        "require_nous": REQUIRE_NOUS,
        "operator_fee_bps": OPERATOR_FEE_BPS,
        "mpp_enabled": bool(payment_methods),
        "payment_methods": payment_methods,
        "stripe_enabled": stripe_enabled,
        # P37-1: which key space, not just whether a key exists. The two are
        # disjoint, and the correct payout advice is opposite in each: test mode
        # auto-provisions a Connected Account at registration and
        # /agents/{id}/stripe-onboarding refuses to run, while live mode needs
        # the operator to walk Stripe's hosted KYC. Without this the dashboard
        # was telling test-mode operators to run a tool that 400s at them.
        "stripe_mode": _stripe_key_mode(),
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
        # P51: liveness only. This route is unauthenticated, and
        # PAYOUT_RECONCILE_STATS["outcomes"] counts how many settlements the
        # relay currently owes creators — an aggregate about other people's
        # money, which settlement reads are ownership-scoped for (N8-R4/R5).
        # A stalled loop has to be visible to an operator, though, because a
        # loop that died silently is indistinguishable from the defect P51
        # exists to fix. So: is it running, and is it failing — nothing about
        # what it found. (This route has outgrown its name; `sweep` was already
        # not a safety counter. Renaming it is a breaking change and its own
        # decision, recorded in ROADMAP rather than done in passing.)
        "payout_reconcile": {
            "runs": PAYOUT_RECONCILE_STATS["runs"],
            "last_run_at": PAYOUT_RECONCILE_STATS["last_run_at"],
            "last_error_at": PAYOUT_RECONCILE_STATS["last_error_at"],
            "consecutive_failures": PAYOUT_RECONCILE_STATS["consecutive_failures"],
        },
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
async def ui_config(request: Request):
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
        # P36-5: whether THIS connection may claim an identity with the
        # X-Emporia-Agent-Id header. The dashboard used to infer it from the
        # relay URL looking local, which is the wrong question: header auth is
        # off unless EMPORIA_TRUST_LOCAL_HEADER_AUTH=1, so a co-located relay
        # rejects the header too, and the dashboard hid its sign-in panel anyway
        # and left every agent-scoped view showing a bare 401.
        "header_auth_trusted": _is_header_auth_trusted(request),
        "agent_count": agent_count,
        "active_session_count": active_session_count,
        # P36-2: was a hardcoded "2.0.0" — undocumented, present since the initial
        # commit, and disagreeing with the "0.1.0" every other endpoint reported.
        # The dashboard showed both at once: v0.1.0 in the relay strip, 2.0.0 in
        # the profile. One source now, which is also what makes "bump the version
        # to make a break loud" a thing you can actually do.
        "version": __version__,
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

    # P16: the X25519 encryption key, if the client published it inline. Runs after
    # AGENT_REGISTRY.register() because the binding is verified against the Ed25519
    # key that call just stored, and goes through the same
    # _validated_encryption_key() as the standalone endpoint so the two paths can't
    # apply different rules. A bad key fails the whole registration rather than
    # succeeding quietly without one — an agent who thinks they published a key but
    # didn't is invisible to their peers, who just see someone who doesn't do
    # encrypted DMs.
    if req.encryption_public_key_hex:
        enc_key = _validated_encryption_key(
            canonical_id, req.encryption_public_key_hex, req.encryption_key_signature,
        )
        AGENT_REGISTRY.set_encryption_key(canonical_id, enc_key, req.encryption_key_signature)

    # Auto-provision a Stripe Connected Account in test mode so the agent can
    # receive real (test) payouts without manual Stripe onboarding.
    stripe_account_id: str | None = None
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if stripe_key.startswith("sk_test_"):
        try:
            from emporia.payments import create_connected_account
            acct = await create_connected_account(canonical_id)
            stripe_account_id = acct["stripe_account_id"]
            # P26-4: tag the key space that created it — this block only ever
            # runs under sk_test_, so "test" is a fact here, not an inference.
            AGENT_REGISTRY.set_stripe_account(canonical_id, stripe_account_id, mode="test")
            # P25-2: test mode is the one case where 'active' is correct
            # immediately — Stripe auto-approves a Custom account's
            # capabilities in test mode (create_connected_account()'s own
            # docstring), so there's no webhook to wait for.
            AGENT_REGISTRY.set_stripe_account_status(stripe_account_id, "active")
        except Exception:
            pass  # non-fatal — agent can still play free sessions

    return {
        "status": "registered",
        "agent_id": canonical_id,
        # P47: the agent learns its own permanent identity here. Without it an
        # agent would have to make a second request to find out who it is, and
        # would have no way to notice a handle it re-registered was allocated to
        # a different identity than it expected.
        "stable_id": profile["stable_id"],
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
            "stripe_account_status, nous_user_id, trust_level, identity_providers_json, "
            "encryption_public_key, public_key_hex, stable_id "
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
        # P47: `agent_id` is the handle (mutable, claimable); `stable_id` is the
        # identity (permanent, pubkey-anchored). A client that wants to remember
        # *who* someone is stores the latter — see stable_id_for()'s docstring.
        "stable_id": row["stable_id"],
        "display_name": row["display_name"] or row["agent_id"],
        "trust_level": row["trust_level"] or "key_only",
        "nous_user_id": row["nous_user_id"],
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
        "registered_at": row["registered_at"],
        "is_active": bool(row["is_active"]),
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


class RenameAgentRequest(BaseModel):
    new_agent_id: str


@app.post("/agents/{agent_id}/rename")
async def rename_agent(agent_id: str, req: RenameAgentRequest, request: Request):
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
    _require_caller_is(agent_id, request)
    _require_registered(agent_id)
    _enforce_agent_rate_limit(agent_id)
    try:
        result = AGENT_REGISTRY.rename(agent_id, req.new_agent_id)
    except KeyError as e:
        # Taken, not malformed — a different answer to the caller, who should
        # pick another name rather than fix this one.
        raise HTTPException(409, str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info(
        "agent_renamed",
        extra={
            "stable_id": result["stable_id"],
            "from_agent_id": result["previous_agent_id"],
            "to_agent_id": result["agent_id"],
        },
    )
    return result


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


def _stripe_onboarding_return_url(request: Request) -> str:
    base = (os.getenv("EMPORIA_DASHBOARD_BASE_URL") or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/stripe-onboarding-return"


@app.post("/agents/{agent_id}/stripe-onboarding")
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
    _require_caller_is(agent_id, request)
    _require_registered(agent_id)
    _enforce_agent_rate_limit(agent_id)

    stripe_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not stripe_key:
        raise HTTPException(400, "Stripe is not configured on this relay")
    if stripe_key.startswith("sk_test_"):
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

    key_mode = _stripe_key_mode()
    stripe_account_id = AGENT_REGISTRY.get_stripe_account(agent_id)
    account_mode = AGENT_REGISTRY.get_stripe_account_mode(agent_id)
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
        AGENT_REGISTRY.set_stripe_account(agent_id, stripe_account_id, mode=key_mode or "live")
        AGENT_REGISTRY.set_stripe_account_status(stripe_account_id, "pending")

    return_url = _stripe_onboarding_return_url(request)
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


@app.get("/stripe-onboarding-return", response_class=HTMLResponse)
async def stripe_onboarding_return_page():
    """Landing page for Stripe's Account Link refresh_url/return_url (P25-2).

    Deliberately plain HTML served by the relay itself, not the dashboard
    SPA — Stripe just needs a valid URL to redirect to, and completing the
    hosted form here is not the same as the account being payable (capability
    activation is asynchronous). Only POST /webhooks/stripe's verified
    account.updated event ever confirms that; this page must not claim it.
    """
    return (
        "<!doctype html><html><head><title>Emporia — Stripe onboarding</title></head>"
        "<body style=\"font-family: sans-serif; max-width: 32em; margin: 4em auto; padding: 0 1em;\">"
        "<h1>Onboarding step complete</h1>"
        "<p>Stripe has recorded what you submitted. Your account becomes payable "
        "automatically once Stripe finishes its own verification — check this "
        "agent's <code>stripe_account_status</code> field (via <code>GET /agents/"
        "{agent_id}</code> or the dashboard) to see when that happens. "
        "You can close this tab.</p>"
        "</body></html>"
    )


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Verified receiver for Stripe's `account.updated` events (P25-2) — the
    only thing in this codebase allowed to mark a live-mode Connected Account
    'active'. Security-critical: an unverified webhook would let anyone POST
    a claim that any Stripe account is payable. Reads the raw body before any
    JSON parsing — FastAPI's model binding would consume/reparse it, and
    Stripe's signature is computed over the exact raw bytes, not a
    re-serialized re-encoding of them. Unauthenticated by design (this is
    Stripe's server calling us, not an Emporia agent) — the signature check
    is the entire authorization boundary, which is why it must never fail
    open (see _verify_stripe_webhook_signature's own docstring).

    Always returns 200 once the signature verifies, even for an event type
    this relay doesn't act on or an account id it doesn't recognize (logged,
    not rejected) — Stripe retries non-2xx responses, and there's nothing to
    gain by making it retry an event this relay will never act on differently.
    """
    raw_body = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")
    secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not _verify_stripe_webhook_signature(raw_body, sig_header, secret):
        raise HTTPException(403, "invalid Stripe webhook signature")

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid JSON body")

    if not isinstance(event, dict):
        return {"received": True, "ignored": "payload is not an object"}

    # P26-5 (L10): Stripe delivers test-mode and live-mode events to the same
    # configured endpoint. Acting on the wrong one would let a test-mode
    # capability report mark a *live* account payable, so an event whose
    # livemode disagrees with this relay's own key is ignored outright.
    # `livemode` absent (older payloads, hand-built test fixtures) is treated
    # as "matches" rather than rejected, so this can't silently drop real work.
    key_mode = _stripe_key_mode()
    event_livemode = event.get("livemode")
    # P27-5: whatever this block concludes about the event's key space is worth
    # persisting. The mode guard can only protect a row whose mode is known,
    # and P25-era accounts read "" forever -- P25 had no mode column, and the
    # P26-1 backfill can't help them either (it repairs 'none' rows, while a
    # P25 live account sits at 'pending'/'active'). Stamping here publishes a
    # fact this handler has already established rather than inferring one, so
    # it's set only when the event states `livemode` outright.
    proven_mode = ""
    if isinstance(event_livemode, bool) and key_mode in ("test", "live"):
        if event_livemode != (key_mode == "live"):
            logger.info(
                "Stripe webhook ignored: event livemode=%s but relay key is %s",
                event_livemode, key_mode,
            )
            return {"received": True, "ignored": "livemode mismatch"}
        proven_mode = "live" if event_livemode else "test"

    event_type = event.get("type")
    # P26-3 (M3): every field below is attacker-shaped only in the sense that
    # Stripe could reshape it in a future API version -- but P25 assumed dicts
    # throughout (`.get("object") or {}` doesn't save you when `object` is a
    # *string*), so any such change became a 500, and Stripe's retry-on-non-2xx
    # turned that into an infinite loop. Every access is now shape-checked.
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    obj = obj if isinstance(obj, dict) else {}

    if event_type == "account.updated":
        stripe_account_id = obj.get("id")
        capabilities = obj.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if isinstance(stripe_account_id, str) and stripe_account_id:
            # P27-6: `transfers` alone, deliberately. This status has exactly
            # one consumer -- the pre-check guarding payout_winner(), which
            # issues `POST /v1/transfers` with `destination=acct_...`. That
            # needs the transfers capability and nothing else. P26 also
            # required `card_payments`, which governs the connected account
            # accepting card charges directly -- something this codebase never
            # does (every charge is created on the platform account; no call
            # passes `destination`/`on_behalf_of` on a charge). Requiring it
            # pinned any transfers-approved-but-card-restricted account, a
            # normal outcome for an account that only receives, at 'pending'
            # forever: payer charged, payee never paid, no retry. Same
            # money-stranding class as P26-1, reached through P26's own fix.
            status = "active" if capabilities.get("transfers") == "active" else "pending"
            if not AGENT_REGISTRY.set_stripe_account_status(
                stripe_account_id, status, mode=proven_mode,
            ):
                logger.warning(
                    "Stripe webhook account.updated for unrecognized account %s",
                    stripe_account_id,
                )
    elif event_type == "account.application.deauthorized":
        # P26-5 (L10): the operator disconnected this account in Stripe. It can
        # no longer receive transfers, so leaving it 'active' would put it right
        # back in the doomed-transfer case the pre-check exists to prevent.
        # The account id is the event's top-level `account` field here -- the
        # `data.object` is the *application*, not the account.
        stripe_account_id = event.get("account")
        if isinstance(stripe_account_id, str) and stripe_account_id:
            AGENT_REGISTRY.set_stripe_account_status(stripe_account_id, "none")

    return {"received": True}


def _validated_encryption_key(agent_id: str, public_key_hex: str, signature: str) -> str:
    """Shared by `POST /agents/{id}/encryption-key` and the inline field on
    registration, so the two paths cannot end up applying different rules to the
    same key.

    Returns the normalized key. Raises `HTTPException` — 400 for a malformed key,
    403 for a binding that doesn't verify.
    """
    from emporia.wire import signed_encryption_key_payload

    key = (public_key_hex or "").strip().lower()
    try:
        if len(bytes.fromhex(key)) != 32:
            raise ValueError()
    except ValueError:
        raise HTTPException(
            400, "encryption_public_key_hex must be a 64-char hex string (X25519 public key)"
        )
    # P19 (H1): any 32 bytes pass the length check above and *were* being accepted
    # as a published key — including a low-order/all-zeros point, which is a
    # well-formed public key that produces a fixed, attacker-known shared secret
    # and, worse, makes `crypto_box._wrap_key_for()`'s exchange() raise instead of
    # returning one. crypto_box.py's own docstring on that guard explains the
    # blast radius: before P19, any member of an *encrypted room* publishing such
    # a key permanently broke sealing for every other member, since seal() wraps
    # to every recipient in one call. Testing agreement against a throwaway
    # ephemeral key here — the same operation _wrap_key_for() performs — catches
    # every low-order point, not just the all-zeros case, and rejects it before it
    # ever reaches storage rather than discovering it mid-send.
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    try:
        X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(key)))
    except ValueError:
        raise HTTPException(
            400,
            "encryption_public_key_hex is not usable for key agreement (a low-order "
            "or all-zeros point) — generate a real X25519 keypair",
        )
    # Verified here, at publish time, so a key whose binding doesn't check out
    # never reaches storage. The relay is the one *serving* this key to peers, so
    # a peer who skipped the check would otherwise be trusting the relay's word —
    # which is exactly the trust this whole feature is built to avoid needing.
    # verify_signature() also returns False when no Ed25519 key is registered for
    # this agent, which is the correct answer for an unregistered publisher.
    if not AGENT_REGISTRY.verify_signature(
        agent_id, signed_encryption_key_payload(agent_id, key), signature,
    ):
        raise HTTPException(
            403,
            "encryption key signature does not verify against this agent's Ed25519 "
            "key — the key must be signed by the identity publishing it",
        )
    return key


@app.post("/agents/{agent_id}/encryption-key")
async def set_encryption_key(agent_id: str, req: SetEncryptionKeyRequest, request: Request):
    """Publish (or rotate) this agent's X25519 public key for end-to-end encrypted
    DMs. The relay stores and serves it; it never holds the matching private key,
    which lives 0600 under the agent's own `identity.KEY_DIR` and is never
    transmitted.

    Rotation is allowed and is a normal operation — but note that peers pin this
    key on first use, so a rotation requires each peer to confirm the new
    fingerprint out of band before their client will encrypt again. That friction
    is the feature.
    """
    _require_caller_is(agent_id, request)
    _require_registered(agent_id)
    _enforce_agent_rate_limit(agent_id)
    key = _validated_encryption_key(agent_id, req.encryption_public_key_hex, req.signature)
    AGENT_REGISTRY.set_encryption_key(agent_id, key, req.signature)
    return {"agent_id": agent_id, "encryption_public_key": key}


@app.get("/agents/{agent_id}/encryption-key")
async def get_encryption_key(agent_id: str, request: Request):
    """Fetch an agent's published encryption key and its Ed25519 binding.

    Returns `""` rather than 404 when the agent has published nothing: "does not
    do encrypted DMs" is an ordinary state clients branch on, and a 404 here would
    be indistinguishable from "no such agent". Authenticated for the same reason
    `GET /agents/{id}` is (N4-3) — key discovery stays a feature, it just doesn't
    happen anonymously.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    if not AGENT_REGISTRY.is_authorized(agent_id):
        raise HTTPException(404, f"Agent not found: {agent_id}")
    key, signature = AGENT_REGISTRY.get_encryption_key(agent_id)
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
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
    }


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
    # P19-7 (M4)/P20-5 (A3): title/description/metadata used to be bounded by
    # hand here. P21-5 (B5): _assert_payload_safe_counted now bounds every
    # top-level field of whatever it's handed, so this is redundant — see
    # _require_payload_content_within_limit.
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
    # P21-5 (B5): title/description bounded automatically by
    # _assert_payload_safe_counted now — see _require_payload_content_within_limit.
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


async def _fetch_federation_batch(url: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """GET a federation feed with the response body capped at
    MAX_FEDERATION_RESPONSE_BYTES, enforced while streaming rather than after a full
    buffer.

    P21-1 (B1): both federation pulls used to call `client.get(url)` then
    `resp.json()` — a non-streaming call already reads the *entire* body into memory
    before returning, so by the time the signature check below got a chance to reject
    an untrusted peer's response, an arbitrarily large body (a hijacked peer URL, or a
    misbehaving one) had already been fully buffered. Content-Length isn't trusted
    alone — chunked transfer can omit or lie about it — so the cap is enforced against
    actual bytes read as they arrive.

    `client` is injectable for tests; the two callers below always let this open (and
    close) its own short-lived client, same as they did before this helper existed.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15)
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_FEDERATION_RESPONSE_BYTES:
                    raise ValueError(
                        f"peer response exceeds {MAX_FEDERATION_RESPONSE_BYTES} "
                        "bytes — refusing to buffer further"
                    )
            parsed = json.loads(bytes(body))
            # P24-4 (W4): both callers immediately do `data.get(...)` on the return
            # value, outside their own try/except -- valid JSON that isn't an object
            # (a peer serving a bare list/string/number) used to raise AttributeError
            # straight out of _sync_all_federated_peers()'s asyncio.gather(), 500ing
            # the sync endpoint and leaving sibling peer tasks orphaned. Rejecting
            # here, the one chokepoint both pullers already route through, converts
            # it into an ordinary per-peer failure their existing except-and-record
            # path already handles.
            if not isinstance(parsed, dict):
                raise ValueError("peer response is not a JSON object")
            return parsed
    finally:
        if owns_client:
            await client.aclose()


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
        data = await _fetch_federation_batch(f"{peer_url}/gaming/v1/federate/listings")
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
        data = await _fetch_federation_batch(f"{peer_url}/gaming/v1/federate/lobby")
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


async def _sync_all_federated_peers() -> tuple[dict[str, int], dict[str, int]]:
    """Pull listings + lobby from every configured peer, concurrently.

    P23-3: used to be a `for peer in FEDERATED_RELAYS: await ...` loop -- both
    `_pull_federated_listings`/`_pull_federated_lobby` already catch and record
    per-peer failures into `_FEDERATION_LAST_SYNC`/`_FEDERATION_LOBBY_LAST_SYNC`
    rather than raising, so a bad peer never crashed the loop, it just serialized
    everyone behind its own `_fetch_federation_batch` timeout (15s) needlessly.
    `asyncio.gather` here cuts worst-case wall time from O(peers) to
    O(slowest peer) since each peer's own error handling already isolates
    failures -- nothing about correctness changes, only how long it takes.
    Shared by the manual `POST /gaming/v1/federate/sync` endpoint and the
    periodic `_federation_sync_loop()` below.

    P24-4 (W4): that "already catch and record" claim was wrong once -- a peer
    serving valid-but-non-dict JSON raised past both pullers' own try/except
    (closed at the source in `_fetch_federation_batch()` now), and an
    uncaught exception from a bare `asyncio.gather()` propagates immediately,
    leaving every other still-running peer task orphaned: cancelled by
    nothing, still mutating `_FEDERATION_LAST_SYNC`/the DB after this
    function's caller has already received (and reported) a failure. Belt and
    braces: `return_exceptions=True` so one peer's surprise can never again
    take the whole fan-out down or abandon its siblings mid-flight, on top of
    (not instead of) fixing the currently-known escape at its source.
    """
    peers = list(FEDERATED_RELAYS)
    listing_results, lobby_results = await asyncio.gather(
        asyncio.gather(*(_pull_federated_listings(p) for p in peers), return_exceptions=True),
        asyncio.gather(*(_pull_federated_lobby(p) for p in peers), return_exceptions=True),
    )

    def _coerce(results: list) -> dict[str, int]:
        counts: dict[str, int] = {}
        for peer, result in zip(peers, results):
            if isinstance(result, BaseException):
                logger.error(
                    "federation_sync_peer_raised_unexpectedly",
                    extra={"peer_url": peer}, exc_info=result,
                )
                counts[peer] = 0
            else:
                counts[peer] = result
        return counts

    return _coerce(listing_results), _coerce(lobby_results)


_FEDERATION_SYNC_INTERVAL_SECONDS = float(os.getenv("EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS", "300"))
_FEDERATION_SYNC_TASK: asyncio.Task | None = None  # set/cancelled in lifespan(); exposed for tests/ops


async def _federation_sync_loop() -> None:
    """Runs _sync_all_federated_peers() on EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS
    (default 300s) -- mirrors _sweep_loop()'s exact shape. Started in lifespan()
    only when FEDERATED_RELAYS is non-empty; cancelled there on shutdown.

    P23-3: before this, `POST /gaming/v1/federate/sync` was the *only* sync
    path -- operator-triggered only, no periodic retry. Per-peer failures were
    already recorded and non-fatal (see _pull_federated_listings), so a
    transient failure just sat there until a human re-triggered. This periodic
    loop is itself the retry mechanism -- no separate backoff needed, the same
    reasoning _sweep_loop() already applies to its own failures.
    """
    while True:
        await asyncio.sleep(_FEDERATION_SYNC_INTERVAL_SECONDS)
        try:
            await _sync_all_federated_peers()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background federation sync failed — will retry next interval")


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
    results, lobby_results = await _sync_all_federated_peers()
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
    # P44: one read, not two. This used to call verify_chain(session_id) and
    # then get_public_log(session_id), reading and JSON-parsing the same file
    # twice per request — and verifying a *different* read than the one it
    # served, so a write landing between them would be returned unverified.
    # Off the loop for the same reason as the two chain routes: this parses the
    # whole log and serializes it back out, all of it blocking CPU.
    entries = await run_in_threadpool(get_public_log, session_id)
    ok, message = verify_chain_entries(entries)
    return {
        "session_id": session_id,
        "verified": ok,
        "message": message,
        "chain": entries,
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
    # P21-5 (B5): peer_text_rationale/payload used to be bounded by hand here
    # (payload's check also covered DisputeDeliveryRequest.reason, which flows
    # in as payload={"reason": ...}). _assert_payload_safe_counted below scans
    # full_payload, whose own top-level keys are exactly "payload" and
    # "peer_text_rationale" (see signed_action_payload) — same bound, for free.

    from emporia.wire import signed_action_payload
    full_payload = signed_action_payload(
        req.agent_id, req.action_type, req.payload, req.peer_text_rationale,
        session_id, s["step_number"],
    )

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
        logger.warning("unsigned_action_rejected", extra={"reason": "missing_signature"})
        raise HTTPException(401, "Signature required for session actions")
    if not AGENT_REGISTRY.verify_signature(req.agent_id, full_payload, req.signature):
        SAFETY_STATS["unsigned_actions_rejected"] += 1
        logger.warning("unsigned_action_rejected", extra={"reason": "invalid_signature"})
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
                    winner_stripe_account_status=AGENT_REGISTRY.get_stripe_account_status(winner_id),
                    winner_stripe_account_mode=AGENT_REGISTRY.get_stripe_account_mode(winner_id),
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
    rationale: str = ""
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
    from emporia.wire import DEFAULT_CONFIRM_RATIONALE
    return await submit_action(
        session_id=session_id,
        req=SubmitActionRequest(
            agent_id=req.agent_id,
            action_type="confirm",
            payload={},
            peer_text_rationale=req.rationale or DEFAULT_CONFIRM_RATIONALE,
            signature=req.signature,
        ),
    )


@app.post("/sessions/{session_id}/dispute-delivery")
async def dispute_delivery(session_id: str, req: DisputeDeliveryRequest, request: Request):
    """Buyer disputes delivery. Triggers refund: all escrowed holds released.

    Shorthand for submitting action_type='dispute' to an emporia:service:v1 session.
    """
    from emporia.wire import default_dispute_rationale
    return await submit_action(
        session_id=session_id,
        req=SubmitActionRequest(
            agent_id=req.agent_id,
            action_type="dispute",
            payload={"reason": req.reason},
            peer_text_rationale=req.rationale or default_dispute_rationale(req.reason),
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
    # P5-1: was `== "complete"`, a status never actually written (the real
    # terminal values are "completed" and "abandoned") — this guard never
    # fired, so a completed session could be "abandoned" a second time,
    # re-running cancel_payment_hold() over payments already captured.
    # Re-abandoning an already-abandoned session is refused for the same
    # reason: cancelling the same holds twice is the exact double-action
    # this guard exists to prevent, not a distinct case to allow.
    if s["status"] in _SESSION_TERMINAL_STATUSES:
        raise HTTPException(400, f"Session already {s['status']} — cannot abandon")

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


@app.delete("/admin/sessions/{session_id}")
async def delete_session(session_id: str, request: Request, force: bool = False):
    """P4-5 (ROADMAP.md § Architecture cleanup): operator-only cleanup for a
    finished session's operational rows. Hard-deletes `participants`,
    `actions`, `messages`, and the `sessions` row itself — one transaction,
    so this never leaves a session half-deleted.

    Deliberately does NOT delete `payments` or `settlements` rows — those
    are the record that real money moved, and deleting them destroys
    reconciliation. This is a permanent product decision, not an
    oversight the cascade should eventually "finish": the response reports
    exactly how many of each were retained. The `settlements.session_id`
    column is overloaded (session/room/topic escrow all key off it — see
    SECURITY.md's N4-R1) but the `sess_`/`room_`/`tpc_` id prefixes make a
    `WHERE session_id = ?` count on this table unambiguous.

    Also does NOT rewrite the audit trail. `_write_audit_line()` appends to
    a date-keyed `messages_{date}.jsonl`, not a per-session file, so
    "delete the session's audit files" would mean stripping lines out of an
    append-only log — destroying the integrity property the audit trail
    exists for. Instead, this appends one new audit line recording the
    deletion itself (operator, session_id, per-table counts).

    P8-1: an `admin_session_deleted` line is authoritative only if it is
    NOT followed by an `admin_session_delete_aborted` line for the same
    `session_id` — see the second DB_LOCK block below. A reader of the
    audit log must honor the abort.

    Refuses to delete a non-terminal session (`status` not in
    `completed`/`abandoned`) unless `?force=true` is passed, so a wedged
    session can still be cleaned up deliberately without silently deleting
    an in-progress one by mistake.

    P5-2: also clears `rooms.linked_session_id` for any room linked to this
    session — room creation *validates* the linked session exists
    (`create_room()`, 404 if not), so deleting the session without doing
    this leaves that invariant broken and `rooms_db.get_room_for_session()`
    (a live call, `_session_visibility()`) pointing at nothing. Clears the
    link rather than deleting the room: a room is its own resource with its
    own members and paid entitlements, and deleting it as a side effect of
    a session cleanup would destroy far more than the caller asked for.
    `rooms` lives in the same physical `EMPORIA_DB_PATH` file as `sessions`
    (rooms.py's own `ROOM_DB_PATH` resolves the same env var) — updated on
    this same connection so it's part of the one transaction, not a second
    connection to reason about ordering for.
    """
    caller = _dashboard_agent_id(request)
    _require_operator(caller, "Delete session")
    s = load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s["status"] not in _SESSION_TERMINAL_STATUSES and not force:
        raise HTTPException(
            409,
            f"Session status is {s['status']!r}, not terminal "
            f"({sorted(_SESSION_TERMINAL_STATUSES)}) — pass ?force=true to delete anyway",
        )

    # P5-3: the audit line must be written BEFORE any row is deleted — a
    # failed audit write (missing LOG_DIR, full disk) must abort the whole
    # request with nothing touched, rather than a deletion nothing recorded.
    #
    # P6-1: but _write_audit_line() must NOT run while DB_LOCK is held.
    # log_message() (this file's only other caller, above) deliberately
    # writes its audit line after releasing DB_LOCK — direct precedent that
    # this function doesn't belong under the lock. P5-3 put it there anyway.
    # Nothing here awaits, so this isn't the same async-starvation class as
    # the Agora/Stripe DB_LOCK-across-a-network-round-trip bug this codebase
    # already fixed once (ROADMAP.md § Architecture cleanup) — but it's the
    # same shape of mistake, so it gets fixed the same way: two short
    # DB_LOCK-held blocks (count, then delete) with the file write in
    # between, holding no lock.
    def _unlinked_and_deleted_counts(conn: sqlite3.Connection) -> tuple[dict, dict]:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM rooms WHERE linked_session_id = ?) AS rooms,
                (SELECT COUNT(*) FROM participants WHERE session_id = ?) AS participants,
                (SELECT COUNT(*) FROM actions WHERE session_id = ?) AS actions,
                (SELECT COUNT(*) FROM messages WHERE session_id = ?) AS messages,
                (SELECT COUNT(*) FROM sessions WHERE session_id = ?) AS sessions
            """,
            (session_id, session_id, session_id, session_id, session_id),
        ).fetchone()
        unlinked = {"rooms": row["rooms"]}
        deleted = {
            "participants": row["participants"],
            "actions": row["actions"],
            "messages": row["messages"],
            "sessions": row["sessions"],
        }
        return unlinked, deleted

    with DB_LOCK, get_db() as conn:
        unlinked, deleted = _unlinked_and_deleted_counts(conn)
        retained = {
            "payments": conn.execute(
                "SELECT COUNT(*) FROM payments WHERE session_id = ?", (session_id,)
            ).fetchone()[0],
            "settlements": conn.execute(
                "SELECT COUNT(*) FROM settlements WHERE session_id = ?", (session_id,)
            ).fetchone()[0],
        }

    _write_audit_line({
        "timestamp": datetime.now(UTC).isoformat(),
        "type": "admin_session_deleted",
        "operator": caller,
        "session_id": session_id,
        "deleted": deleted,
        "retained": retained,
        "unlinked": unlinked,
    })

    # Re-check counts, not just existence: a narrow TOCTOU window exists
    # between the two DB_LOCK blocks above/below (a concurrent submit_action/
    # join_session on this exact session — reachable only via ?force=true on
    # a non-terminal session, since neither of those write paths accepts a
    # terminal one). P7-1: the original version of this guard only checked
    # the session still existed, so a row landing in that window (e.g. a
    # concurrent submit_action's new `actions` row) got silently deleted
    # anyway — undercounting the audit line and the response already
    # committed to above. Re-running the same counts and refusing on ANY
    # mismatch (not just "gone") closes that; still no retry logic, same as
    # the existence-only version — this is an admin cleanup endpoint, fail
    # loudly and let the operator re-issue the call against the new state.
    #
    # P8-1: the "admin_session_deleted" line above is already on disk by the
    # time this block runs, and can't be un-written (append-only log) — so a
    # mismatch here means that line is now FALSE (it claims a deletion that's
    # about to be aborted). Don't raise from inside this lock block (that
    # would leave the false line uncorrected); record the mismatch, exit the
    # lock, then append a compensating "admin_session_delete_aborted" line —
    # same P6-1 rule (_write_audit_line() never runs under DB_LOCK) applies
    # to this line too — before raising.
    mismatch: tuple[dict, dict] | None = None
    with DB_LOCK, get_db() as conn:
        recheck_unlinked, recheck_deleted = _unlinked_and_deleted_counts(conn)
        if recheck_unlinked != unlinked or recheck_deleted != deleted:
            mismatch = (recheck_unlinked, recheck_deleted)
        else:
            conn.execute(
                "UPDATE rooms SET linked_session_id = NULL WHERE linked_session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM participants WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM actions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    if mismatch is not None:
        recheck_unlinked, recheck_deleted = mismatch
        # P9-1: this write can fail exactly like the first one (missing LOG_DIR,
        # full disk) — but unlike the first (P5-3's guard aborts before deleting
        # anything), there's nothing left here to protect: the session already
        # survived intact. Left unguarded, a failure here surfaces as a bare,
        # undiagnostic 500 (no @app.exception_handler is registered anywhere in
        # this file) and silently leaves the earlier "admin_session_deleted" line
        # unfollowed by the "aborted" line that would explain it's stale — exactly
        # the false-record shape P8-1 exists to prevent. No further compensation
        # is meaningful here (there's no useful "compensate for the compensating
        # write" step); the fix is making the failure legible instead of opaque.
        try:
            _write_audit_line({
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "admin_session_delete_aborted",
                "reason": "concurrent_modification",
                "operator": caller,
                "session_id": session_id,
                "expected": {"unlinked": unlinked, "deleted": deleted},
                "observed": {"unlinked": recheck_unlinked, "deleted": recheck_deleted},
            })
        except OSError as e:
            raise HTTPException(
                500,
                f"Session {session_id} was NOT deleted (concurrent modification detected), but "
                f"the compensating audit record could not be written: {e}. The earlier "
                "'admin_session_deleted' line for this session_id is stale — verify manually.",
            ) from e
        raise HTTPException(
            409, "Session was modified by a concurrent request — retry to delete the current state",
        )

    return {
        "session_id": session_id,
        "deleted": deleted,
        "retained": retained,
        "unlinked": unlinked,
        "message": "Payments and settlements are never deleted — see this endpoint's docstring.",
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
    # P19-7 (M4): payload is a dict, not a str — bound its serialized size, same
    # budget every other content-bearing field now gets.
    _require_content_within_limit(json.dumps(req.payload), "payload")
    if not req.signature:
        raise HTTPException(401, "Signature required")
    # N3-8 (R7): bound to the routing envelope (to_agent/type/session_id), not just
    # payload — a payload-only signature was valid for delivery to *any* recipient/type,
    # since those fields sit outside what was signed yet drive routing (:3339 below).
    from emporia.wire import signed_dm_envelope
    signed_envelope = signed_dm_envelope(req.to_agent, req.type, req.session_id, req.payload)
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
    ws_id = uuid.uuid4().hex[:12]
    logger.info("ws_connected", extra={"ws_id": ws_id, "channel": "global"})
    ws_connections["__global__"].add(ws)
    try:
        # Send a snapshot on connect
        health_data = {
            "status": "ok",
            "service": BRAND,
            "version": __version__,
            "listing_count": 0,
            "session_count": 0,
            "modules": list(MODULE_REGISTRY.keys()),
        }
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute("SELECT COUNT(*) AS c FROM listings WHERE status='open'").fetchone()
                health_data["listing_count"] = row["c"] if row else 0
                # P5-1: same fix as /health's REST handler above — see that
                # comment for why the old literals were wrong.
                row2 = conn.execute(
                    f"SELECT COUNT(*) AS c FROM sessions WHERE {_session_status_not_in_terminal_sql()}",
                    tuple(_SESSION_TERMINAL_STATUSES),
                ).fetchone()
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
        logger.info("ws_disconnected", extra={"ws_id": ws_id, "channel": "global"})
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
        logger.warning("ws_rejected", extra={"channel": "session", "session_id": session_id, "reason": "unknown_session"})
        await ws.close(code=1008)
        return
    if not _session_viewer_allowed(s, _ws_agent_id(ws)):
        logger.warning("ws_rejected", extra={"channel": "session", "session_id": session_id, "reason": "not_allowed"})
        await ws.close(code=1008)
        return
    await ws.accept()
    ws_id = uuid.uuid4().hex[:12]
    logger.info("ws_connected", extra={"ws_id": ws_id, "channel": "session", "session_id": session_id})
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
        logger.info("ws_disconnected", extra={"ws_id": ws_id, "channel": "session", "session_id": session_id})
        ws_connections[session_id].discard(ws)


@app.websocket("/ws/agent/{agent_id}")
async def ws_agent(ws: WebSocket, agent_id: str):
    """Per-agent notification channel — private. Caller must be the agent itself (or the
    relay operator); see _ws_agent_id()."""
    caller = _ws_agent_id(ws)
    owner = _relay_operator_id()
    if caller != agent_id and not (owner and caller == owner):
        logger.warning("ws_rejected", extra={"channel": "agent", "agent_id": agent_id, "reason": "not_authorized"})
        await ws.close(code=1008)
        return
    await ws.accept()
    ws_id = uuid.uuid4().hex[:12]
    logger.info("ws_connected", extra={"ws_id": ws_id, "channel": "agent", "agent_id": agent_id})
    key = f"agent:{agent_id}"
    ws_connections[key].add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("ws_disconnected", extra={"ws_id": ws_id, "channel": "agent", "agent_id": agent_id})
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
    # P21-5 (B5): name/description bounded automatically — see
    # _require_payload_content_within_limit.
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
    # P18-4: same rule encrypted DM threads already have. An encrypted room whose
    # creator has no published key is a room in which nothing can ever be sent —
    # every message would fail envelope validation with an error nowhere near the
    # cause. Joiners are checked at join time, not here, since they don't exist yet.
    if req.encrypted:
        _require_encryption_keys_published([caller])
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
    payout — mirrors record_payment's placement relative to payout, for the
    same "commit the durable record before the outbound call" reason both
    join_room and subscribe_agora_topic already followed.

    P50: **exactly-once per payment.** Both membership paths now re-enter this
    function to finish a purchase a crash cut short (see
    `_finish_interrupted_access_purchase()`), so "already ran" has to be a
    thing this function can answer about itself rather than a property of who
    calls it. It answers from the settlement row it writes last, whose
    `dedupe_key` is UNIQUE — the durable record, not an inference from
    membership or from holding some entitlement here.

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

    # P50: this function is exactly-once per payment, because
    # _finish_interrupted_access_purchase() below re-enters it to finish a
    # purchase a crash cut short. The settlement row is written last and its
    # `dedupe_key` is UNIQUE, so the row's existence *is* the record that this
    # whole function already ran to the end — the same "ask the durable record,
    # don't reason about it" move P48 made with the chain.
    dedupe_key = settlement_dedupe_key(resource_id, payer_id, [payment_ref.reference])
    if _settlement_exists(dedupe_key):
        return

    # Skip only a grant *this same payment* already bought. has_any_grant()
    # would be wrong here: a payer re-purchasing after their window expired
    # holds an older grant and must still get the one they just paid for.
    if not entitlements.has_grant_for_payment(
        resource_type, resource_id, payer_id, payment_ref.reference,
        db_path=DATABASE_PATH,
    ):
        entitlements.grant(
            resource_type, resource_id, payer_id, payment_ref.reference,
            access_duration_days, rail=payment_ref.rail, db_path=DATABASE_PATH,
        )
    if payment_ref.rail == RAIL_X402:
        # P48: the entitlement now exists, so the purchase this intent was
        # guarding is finished and the intent must go — otherwise a *later*
        # re-purchase of the same resource, after this entitlement expires,
        # would collide with the settled row and be handed free access. This
        # is the retirement point because the entitlement is what the payment
        # bought; see _retire_x402_intent().
        _retire_x402_intent(
            payer_id=payer_id, resource_type=resource_type,
            resource_id=resource_id, amount_cents=amount_cents,
        )
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
            if creator_stripe and _stripe_account_mode_mismatch(
                AGENT_REGISTRY.get_stripe_account_mode(creator_id)
            ):
                # P26-4: account belongs to the other Stripe key space (e.g. a
                # test-mode leftover after switching to a live key) — the two
                # acct_... namespaces are disjoint, so this transfer cannot
                # succeed and must not be attempted.
                transfer_status = "pending_connect_wrong_mode"
            elif creator_stripe and AGENT_REGISTRY.get_stripe_account_status(creator_id) != "active":
                # P25-2: a live-mode account that hasn't finished onboarding
                # yet — skip the doomed Stripe call rather than let it fail
                # against Stripe's own API and surface as an opaque
                # "transfer_failed: ...".
                transfer_status = "pending_connect_not_active"
            elif creator_stripe:
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


def _settlement_exists(dedupe_key: str) -> bool:
    """P50: has this exact (resource, payer, payment) settlement already been
    recorded? `settlements.dedupe_key` is UNIQUE (see
    `_migrate_settlement_dedupe_key`) and `record_settlement()` is the last
    statement of `_settle_access_payment()`, so the row's presence is a durable
    "that purchase finished" flag rather than an inference from other state.
    """
    with DB_LOCK, get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM settlements WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)
        ).fetchone() is not None


async def _finish_interrupted_access_purchase(
    *,
    resource_type: str,
    resource_id: str,
    outcome_type: str,
    payment_type: str,
    payer_id: str,
    creator_id: str,
    access_duration_days: int | None,
) -> None:
    """P50: finish a paid join whose settlement never ran, before answering
    "you are already a member".

    Both membership paths commit the membership row first and settle after —
    deliberately, so a retry lands on the already-member short-circuit instead
    of re-entering payout work (N5 review R2). What that ordering did not
    account for is that the short-circuit then *ends* the request: a relay that
    died between the membership write and `_settle_access_payment()` leaves a
    payer whose money is captured and recorded, with no entitlement, no payout
    to the creator and no settlement row — and every retry from then on returns
    200 and repairs none of it. The short-circuit reads membership as proof the
    purchase completed. It is not: it is proof only that the membership write
    is the one that landed.

    So the short-circuit now finishes the purchase before it short-circuits.
    Re-entry is safe because `_settle_access_payment()` is exactly-once per
    payment (its settlement row is the key), and it is *needed* because
    nothing else will ever run: there is no reconciler, and the payer cannot
    reach the settlement path again by paying more.

    Minting a `PaymentRef` here is the one place outside
    `_verify_and_confirm_payment()` that does so. It is not a second
    verification path: the row being read exists only because that funnel
    already verified this payment and `record_payment()` committed it. The
    rail is read back from storage rather than assumed (P3).
    """
    from emporia.payments import PaymentRef

    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT payment_intent_id, amount_cents, rail FROM payments "
            "WHERE room_id = ? AND agent_id = ? AND payment_type = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (resource_id, payer_id, payment_type),
        ).fetchone()
    if not row:
        # Never paid for this resource — an open room, a free topic, or a
        # member who was invited rather than charged. Nothing to finish.
        return

    await _settle_access_payment(
        resource_type=resource_type,
        resource_id=resource_id,
        outcome_type=outcome_type,
        payer_id=payer_id,
        creator_id=creator_id,
        amount_cents=row["amount_cents"],
        payment_ref=PaymentRef(row["rail"], row["payment_intent_id"]),
        access_duration_days=access_duration_days,
    )


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
        # P50: membership is not proof of purchase — finish the settlement a
        # crash may have cut short before answering. See
        # _finish_interrupted_access_purchase().
        await _finish_interrupted_access_purchase(
            resource_type="room", resource_id=room_id,
            outcome_type="room_entry", payment_type="room_entry",
            payer_id=req.agent_id, creator_id=room.creator_id,
            access_duration_days=room.access_duration_days,
        )
        return {"status": "already_member", "room_id": room_id}
    # P18-4: joining an encrypted room without a published key would make every
    # subsequent message in that room fail coverage validation — one member with no
    # key blocks the whole room, not just themselves. Refused at the door, where the
    # error names the one thing the joiner has to do.
    if room.encrypted:
        _require_encryption_keys_published([req.agent_id])
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

    # P28-1: capacity is checked BEFORE the payment gate below. It used to be
    # checked only by add_member(), which runs *after* the entry fee has been
    # confirmed and recorded — and the room path confirms with
    # capture_method="automatic"/status "succeeded", so the money is captured,
    # not held. Joining a full paid room therefore charged the payer and then
    # answered 403 with no membership, no entitlement and no refund. add_member()
    # still re-checks under the write lock (two agents can race the last seat);
    # this is the early refusal that keeps a doomed join away from the money.
    if rooms_db.room_is_full(room_id, db_path=DATABASE_PATH):
        raise HTTPException(403, "Room is full")

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
    #
    # P50 correction: "payout_winner is a real, non-idempotent Stripe transfer" was
    # true when N5 wrote it and stopped being true at P1a-3, which made
    # `idempotency_key` required and derived it from settlement_dedupe_key() — a
    # resumed payout for the same payment reuses the same key and is a no-op at
    # Stripe. The ordering is still right, and the residual reason is narrower than
    # the one recorded: Stripe expires an Idempotency-Key after 24 hours, so a
    # *very* late replay would move money twice. What was wrong was treating the
    # short-circuit as free — it also ended the request, stranding any purchase the
    # crash had left unfinished. See _finish_interrupted_access_purchase().
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
                logger.error(
                    "refund_persist_failed_after_reversal",
                    extra={"settlement_id": settlement_id, "reversal_id": reversal_id},
                    exc_info=persist_exc,
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
        logger.error(
            "refund_persist_failed",
            extra={"settlement_id": settlement_id, "reversal_id": reversal_id},
            exc_info=persist_exc,
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

    # P12: msg_type resolved here (not left as a bare pydantic field default) so it's
    # single-sourced from wire.py the same way confirm/dispute's rationale is — a raw
    # client omitting the optional field used to be verified against this file's own
    # hardcoded "chat" copy, a hand-copied literal that could silently diverge from the
    # one mcp_server.py/agent_sdk.py's signers actually send. Resolved once and reused
    # for both signature verification and persistence below, so an omitted field can
    # never land as an empty msg_type in storage. Local import (not module-level) is
    # deliberate, matching confirm_delivery/dispute_delivery: it re-reads
    # wire.DEFAULT_MSG_TYPE's current value on every call rather than binding a copy at
    # module-import time, which is what makes both this and the rationale constants
    # actually re-patchable at test/runtime rather than merely another hardcoded copy.
    from emporia.wire import (
        ERR_DUPLICATE_NONCE,
        ERR_MISSING_RECIPIENTS,
        DEFAULT_MSG_TYPE,
        signed_room_message_payload,
    )
    effective_msg_type = req.msg_type or DEFAULT_MSG_TYPE
    _require_valid_msg_type(effective_msg_type)
    _require_content_within_limit(req.content)

    # Encrypted rooms: relay stores opaque ciphertext — never scan it.
    # Ciphertext is binary/base64 and WILL false-positive on injection patterns.
    # Membership check above is the only gate; clients own key exchange + decryption.
    # P15: msg_type is no longer in this payload. P14 added it (a real gap at the time —
    # the field was free-form and prose-capable), but _require_valid_msg_type() above now
    # rejects anything outside a closed six-value enum, every member of which is exempt
    # from scanning anyway as a token-shaped value, so scanning it here would be provably
    # dead code behind a strictly stronger control. The enum runs before the scan on both
    # message routes so the same bad input gets the same actionable 400 naming the valid
    # values, rather than one route saying 400 and the other a generic guardrails 403.
    # P18-4: enforce the room's mode, the room analogue of the DM check. The relay
    # reads no plaintext and holds no key — it inspects the envelope's structure and
    # the *set of agent_ids* the content key was wrapped to.
    from emporia import crypto_box
    if room.encrypted:
        # P19 (H5): the DM route (dm_send, below) has this exact guard for the exact
        # same reason — the room AAD binds msg_type, and this handler *resolves*
        # msg_type two lines above (`req.msg_type or DEFAULT_MSG_TYPE`). A client
        # that omits the field seals under "" while the row stores "chat"; every
        # reader then computes different AAD and the message is permanently
        # unreadable, accepted with a 200. P18-4 shipped the room's coverage check
        # without this one — the same "X or DEFAULT" resolution-shape defect class
        # (P12-3, P13-1, P15-1, P17-1) recurring in the very code that closed the
        # DM instance of it.
        if not req.msg_type:
            raise HTTPException(
                400,
                "msg_type must be sent explicitly on an end-to-end encrypted room: "
                "it is bound into the message's AAD, and the relay's own default "
                f"resolution (to '{DEFAULT_MSG_TYPE}') would not match what you "
                "sealed under, making the message permanently unreadable",
            )
        try:
            # Coverage is checked against room.members (structural membership), the
            # same set _room_member_encryption_keys() hands clients — see there for
            # why a lapsed paid member stays in it.
            crypto_box.validate_envelope_for(req.content, set(room.members))
        except crypto_box.MissingRecipients as e:
            # 409, not 400: membership churns, so a joiner arriving between the
            # client's key-fetch and its send is a *race*, not a malformed request.
            # Naming the missing members makes it recoverable — re-fetch the
            # bundle, re-seal, retry — instead of silently producing a message
            # they can never read. P19: this used to be a substring match on
            # crypto_box's error text ("no wrapped key for" in str(e)) — a
            # dedicated exception type means rewording that message can no longer
            # silently turn every membership race into a terminal 400.
            # P21-3 (B4): structured `code` -- this room route's 409 is otherwise
            # ambiguous with the duplicate-nonce 409 below, and clients used to
            # tell them apart the same substring-matching way, one exception type
            # later. See wire.ERR_MISSING_RECIPIENTS.
            raise HTTPException(409, {
                "code": ERR_MISSING_RECIPIENTS,
                "message": f"this room is end-to-end encrypted — {e}",
            })
        except ValueError as e:
            raise HTTPException(400, f"this room is end-to-end encrypted — {e}")
    elif crypto_box.looks_like_envelope(req.content):
        raise HTTPException(
            400,
            "this room is not encrypted, so an encrypted envelope cannot be stored "
            "in it — create the room with encrypted=true instead",
        )

    if not req.signature:
        raise HTTPException(401, "Signature required")
    # P19-5 (H4): the DM route (dm_send, above) has this exact guard for the exact
    # same reason — without a nonce bound into the signature, an identical message
    # replayed to this room verifies and stores again indistinguishably from the
    # original. Checked here rather than earlier so a missing *signature* still
    # answers 401 first, matching the DM route's ordering.
    if not req.nonce:
        raise HTTPException(
            400,
            "nonce is required — it binds this message's signature (and, on an "
            "encrypted room, its ciphertext) to this message, so a stored row "
            "cannot be replayed as another. Generate one with "
            "wire.new_message_nonce().",
        )
    # P20-5 (A9): moved ahead of the guardrails scan below — a cheap regex check
    # shouldn't run after the expensive (paid NIM) scan P15 already hoisted checks
    # like this one to avoid wasting.
    if not _NONCE_RE.fullmatch(req.nonce):
        raise HTTPException(
            400, "nonce must be 32-64 hex characters (see wire.new_message_nonce())",
        )

    # N3-8 (R7): bound to room_id + msg_type, not just content — a content-only
    # signature was valid in *any* room the caller belongs to, forever (replayable
    # across rooms and across msg_type, e.g. a signed "chat" replayed as "accept").
    # P21-2 (B3): moved ahead of the guardrails scan below — Ed25519 verification is
    # microseconds; the scan is a paid NIM call. A9 hoisted the cheap nonce-shape
    # check over the scan and stopped one statement short of this one, so a
    # registered member sending a garbage signature still burned scan budget on
    # every request until now.
    signed_envelope = signed_room_message_payload(room_id, req.content, effective_msg_type, req.nonce)
    if not AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid message signature")

    if not room.encrypted:
        try:
            await _assert_payload_safe_counted({"content": req.content})
        except PermissionError as e:
            raise HTTPException(403, str(e))

    # Negotiation broker: route counter_offer/accept/reject in 2-member private rooms
    broker_response: dict[str, Any] | None = None
    if effective_msg_type in ("counter_offer", "accept", "reject") and room.is_negotiation_room():
        # P19-6 (M8): the broker used to run unconditionally here — on an
        # encrypted room, req.content is the *envelope*, not the offer: it's a
        # JSON dict (crypto_box.seal()'s output serialized), so
        # `req.content.strip().startswith("{")` is true and json.loads()
        # parses it cleanly, handing process_offer() the envelope's structural
        # fields (v, alg, epk, ct, ...) as if they were an offer. Worse than a
        # no-op: on ACCEPT/COUNTER it posted `broker_response` as a *plaintext*
        # system row via rooms_db.post_message() below, bypassing
        # validate_envelope_for() entirely — a guaranteed plaintext write into
        # a room whose entire point is that the relay never holds plaintext.
        # The relay cannot decrypt, so it cannot broker; refuse cleanly instead.
        if room.encrypted:
            raise HTTPException(
                400,
                "the negotiation broker is unavailable in an end-to-end encrypted "
                "room — it would need to read the offer's plaintext to apply the "
                "midpoint algorithm, which is exactly what this room's encryption "
                "denies the relay. Negotiate out of band, or use an unencrypted room.",
            )
        from emporia.negotiation import process_offer
        try:
            offer_payload = json.loads(req.content) if req.content.strip().startswith("{") else {"text": req.content}
            constraints = req.negotiation_constraints or {}
            decision, response = process_offer(caller, offer_payload, constraints)
            broker_response = {"decision": decision, "response": response}
        except Exception:
            pass  # non-JSON content — treat as plain chat

    try:
        msg = rooms_db.post_message(
            room_id=room_id,
            sender_id=caller,
            content=req.content,
            msg_type=effective_msg_type,
            parent_message_id=req.parent_message_id,
            signature=req.signature,
            nonce=req.nonce,
            metadata={"broker": broker_response} if broker_response else {},
            db_path=DATABASE_PATH,
        )
    except sqlite3.IntegrityError as e:
        # P19-5 (H4): idx_room_messages_nonce's UNIQUE(room_id, nonce) is the
        # database-level half of the replay defense, mirroring the DM fix in
        # dm_send above — same reasoning, same 409.
        raise HTTPException(409, {
            "code": ERR_DUPLICATE_NONCE,
            "message": "duplicate nonce — this message has already been posted to this room",
        }) from e

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


def _room_member_encryption_keys(room: Any) -> dict[str, dict[str, str]]:
    """`{agent_id: {encryption_public_key, signature, public_key_hex}}` for every
    member of `room`, in one query per member.

    Keyed on `room.members` — *structural* membership — rather than
    `rooms_db.is_member()`, which additionally requires an active entitlement on a
    paid room. A member whose access lapsed and is later restored must not find a
    hole in their history, and excluding them here would create exactly that. They
    still cannot *fetch* messages while lapsed, because the relay gates reads — so
    this is an access-control question the relay already answers elsewhere, not a
    confidentiality hole opened here.
    """
    # P19-6 (M9): one query, one connection, one DB_LOCK acquisition for the whole
    # member set — this used to be get_encryption_key() (a connection of its own)
    # plus a second connection for public_key_hex, *per member*, against a global
    # write lock. A large room's every send paid 2N connections and 2N lock
    # acquisitions for what a single `WHERE agent_id IN (...)` answers at once.
    if not room.members:
        return {}
    placeholders = ",".join("?" * len(room.members))
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT agent_id, encryption_public_key, encryption_key_signature, "
            f"public_key_hex FROM authorized_agents WHERE agent_id IN ({placeholders})",
            room.members,
        ).fetchall()
    found = {
        row["agent_id"]: {
            "encryption_public_key": row["encryption_public_key"] or "",
            "signature": row["encryption_key_signature"] or "",
            "public_key_hex": row["public_key_hex"] or "",
        }
        for row in rows
    }
    return {
        member: found.get(member, {"encryption_public_key": "", "signature": "", "public_key_hex": ""})
        for member in room.members
    }


@app.get("/rooms/{room_id}/encryption-keys")
async def get_room_encryption_keys(room_id: str, request: Request):
    """Every member's encryption key in one round trip — what a client needs to
    seal a message to an encrypted room.

    Exists as a bundle rather than N calls to `/agents/{id}/encryption-key` for two
    reasons: N requests to send one message is a poor trade, and more importantly
    the member set could drift between the first fetch and the last, producing a
    message that silently excludes someone. One snapshot, one answer.

    Members only — the key material is public, but the *membership list* is not,
    and this returns both.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if not rooms_db.is_member(room_id, caller, db_path=DATABASE_PATH):
        raise HTTPException(403, "Not a member of this room")
    return {"room_id": room_id, "encrypted": room.encrypted,
            "keys": _room_member_encryption_keys(room)}


@app.get("/rooms/{room_id}/messages")
async def get_room_messages(
    room_id: str,
    request: Request,
    limit: int = 50,
    before: str | None = None,
    before_seq: int | None = None,
):
    viewer_id = _dashboard_agent_id(request)
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    # P31-3: `before_seq` completes the pagination cursor — see get_messages().
    msgs = rooms_db.get_messages(
        room_id, limit=_clamp_limit(limit), before=before,
        before_seq=before_seq, db_path=DATABASE_PATH,
    )
    return {"messages": [m.to_dict() for m in msgs], "count": len(msgs)}


@app.get("/rooms/{room_id}/chain")
async def get_room_chain(room_id: str, request: Request):
    """Chain-integrity verification for a room — the fix direction SECURITY.md
    has recorded since P19: "the relay signs each chain head with its
    federation keypair, a route serves it, and clients recompute and verify on
    read." Mirrors GET /sessions/{id}/audit (session_audit.verify_chain(),
    signed the same way GET /gaming/v1/federate/listings signs a batch —
    federation_signable_bytes() already invites a third `key`, see its
    docstring) — same gate as GET /rooms/{id}/messages (private rooms require
    membership; public rooms are open to any connection).

    Deliberately returns hashes, not message content — the caller already has
    every message's own `content`/`chain_hash` from GET /rooms/{id}/messages;
    this route's only job is the piece that read can't provide on its own: an
    attestation, signed with a key the caller doesn't hold, of what the
    current chain head actually is.

    **What the signature does and does not prove.** It proves the response
    came from this relay's own key, unaltered in transit — a third party
    (network MITM, a compromised CDN in front of the relay) cannot forge a
    different head. It does **not** prove the relay itself is honest: an
    operator with DB write access can rewrite a message's content, recompute
    a self-consistent chain from that point forward (verify_chain() would
    report `verified: true` for the tampered chain, since recomputation-and-
    compare only catches inconsistency, not a rewrite that stays internally
    consistent), and sign the new head with the same key — the signature
    changes nothing about that, since the relay holds it. Catching *that*
    is what client-side head-pinning is for (agent_sdk.py's/mcp_server.py's
    verify_room_chain(), which persist the last head they saw per room and
    refuse a chain that doesn't extend it — the same TOFU shape
    _FEDERATION_PEER_PUBKEYS already uses for peer relays). This route is the
    signed-attestation half of that; without it, there would be nothing for a
    client to pin at all.

    **What the cap changes, and what it doesn't** (P44). The recompute is bounded
    to the newest `CHAIN_VERIFY_LIMIT` messages, and `chain_verified_scope` says
    which happened — `"genesis"` when the walk reached the room's first message,
    `"recent_window"` when it stopped short. `chain_head` and `message_count` are
    unaffected: both are indexed lookups, not walk products, so the *signed*
    payload is byte-identical at any cap and the five independent verifiers that
    reconstruct those bytes need no change. What a window gives up is the relay's
    own opinion about old messages — which the paragraph above already says is
    not the guarantee. A client that wants more asks for more: the SDK's and MCP
    server's `verify_room_chain()` page backward through
    GET /rooms/{id}/messages and recompute the history themselves, against a head
    they pinned earlier, which is the check that actually catches an operator.
    """
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        raise HTTPException(404, "Room not found")
    viewer_id = _dashboard_agent_id(request)
    if room.room_type == "private" and not rooms_db.is_member(room_id, viewer_id or "", db_path=DATABASE_PATH):
        raise HTTPException(403, "Private room — not a member")
    result = await run_in_threadpool(
        rooms_db.verify_chain, room_id, db_path=DATABASE_PATH, limit=CHAIN_VERIFY_LIMIT,
    )
    signature = sign_raw_relay(federation_signable_bytes(
        RELAY_BASE_URL, "room_chain",
        [{
            "room_id": room_id,
            "chain_head": result.head or "",
            "message_count": result.message_count,
        }],
    ))
    return {
        "room_id": room_id,
        "verified": result.ok,
        "message": result.message,
        "chain_head": result.head,
        "message_count": result.message_count,
        "verified_message_count": result.verified_count,
        "chain_verified_scope": result.scope,
        "origin_relay": RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
    }


@app.websocket("/ws/rooms/{room_id}")
async def ws_room(ws: WebSocket, room_id: str):
    """Real-time room channel. Sends last 20 messages on connect. Private rooms require
    the caller to be an authenticated member (see _ws_agent_id()) — public rooms are open
    to any connection, matching GET /rooms/{id}/messages."""
    room = rooms_db.get_room(room_id, db_path=DATABASE_PATH)
    if not room:
        logger.warning("ws_rejected", extra={"channel": "room", "room_id": room_id, "reason": "unknown_room"})
        await ws.close(code=1008)
        return
    caller = _ws_agent_id(ws)
    if room.room_type == "private" and not rooms_db.is_member(room_id, caller or "", db_path=DATABASE_PATH):
        logger.warning("ws_rejected", extra={"channel": "room", "room_id": room_id, "reason": "not_a_member"})
        await ws.close(code=1008)
        return
    await ws.accept()
    ws_id = uuid.uuid4().hex[:12]
    logger.info("ws_connected", extra={"ws_id": ws_id, "channel": "room", "room_id": room_id})
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
        logger.info("ws_disconnected", extra={"ws_id": ws_id, "channel": "room", "room_id": room_id})
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
    # P21-5 (B5): name/description bounded automatically — see
    # _require_payload_content_within_limit.
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
        await _finish_interrupted_access_purchase(
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
    # of re-entering payout work. See join_room's identical restructuring, and the
    # P50 correction there for why "payout_winner is non-idempotent" has been stale
    # since P1a-3 and what the short-circuit was actually costing.
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
    # P21-5 (B5): title/flair/content bounded automatically — see
    # _require_payload_content_within_limit.
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
    # P21-5 (B5): content bounded automatically — see
    # _require_payload_content_within_limit.
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
    # P16-5: tri-state, and the third state is the common case. `True` = this thread
    # must be encrypted (409 if an existing one isn't — the dangerous direction, never
    # silently downgraded). `False` = explicitly asking for plaintext (409 if the
    # existing thread is encrypted, so the caller gets a clear error rather than a
    # confusing 400 on their first send). `None`/omitted = "whatever this pair already
    # has, plaintext if new" — what a client that just wants to reach an agent means,
    # and the only value that lets one round trip both find an existing thread and
    # learn its mode. Started as a plain `bool` in P16-4; building the clients showed
    # that forced every send path into a thread-list scan just to avoid a spurious 409.
    encrypted: bool | None = None


# P19 (M3): what an honest nonce looks like — `wire.new_message_nonce()` is
# `secrets.token_hex(16)` (32 hex chars); the upper bound leaves room for a future
# longer nonce without immediately needing a second constant.
_NONCE_RE = re.compile(r"[0-9a-fA-F]{32,64}")


class DMSendRequest(BaseModel):
    content: str
    msg_type: str = ""
    signature: str | None = None
    # P18-2: sender-chosen, inside the signature and the AAD. Empty is rejected
    # rather than defaulted — a silent fallback would leave replay open to any
    # client that just omitted the field.
    nonce: str = ""


def _require_encryption_keys_published(agent_ids: list[str]) -> None:
    """P16: every participant must have published an X25519 key before a thread can
    be marked encrypted.

    An encrypted thread with an unreachable participant is worse than no flag at
    all — every send would fail `validate_envelope_for()` with an error about
    wrapped keys, nowhere near the actual cause. Fails 409 (a state conflict, not a
    malformed request) naming the agent who needs to act.
    """
    missing = [a for a in agent_ids if not AGENT_REGISTRY.get_encryption_key(a)[0]]
    if missing:
        raise HTTPException(
            409,
            f"cannot encrypt this thread: {', '.join(missing)} has not published an "
            f"encryption key (POST /agents/{{agent_id}}/encryption-key)",
        )


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
    if req.encrypted:
        _require_encryption_keys_published([a, b])
    now = datetime.now(UTC).isoformat()
    thread_id = f"dm_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        existing = conn.execute(
            "SELECT thread_id, encrypted FROM dm_threads WHERE agent_a=? AND agent_b=?", (a, b)
        ).fetchone()
        if existing:
            # P16: this route is idempotent on the *pair*, so without this check
            # asking for an encrypted thread when a plaintext one already exists
            # would hand back the plaintext thread — and a client that didn't
            # re-read the flag would post prose believing it was encrypted. That
            # silent downgrade is the worst failure mode this feature has, so the
            # mismatch is a 409 rather than a quietly different answer. The mirror
            # case conflicts too: a caller expecting plaintext must not be handed
            # an encrypted thread and post prose into it. `POST /dm/{id}/encrypt`
            # is how an existing thread changes mode, deliberately and one-way.
            if req.encrypted is not None and bool(existing["encrypted"]) != req.encrypted:
                state = "encrypted" if existing["encrypted"] else "not encrypted"
                raise HTTPException(
                    409,
                    f"a DM thread with '{req.to_agent}' already exists and is {state}. "
                    f"Use POST /dm/{existing['thread_id']}/encrypt to enable encryption "
                    f"on it (encryption cannot be turned off).",
                )
            return {
                "thread_id": existing["thread_id"],
                "created": False,
                "encrypted": bool(existing["encrypted"]),
            }
        conn.execute(
            "INSERT INTO dm_threads "
            "(thread_id, agent_a, agent_b, created_at, last_message_at, encrypted) "
            "VALUES (?,?,?,?,?,?)",
            (thread_id, a, b, now, now, int(bool(req.encrypted))),
        )
    return {
        "thread_id": thread_id, "agent_a": a, "agent_b": b,
        "created": True, "encrypted": bool(req.encrypted),
    }


def _dm_last_chain_hash(conn: Any, thread_id: str) -> str | None:
    """The chain head this thread's next message links onto, or None for a
    thread with no chained messages yet.

    Ordering matches `_verify_dm_chain()`'s walk exactly, `rowid` tiebreak
    included — rooms.py's `_last_chain_hash()` carries the same note (P21-7):
    two inserts landing on the same microsecond would otherwise let the writer
    chain onto a different "last" row than the verifier recomputes, breaking
    every message after the tie.
    """
    row = conn.execute(
        "SELECT chain_hash FROM dm_messages WHERE thread_id = ? AND chain_hash IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return row["chain_hash"] if row else None


class DmChainVerification(NamedTuple):
    """`_verify_dm_chain()`'s result. Mirrors `rooms.ChainVerification` field for
    field, plus `unchained_prefix` — the DM-only region P28-4 refuses to vouch
    for. `message_count` counts *chained* messages, as it always has: the
    unchained prefix is reported separately rather than folded into a total that
    would imply coverage.
    """

    ok: bool
    message: str
    head: str | None
    message_count: int
    unchained_prefix: int
    verified_count: int
    scope: str


def _verify_dm_chain(thread_id: str, limit: int | None = None) -> DmChainVerification:
    """Recompute a DM thread's hash chain and compare against each stored
    `chain_hash`.

    The DM counterpart of `rooms.verify_chain()`, and it reuses that module's
    `compute_chain_hash()` rather than restating the frame. The one real
    difference is the boundary: rooms have been chained since P18, while DM rows
    written before P28-4 have `chain_hash` NULL. Those are skipped and *counted*,
    never treated as verified — an operator can delete the entire unchained
    prefix and the chained suffix still verifies perfectly, so a bare
    `verified: true` would overclaim precisely where the guarantee stops.

    `limit` bounds the walk exactly as `rooms.verify_chain()`'s does, and for the
    same reason (P44) — kept in step with it deliberately, since P8's lesson here
    was that these two verifiers drift when only one of them gets fixed. `scope`
    carries all three outcomes: `"genesis"` (walked every message in the thread),
    `"chained_start"` (walked every *chained* message, but an unchained prefix
    sits in front of it), `"recent_window"` (stopped at the cap).
    """
    with DB_LOCK, get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM dm_messages WHERE thread_id = ?", (thread_id,),
        ).fetchone()["n"]
        chained_total = conn.execute(
            "SELECT COUNT(*) AS n FROM dm_messages WHERE thread_id = ? AND chain_hash IS NOT NULL",
            (thread_id,),
        ).fetchone()["n"]
        unchained_prefix = total - chained_total
        rows = []
        if chained_total:
            if limit is None or chained_total <= limit:
                rows = conn.execute(
                    "SELECT sender_id, content, created_at, chain_hash FROM dm_messages "
                    "WHERE thread_id = ? AND chain_hash IS NOT NULL "
                    "ORDER BY created_at ASC, rowid ASC",
                    (thread_id,),
                ).fetchall()
            else:
                # Newest-first + LIMIT, then reversed — the only shape
                # idx_dm_messages_thread can serve without reading the thread.
                rows = conn.execute(
                    "SELECT sender_id, content, created_at, chain_hash FROM dm_messages "
                    "WHERE thread_id = ? AND chain_hash IS NOT NULL "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (thread_id, limit + 1),
                ).fetchall()
                rows.reverse()

    if not rows:
        return DmChainVerification(
            True, "no chained messages — nothing to verify", None, 0,
            unchained_prefix, 0, "chained_start" if unchained_prefix else "genesis",
        )

    head = rows[-1]["chain_hash"]
    at_start = limit is None or chained_total <= limit
    if at_start:
        prev_hash: str | None = None
        walk, first_index = rows, 0
        scope = "chained_start" if unchained_prefix else "genesis"
    else:
        prev_hash = rows[0]["chain_hash"]
        walk, first_index = rows[1:], chained_total - len(rows) + 1
        scope = "recent_window"

    for offset, row in enumerate(walk):
        expected = rooms_db.compute_chain_hash(
            prev_hash, row["sender_id"], row["content"], row["created_at"],
        )
        if row["chain_hash"] != expected:
            return DmChainVerification(
                False, f"hash mismatch at message {first_index + offset}",
                None, chained_total, unchained_prefix, offset, scope,
            )
        prev_hash = row["chain_hash"]

    verified = len(walk)
    detail = "chain valid" if scope != "recent_window" else "recent window valid"
    return DmChainVerification(
        True, f"{detail} ({verified} of {chained_total} messages)",
        head, chained_total, unchained_prefix, verified, scope,
    )


def _dm_thread_or_404(thread_id: str, caller: str) -> Any:
    """Load a DM thread and assert `caller` is one of its two participants."""
    with get_db() as conn:
        thread = conn.execute(
            "SELECT * FROM dm_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
    if not thread:
        raise HTTPException(404, "DM thread not found")
    if caller not in (thread["agent_a"], thread["agent_b"]):
        raise HTTPException(403, "Not a participant in this thread")
    return thread


@app.get("/dm/{thread_id}/chain")
async def get_dm_chain(thread_id: str, request: Request):
    """Chain-integrity verification for a DM thread — the DM half of
    GET /rooms/{id}/chain, which P24 built and explicitly deferred this from.

    Same gate as GET /dm/{id}/messages: participants only. A chain head plus a
    message count is metadata about a private conversation (who is talking, how
    much, and when it last changed), so the attestation route must not be more
    open than the read route it attests to.

    Like the room route, this returns hashes rather than content — the caller
    already has every message from GET /dm/{id}/messages, and the only thing a
    read cannot provide for itself is an attestation, signed with a key the
    caller does not hold, of what the head actually is.

    **What it proves, and what it does not.** Identical limits to the room
    route, and worth restating rather than cross-referencing away: the signature
    proves the response came from this relay unaltered, not that the relay is
    honest. An operator with DB write access can rewrite a message, recompute a
    self-consistent chain forward from it, and sign the new head — recomputation
    catches inconsistency, not an internally consistent rewrite. Client-side
    head pinning is what catches that; this route is the half a client pins.

    `unchained_prefix` is the DM-specific caveat: messages written before P28-4
    have no `chain_hash` and are not covered by `chain_head`. A client treating
    a nonzero value as "verified" would be trusting exactly the region the relay
    is declining to vouch for.

    `chain_verified_scope` and `verified_message_count` carry P44's bounded walk,
    with the same guarantees the room route's docstring spells out: the signed
    payload is unchanged by the cap, and a client wanting the full history
    recomputes it itself against a pinned head.
    """
    _dm_thread_or_404(thread_id, _dashboard_agent_id(request) or "")
    result = await run_in_threadpool(_verify_dm_chain, thread_id, CHAIN_VERIFY_LIMIT)
    signature = sign_raw_relay(federation_signable_bytes(
        RELAY_BASE_URL, "dm_chain",
        [{
            "thread_id": thread_id,
            "chain_head": result.head or "",
            "message_count": result.message_count,
        }],
    ))
    return {
        "thread_id": thread_id,
        "verified": result.ok,
        "message": result.message,
        "chain_head": result.head,
        "message_count": result.message_count,
        "unchained_prefix": result.unchained_prefix,
        "verified_message_count": result.verified_count,
        "chain_verified_scope": result.scope,
        "origin_relay": RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
    }


@app.get("/dm/{thread_id}")
async def dm_thread_info(thread_id: str, request: Request):
    """Thread metadata — participants and whether it is encrypted.

    Exists so a client holding only a `thread_id` (from an inbox event, a stored
    reference) can learn the mode before it sends or displays anything, without
    fetching the message history to infer it.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    thread = _dm_thread_or_404(thread_id, caller)
    return {
        "thread_id": thread["thread_id"],
        "agent_a": thread["agent_a"],
        "agent_b": thread["agent_b"],
        "encrypted": bool(thread["encrypted"]),
        "created_at": thread["created_at"],
        "last_message_at": thread["last_message_at"],
    }


@app.post("/dm/{thread_id}/encrypt")
async def dm_encrypt(thread_id: str, request: Request):
    """Turn on end-to-end encryption for an existing thread. Idempotent.

    **One-way, on purpose.** Either participant may enable it unilaterally — there
    is nothing to negotiate, since the peer's client handles both modes and the
    flag only ever adds protection. There is deliberately no route that clears it:
    a downgrade endpoint is a force-plaintext attack, and one participant being
    able to strip a thread's encryption is exactly what an attacker with a stolen
    session would reach for. The only way back to plaintext is a different thread.

    Messages already stored stay as they are — an upgrade is not a purge. Old
    plaintext remains readable; everything after this point must be an envelope.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    thread = _dm_thread_or_404(thread_id, caller)
    _require_encryption_keys_published([thread["agent_a"], thread["agent_b"]])
    with DB_LOCK, get_db() as conn:
        conn.execute("UPDATE dm_threads SET encrypted=1 WHERE thread_id=?", (thread_id,))
    return {"thread_id": thread_id, "encrypted": True}


@app.post("/dm/{thread_id}/send")
async def dm_send(thread_id: str, req: DMSendRequest, request: Request):
    """Send a message in a DM thread. Pushes inbox event to the recipient."""
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    # P15: thread lookup + participant check hoisted above the guardrails scan. They
    # used to sit in the write block below, so any registered agent could force a scan —
    # including the optional paid NIM layer — against a thread_id they have nothing to
    # do with. Bounded by _enforce_agent_rate_limit() above, so this is "stop doing
    # expensive work for a request that's already rejected", not a live DoS fix. Moving
    # only the *signature* check up (considered at P14) would not have helped: signing
    # is free for the attacker, so it discriminates nothing. Read-only, so it takes its
    # own connection rather than holding DB_LOCK across the scan; the resulting TOCTOU
    # window is immaterial (threads are never deleted).
    thread = _dm_thread_or_404(thread_id, caller)
    # Local import + resolved-once pattern matches P12-3's room-message fix.
    from emporia.wire import ERR_DUPLICATE_NONCE, DEFAULT_MSG_TYPE, signed_dm_thread_payload
    effective_msg_type = req.msg_type or DEFAULT_MSG_TYPE
    _require_valid_msg_type(effective_msg_type)
    _require_content_within_limit(req.content)
    # P16: enforce the thread's mode. The relay reads no plaintext and holds no key
    # here — `validate_envelope_for()` inspects the envelope's structure and the set
    # of agent_ids the content key was wrapped to, nothing more. It catches the two
    # failures that actually happen: prose posted to an encrypted thread (a silent
    # plaintext leak into a store the sender believes is opaque), and a sender who
    # wrapped only for themselves — a message the peer can never read, which without
    # this check would be stored, 200'd, and discovered as permanent garbage. The
    # mode comes from the thread row, never from the request, so there is nothing
    # here for a client to lie about.
    from emporia import crypto_box
    if thread["encrypted"]:
        # P17-1: the AAD binds msg_type, and this handler *resolves* msg_type just
        # above (`req.msg_type or DEFAULT_MSG_TYPE`). So a client that omits the
        # field seals under "" while this row stores "chat" — the recipient then
        # computes different AAD and the message is permanently unreadable, having
        # been accepted with a 200. Verified before the fix: row stored
        # msg_type='chat', open_sealed() raised for the recipient.
        #
        # Refusing is the only sound option: the relay cannot know what value the
        # client sealed under, so it can't repair the mismatch. Dropping msg_type
        # from the AAD was considered and rejected — it would throw away
        # relabelling protection (which rooms will reuse when they adopt
        # crypto_box) to fix something a clear 400 fixes outright. Same class as
        # validate_envelope_for() below, and deliberately sitting next to it: both
        # turn "accepted but nobody can ever read it" into an actionable error.
        # Plaintext threads keep the omit path — no AAD, nothing to disagree about.
        if not req.msg_type:
            raise HTTPException(
                400,
                "msg_type must be sent explicitly on an end-to-end encrypted thread: "
                "it is bound into the message's AAD, and the relay's own default "
                f"resolution (to '{DEFAULT_MSG_TYPE}') would not match what you "
                "sealed under, making the message permanently unreadable",
            )
        try:
            crypto_box.validate_envelope_for(
                req.content, {thread["agent_a"], thread["agent_b"]},
            )
        except ValueError as e:
            # P20-5: audited whether this should split MissingRecipients into a
            # 409 the way the room route does (A10) and concluded it shouldn't —
            # a DM thread's recipient set is exactly {agent_a, agent_b}, fixed at
            # creation, with no join path and so no membership race to recover
            # from. A missing-recipient failure here is always the same client
            # bug the room route calls out as the *other* case: wrapped only for
            # self. 400 is correct and test_encrypted_message_must_be_readable_by_
            # every_participant pins it.
            raise HTTPException(400, f"this DM thread is end-to-end encrypted — {e}")
    elif crypto_box.looks_like_envelope(req.content):
        raise HTTPException(
            400,
            "this DM thread is not encrypted, so an encrypted envelope cannot be "
            f"stored in it — enable encryption first via POST /dm/{thread_id}/encrypt",
        )

    if not req.signature:
        raise HTTPException(401, "Signature required")
    # P18-2: no silent fallback — an absent nonce means a message bound to nothing
    # unique, which is exactly the replay hole this closes. Checked here rather than
    # earlier so a missing *signature* still answers 401 first: that is the more
    # fundamental failure, and its status code predates this field.
    if not req.nonce:
        raise HTTPException(
            400,
            "nonce is required — it binds this message's signature and ciphertext to "
            "this message, so a stored row cannot be replayed as another. Generate "
            "one with wire.new_message_nonce().",
        )
    # P19 (M3): "non-empty" was the entire validation. `wire.new_message_nonce()`
    # produces 32 hex chars, but nothing enforced clients use it — a nonce sat
    # outside `_require_content_within_limit`'s reach, so an ~800KB nonce was
    # accepted, stored, *indexed* in idx_dm_messages_nonce, and echoed back on
    # every read. Bounded to what an honest nonce actually looks like, generously
    # (32-64 hex chars covers 128-256 bits of a future longer nonce without
    # forcing a re-tune).
    # P20-5 (A9): moved ahead of the guardrails scan below, same reasoning as the
    # room route — a cheap regex shouldn't run after the expensive (paid NIM) scan.
    if not _NONCE_RE.fullmatch(req.nonce):
        raise HTTPException(
            400, "nonce must be 32-64 hex characters (see wire.new_message_nonce())",
        )

    # Same N3-8/R7 shape as room messages: bound to thread_id + msg_type, not just
    # content — a content-only signature was valid for delivery into any thread this
    # agent is a participant of, forever (replayable across threads and across
    # msg_type).
    # P21-2 (B3): moved ahead of the guardrails scan below — Ed25519 verification is
    # microseconds; the scan is a paid NIM call. A9 hoisted the cheap nonce-shape
    # check over the scan and stopped one statement short of this one, so a
    # registered participant sending a garbage signature still burned scan budget
    # on every plaintext-thread request until now.
    signed_envelope = signed_dm_thread_payload(
        thread_id, req.content, effective_msg_type, req.nonce,
    )
    if not AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid message signature")

    # P15: msg_type dropped from this payload for the same reason as the room route
    # above — the closed enum checked immediately above supersedes scanning it.
    # P16: and the content scan is skipped entirely on an encrypted thread, the same
    # carve-out (and the same reason) encrypted rooms already have — ciphertext is
    # base64 and *will* false-positive on injection patterns. This is the real price
    # of E2E, stated plainly in SECURITY.md rather than implied away: the relay
    # cannot scan what it cannot read. msg_type is unaffected — it sits outside the
    # envelope and is already restricted to a closed six-value enum.
    if not thread["encrypted"]:
        try:
            await _assert_payload_safe_counted({"content": req.content})
        except PermissionError as e:
            raise HTTPException(403, str(e))
    now = datetime.now(UTC).isoformat()
    message_id = f"dmm_{uuid.uuid4().hex[:12]}"
    with DB_LOCK, get_db() as conn:
        try:
            # P28-4: computed inside the same DB_LOCK as the INSERT, so two
            # concurrent sends can't both read the same predecessor and chain
            # onto it. The frame is rooms.compute_chain_hash() itself rather
            # than a copy — P21-7 made it public for exactly this reason.
            chain_hash = rooms_db.compute_chain_hash(
                _dm_last_chain_hash(conn, thread_id), caller, req.content, now,
            )
            conn.execute(
                "INSERT INTO dm_messages (message_id, thread_id, sender_id, content, "
                "msg_type, created_at, signature, nonce, chain_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (message_id, thread_id, caller, req.content, effective_msg_type, now,
                 req.signature, req.nonce, chain_hash),
            )
        except sqlite3.IntegrityError as e:
            # P19 (H3): idx_dm_messages_nonce's UNIQUE(thread_id, nonce) is the
            # database-level half of the replay defense — the one that survives an
            # operator dropping the application check — but nothing caught the
            # constraint firing, and `relay_server.py` registers no
            # `@app.exception_handler`. So the primary case this feature exists for
            # (a captured request replayed verbatim) surfaced as an unhandled 500
            # with a traceback, not the 409 the client-facing contract implies.
            # Reachable by an honest client too: the nonce is free-form, so a
            # counter-based generator 500s on its very second message.
            # P21-3 (B4): same structured code as the room route's duplicate-nonce
            # 409 (wire.ERR_DUPLICATE_NONCE) -- no client branches on this one
            # today (send_dm doesn't retry on 409 at all), but there's no reason
            # to leave this the one hand-copied-string holdout of the shape B4
            # just closed everywhere else.
            raise HTTPException(409, {
                "code": ERR_DUPLICATE_NONCE,
                "message": "a message with this nonce already exists in this thread — "
                "this looks like a replayed request, not a new message",
            }) from e
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
        # P14: was req.msg_type, which is "" on the omit path while the row above
        # stores effective_msg_type ("chat") — the same message reached its two
        # consumers (this inbox/WS event vs. GET /dm/{id}/messages) with different
        # values. The room route avoids this structurally by broadcasting the stored
        # message object; this one hand-builds its event, so both sites have to use
        # the resolved value explicitly.
        "msg_type": effective_msg_type,
        # P16: a client holding only this event has to know whether `content` is
        # ciphertext before it tries to display it. Read from the thread row, the
        # same source the mode check above used — not re-derived from the content,
        # which would be a second definition of the same fact.
        "encrypted": bool(thread["encrypted"]),
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
            # P16: on an encrypted thread this preview is the raw envelope, which
            # is exactly right — the relay has nothing else to give. The `encrypted`
            # flag beside it is how a client knows to decrypt rather than display.
            "last_content": r["last_content"],
            "last_sender": r["last_sender"],
            "encrypted": bool(r["encrypted"]),
        }
        for r in rows
    ]


@app.get("/dm/{thread_id}/messages")
async def dm_messages(
    thread_id: str, agent_id: str, request: Request, limit: int = 100,
    before: str | None = None, before_seq: int | None = None,
):
    """Fetch messages in a DM thread. Agent must be a participant.

    P31-2: `before` alone is not a total cursor. Two messages can share a
    `created_at` — this send path computes it *before* taking DB_LOCK, so two
    concurrent sends genuinely can land on one microsecond, which is the same
    case P21-7 added the chain writer's `rowid` tiebreak for. When such a pair
    straddles a page boundary, `created_at < ?` excludes the older one from
    every subsequent page: it can never be fetched at all, and a client walking
    the chain backward sees an untampered thread as unverifiable.

    `before_seq` (each message's `seq`, its rowid) makes the cursor total,
    ordered exactly the way `_verify_dm_chain()` and `_dm_last_chain_hash()`
    already order: `(created_at, rowid)`. Optional, so a caller that doesn't
    paginate is unaffected.
    """
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
        if before and before_seq is not None:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM dm_messages WHERE thread_id=? AND "
                "(created_at < ? OR (created_at = ? AND rowid < ?)) "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, before, before, before_seq, limit),
            ).fetchall()
        elif before:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM dm_messages WHERE thread_id=? AND created_at < ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM dm_messages WHERE thread_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, limit),
            ).fetchall()
    messages = [
        {
            "message_id": r["message_id"],
            "sender_id": r["sender_id"],
            "content": r["content"],
            "msg_type": r["msg_type"],
            "created_at": r["created_at"],
            # P17-2: served so the *client* can re-verify, not just the relay at
            # write time. NULL for pre-P17 rows, which clients surface as "unknown".
            "signature": r["signature"],
            # P18-2: served so the client can spot a duplicate itself — the UNIQUE
            # index stops the easy replay, but an operator who can write the DB can
            # also drop an index, and this is what keeps that visible.
            "nonce": r["nonce"],
            # P30-1: same reason as the two above, and the one that makes
            # independent chain verification possible at all — without it a
            # client has nothing to compare its own recomputation against, and
            # P28-4's chain could only ever be checked by the relay redoing its
            # own work, which is precisely the party the check exists to doubt.
            # NULL on pre-P28 rows: nothing was stored, so nothing is claimed.
            "chain_hash": r["chain_hash"],
            # P31-2: the second half of this thread's total ordering, so a
            # paginating client can build a cursor that can't skip a message
            # sharing a timestamp with the page boundary. See `before_seq`.
            "seq": r["seq"],
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
