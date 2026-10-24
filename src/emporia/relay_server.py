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


# P92: `python -m emporia.relay_server` runs this file as `__main__`, a second
# module beside the `emporia.relay_server` every router imports. The old guard
# at the bottom called main() *above* the router block P56 added, so that
# command served a relay whose extracted routes (agents, sessions, bounties,
# rooms, DMs, ...) all 404'd, with its own DB handles and registries. Here, before
# anything else runs, `__main__` does nothing but import the real module
# — which loads exactly as the `emporia-relay` entry point loads it — and run
# that. One copy ever exists, and it is the one the routers see.
if __name__ == "__main__":
    import sys as _sys

    from emporia.relay_server import main as _main

    _main()
    _sys.exit(0)

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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


# P55: the dotenv merge and the database path both moved to `emporia.config`,
# which every store now imports. They used to live here, and `rooms` /
# `entitlements` / `game_registry` each carried their own path default — two of
# which named a *different file in a different directory*. Those defaults agreed
# with this one only when `relay_server` happened to be imported first, because
# this import is what ran the merge. See config.py's docstring.
from emporia.config import database_path as _database_path  # noqa: E402

from emporia.engine.game_registry import GameRegistry
from emporia.engine import guardrails
from emporia.engine.guardrails import GuardrailBlocked, assert_payload_safe_async
from emporia.identity import (
    verify,
    verify_raw,
    get_relay_public_key_hex,
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
    SessionState,
    get_interaction_module,
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
import emporia.modules.blotto  # noqa: F401
import emporia.modules.chess  # noqa: F401
import emporia.modules.code_review  # noqa: F401
import emporia.modules.research  # noqa: F401
import emporia.modules.service  # noqa: F401
import emporia.modules.two_thirds  # noqa: F401
# P133: an operator's own games (EMPORIA_MODULES=pkg.module,...), loaded
# explicitly — a module that names winners decides who is paid.
from emporia.module_sdk import load_operator_modules as _load_operator_modules  # noqa: E402

_OPERATOR_MODULES = _load_operator_modules(os.environ.get("EMPORIA_MODULES", ""))

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
# P55: resolved through emporia.config, the package's only reader of
# EMPORIA_DB_PATH. Still captured once here — 111 call sites in the suite treat
# it as a Path — but it is now provably the same file every other store opens.
DATABASE_PATH = _database_path()

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
    # P62: asks `stripe_key_mode()` rather than matching a prefix here. The
    # prefix check missed `rk_live_` — a restricted *live* key, the
    # least-privilege shape Stripe recommends — so this guard fired for the less
    # safe key and not the more safe one, and a live relay started into exactly
    # the state it exists to prevent. Routing both callers through one
    # classifier is what stops that drifting apart again.
    from emporia.payments import stripe_key_mode

    secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    mode = stripe_key_mode()
    if mode == "live" and not secret:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is a live key but STRIPE_WEBHOOK_SECRET is unset — "
            "POST /webhooks/stripe cannot verify Stripe's account.updated events "
            "without it, so live-mode Connected Accounts could never be marked "
            "payable.\n"
            "\n"
            "Three ways forward, depending on what you are doing:\n"
            "  * Developing or testing — use a test key "
            "(STRIPE_SECRET_KEY=sk_test_... or rk_test_...). Nothing here needs a "
            "live one.\n"
            "  * Not using Stripe at all — unset STRIPE_SECRET_KEY. The relay "
            "starts, the Stripe rail is simply disabled, and rooms, Agoras, DMs "
            "and the x402 rail are unaffected.\n"
            "  * Actually running live — set STRIPE_WEBHOOK_SECRET from the "
            "Stripe Dashboard's webhook endpoint configuration.\n"
            "\n"
            "P62 note: this now recognises restricted keys (rk_live_) as live, "
            "which it did not before. If this relay used to start and no longer "
            "does, that is why — the guard was missing your key's shape, not "
            "newly disagreeing with it."
        )
    if mode == "test" and not secret:
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
      - X-Emporia-Authorization: Bearer <jwt>      → the same, when Authorization
                                                     carries a payment (P95)
      - anything else                              → None (unauthenticated)

    P95: a paying request has two things to say — who is paying and with what —
    and they used to share one header. `Authorization: Payment <spt>` displaced
    the identity bearer, so a remote agent paying with an SPT could not also
    prove who it was. Every route that must know the payer (posting a held
    bounty, opening a wager, funding a purchase) 401'd for any agent not
    co-located with the relay; MCP's `post_bounty` never sent a credential at
    all, so a held bounty had never been postable through it. The identity now
    has its own header when the payment needs that one.
    """
    if _is_header_auth_trusted(request):
        header_id = request.headers.get("x-emporia-agent-id")
        if header_id:
            return header_id
    for name in ("authorization", "x-emporia-authorization"):
        auth = request.headers.get(name, "")
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
        # P95: a payer who also sends X-Emporia-Authorization has proven who
        # they are, and is held to it like any other authenticated caller.
        if not request.headers.get("x-emporia-authorization"):
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
        # P61: before anything is created or migrated.
        _assert_schema_not_from_the_future(conn)
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

            CREATE TABLE IF NOT EXISTS escrow_holds (
                hold_hash TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                payer_agent_id TEXT NOT NULL,
                payer_address TEXT NOT NULL,
                receiver_agent_id TEXT NOT NULL,
                receiver_address TEXT NOT NULL,
                network TEXT NOT NULL,
                amount_atomic TEXT NOT NULL,
                payment_info_json TEXT NOT NULL,
                state TEXT NOT NULL,
                authorize_tx TEXT,
                capture_tx TEXT,
                void_tx TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bounty_entries (
                listing_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                nous_user_id TEXT NOT NULL,
                entered_at TEXT NOT NULL,
                PRIMARY KEY (listing_id, agent_id)
            );

            -- P72-4: the Sybil rule, enforced by the database rather than only
            -- by the check that precedes it. bounties.may_enter() counts one
            -- entry per Nous account, but a read-then-write cannot survive two
            -- concurrent entries from two personas of the same account -- both
            -- read an empty set, both pass, both insert. This UNIQUE index is
            -- what makes the rule true under concurrency; the Python check is
            -- what makes the refusal legible.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bounty_entries_one_per_account
                ON bounty_entries (listing_id, nous_user_id);

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
                created_at TEXT NOT NULL,
                federated_from TEXT
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
        _migrate_settlement_atomic_columns(conn)
        _migrate_listings_federated_from(conn)
        # escrow_holds (P99) is a new table, created above by CREATE TABLE IF
        # NOT EXISTS on any database, old or new, so it needs no migration.
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
        _stamp_schema_version(conn)


# P61: the schema's own version, stamped into the database header via
# `PRAGMA user_version`. Bump it whenever the schema changes — a test computes
# a fingerprint of every table and column and fails if the shape moved without
# this number moving, so it cannot quietly stop meaning anything.
SCHEMA_VERSION = 5


def _assert_schema_not_from_the_future(conn: Any) -> None:
    """Refuse a database written by a newer relay than this one (P61).

    `_assert_not_pre_p3_schema()` below already refuses a database that is too
    *old*. Nothing refused one that was too new: an older binary opening a newer
    file simply ran its own migrations over it and served, which is undefined
    rather than diagnosed. That is the ordinary shape of a rollback — deploy,
    hit a problem, roll back the binary, and the database stays migrated.

    Checked *before* the migrations run, because the damage a stale binary does
    is done by migrating a schema it does not understand, not by reading it.

    This protects forward only: a relay predating P61 has no gate to trip. That
    is inherent to adding a version late and is the reason to add it now rather
    than later.
    """
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found > SCHEMA_VERSION:
        raise RuntimeError(
            f"This database was written by a newer Emporia relay: it is at "
            f"schema version {found}, and this relay understands up to "
            f"{SCHEMA_VERSION}. Refusing to start rather than migrate a schema "
            f"this binary does not know — run the newer relay, or restore a "
            f"backup taken before the upgrade."
        )


def _stamp_schema_version(conn: Any) -> None:
    """Record the schema version, after every migration has run.

    Interpolated rather than bound: `PRAGMA user_version = ?` is not a
    parameterised statement in SQLite. `SCHEMA_VERSION` is an int constant in
    this module, so there is nothing here a caller can reach.
    """
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


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


def _migrate_settlement_atomic_columns(conn: Any) -> None:
    """Add `platform_fee_atomic`/`winner_payout_atomic` (P129): an escrow
    capture splits in USDC atomic units, so 2.5% of $1.00 is $0.025 — the
    cents columns could only say 2¢/98¢ while the contract paid 2.5¢/97.5¢.
    Decimal strings of atomic units; NULL on every other rail, where the cents
    columns are exact.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(settlements)").fetchall()}
    for col in ("platform_fee_atomic", "winner_payout_atomic"):
        if col not in cols:
            conn.execute(f"ALTER TABLE settlements ADD COLUMN {col} TEXT")


def _migrate_listings_federated_from(conn: Any) -> None:
    """Add `listings.federated_from` (P87): the peer a row was actually pulled
    from, as opposed to `origin_relay`, which is whatever that peer *said* the
    origin was. Ownership of a federated row is checked against this.

    NULL means "not pulled from a peer" for a local row, and "pulled before
    P87" for a federated one — `_pull_federated_listings` adopts the latter on
    its next refresh from a peer whose URL matches the row's origin.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "federated_from" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN federated_from TEXT")


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
    expected_destination: str | None = None,
) -> None:
    """`expected_destination` (P95): for a destination charge, the connected
    account the money must go to on capture. A hold that names no destination,
    or another one, would pay the platform or a stranger instead of the seller.

    `expected_payer_agent_id` (P1a-1, R1): a PaymentIntent used to be verified as
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
    if expected_destination is not None and payment.get("destination") != expected_destination:
        raise HTTPException(402, "PaymentIntent does not pay this seller")


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


def _record_x402_settlement(
    request: Request | None, settle_result: dict[str, Any], *, network: str, payer: str,
) -> None:
    """Hold the facilitator's settlement result for the `PAYMENT-RESPONSE` header (P93).

    x402 v2's HTTP transport returns the outcome of a settlement to the payer
    in `PAYMENT-RESPONSE`: base64 JSON `{success, transaction, network, payer}`,
    plus `errorReason` when it failed, on the 200 and on the 402 alike. CORS
    has exposed the header since P1b; nothing ever set it, so an x402 client
    had no receipt for a payment that had moved its money. Recorded on
    `request.state` here and attached by `request_context`, so every paid
    route gets it without each one building its own response.
    """
    if request is None:
        return
    body: dict[str, Any] = {
        "success": bool(settle_result.get("success")),
        "transaction": str(settle_result.get("transaction") or ""),
        "network": str(settle_result.get("network") or network),
        "payer": str(settle_result.get("payer") or payer),
    }
    if not body["success"]:
        body["errorReason"] = str(settle_result.get("errorReason") or "unknown")
    request.state.x402_payment_response = base64.b64encode(
        json.dumps(body, separators=(",", ":")).encode()
    ).decode()


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
    request: Request | None = None,
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

    def _same(field: str, got: Any, want: Any) -> bool:
        # P88: an EVM address is one value in any letter case — mixed case is
        # only the EIP-55 checksum encoding. A payer whose client checksummed
        # `payTo` or `asset` while the registered wallet was stored lowercase
        # (or the reverse) was refused for a payment that was exactly right.
        # Solana addresses are base58 and case-sensitive, so only EVM relaxes.
        if field in ("payTo", "asset") and family == "evm" and isinstance(got, str):
            return got.lower() == str(want).lower()
        return got == want

    mismatched = [
        field for field, want in expected.items()
        if want is None or not _same(field, accepted.get(field), want)
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
            # A retry whose earlier settlement landed gets the same receipt.
            _record_x402_settlement(request, {"success": True, "transaction": settled},
                                    network=network or "", payer=authorizer)
            return PaymentRef(RAIL_X402, settled)
        _reopen_x402_intent(
            intent_key=intent_key, network=network or "", asset=asset or "",
            authorizer=authorizer, nonce=nonce, valid_before=valid_before,
        )

    try:
        settle_result = await x402_settle_payment(x402_payload, expected)
        _record_x402_settlement(request, settle_result, network=network or "", payer=authorizer)
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
    destination_account: str | None = None,
    application_fee_cents: int | None = None,
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
                request=request,
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
                destination_account=destination_account,
                application_fee_cents=application_fee_cents,
            )
            result = await verify_payment_intent(result["payment_intent_id"])
            # No expected_payer_agent_id: confirm_spt() mints this PI from the very
            # agent_id this request claimed, so comparing them would be circular and
            # prove nothing — see _assert_payment_intent_matches()'s docstring.
            _assert_payment_intent_matches(
                result, amount_cents=amount_cents, currency=currency,
                resource_type=resource_type, resource_id=resource_id,
                expected_capture_method=capture_method, allowed_statuses=allowed_statuses,
                expected_destination=destination_account,
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
                # Stays a full-key check, deliberately — see
                # test_test_mode_capabilities_stay_gated_on_a_full_secret_key.
                # P75 tried to widen this to stripe_key_mode() and that test
                # caught it: a restricted key (rk_test_) is test *mode* but may
                # lack payment_intents:write, and a 403 surfacing here would be
                # reported to the caller as a payment failure. The narrow gate
                # costs a restricted-key relay this convenience; guessing wrong
                # costs it a misattributed error on the money path.
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
                expected_destination=destination_account,
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


def record_escrow_hold(hold: dict[str, Any]) -> None:
    """Store an authorized escrow hold (P99). The PaymentInfo is kept verbatim:
    capture and void must resubmit exactly what the payer signed."""
    now = datetime.now(UTC).isoformat()
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT INTO escrow_holds (hold_hash, session_id, payer_agent_id, payer_address, "
            "receiver_agent_id, receiver_address, network, amount_atomic, payment_info_json, "
            "state, authorize_tx, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hold["hold_hash"], hold["session_id"], hold["payer_agent_id"],
             hold["payer_address"], hold["receiver_agent_id"], hold["receiver_address"],
             hold["network"], str(hold["amount_atomic"]), json.dumps(hold["payment_info"]),
             "authorized", hold["authorize_tx"], now, now),
        )


def get_escrow_hold(hold_hash: str) -> dict[str, Any] | None:
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM escrow_holds WHERE hold_hash = ?",
                           (hold_hash,)).fetchone()
    if not row:
        return None
    hold = dict(row)
    hold["payment_info"] = json.loads(hold.pop("payment_info_json"))
    return hold


def escrow_holds_in_states(states: tuple[str, ...], limit: int = 100) -> list[dict[str, Any]]:
    """Holds in any of `states`, oldest first — the reconciler's queue (P111)."""
    marks = ",".join("?" * len(states))
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM escrow_holds WHERE state IN ({marks}) ORDER BY created_at LIMIT ?",
            (*states, limit)).fetchall()
    holds = []
    for row in rows:
        hold = dict(row)
        hold["payment_info"] = json.loads(hold.pop("payment_info_json"))
        holds.append(hold)
    return holds


def mark_escrow_settlement_captured(session_id: str, capture_tx: str | None) -> None:
    """A capture that failed at settlement and landed later (P111): the
    session's settlement row stops saying the seller was not paid."""
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "UPDATE settlements SET transfer_status = 'escrow_captured', "
            "transfer_id = COALESCE(?, transfer_id) "
            "WHERE session_id = ? AND rail = 'x402' AND transfer_status = 'capture_failed'",
            (capture_tx, session_id))


def active_sessions_of(module_type: str, limit: int = 200) -> list[dict[str, Any]]:
    """Active sessions of one module, oldest first (P112's deadline sweep)."""
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE module_type = ? AND status = 'active' "
            "ORDER BY updated_at LIMIT ?", (module_type, limit)).fetchall()
    return [_session_row_to_dict(r) for r in rows]


def idle_sessions(before: str, exclude_module: str, limit: int = 200) -> list[dict[str, Any]]:
    """Waiting or active sessions untouched since `before`, oldest first, other
    than `exclude_module` (P129's idle sweep; service sessions have deadlines)."""
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status IN ('waiting', 'active') AND updated_at < ? "
            "AND module_type != ? ORDER BY updated_at LIMIT ?",
            (before, exclude_module, limit)).fetchall()
    return [_session_row_to_dict(r) for r in rows]


def claim_session_status(session_id: str, expected: str, new: str) -> bool:
    """Move a session from `expected` to `new` only if it is still `expected`.
    One UPDATE, so of two concurrent callers exactly one wins (P111)."""
    with DB_LOCK, get_db() as conn:
        cur = conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ? AND status = ?",
            (new, datetime.now(UTC).isoformat(), session_id, expected))
        return cur.rowcount == 1


def update_escrow_hold(hold_hash: str, *, state: str, **tx: str) -> None:
    """Move a hold to `state`, recording the transaction that did it
    (capture_tx= or void_tx=)."""
    cols = {k: v for k, v in tx.items() if k in ("capture_tx", "void_tx")}
    sets = ", ".join(["state = ?", "updated_at = ?", *(f"{k} = ?" for k in cols)])
    with DB_LOCK, get_db() as conn:
        conn.execute(f"UPDATE escrow_holds SET {sets} WHERE hold_hash = ?",
                     (state, datetime.now(UTC).isoformat(), *cols.values(), hold_hash))


def mark_payment_released(payment_intent_id: str) -> None:
    """A held stake went back to its payer (P130). Only from `confirmed`, so a
    payment already refunded or captured keeps what it says."""
    with DB_LOCK, get_db() as conn:
        conn.execute("UPDATE payments SET status = 'released' WHERE payment_intent_id = ? "
                     "AND status = 'confirmed'", (payment_intent_id,))


def get_session_payments(session_id: str) -> list[dict[str, Any]]:
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE session_id = ? AND payment_type = 'session_stake'",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _atomic_str(v: Any) -> str | None:
    return None if v is None else str(int(v))


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
            "transfer_id, transfer_status, status, created_at, payer_id, dedupe_key, rail, "
            "platform_fee_atomic, winner_payout_atomic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
                _atomic_str(settlement.get("platform_fee_atomic")),
                _atomic_str(settlement.get("winner_payout_atomic")),
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
    ("bounty_entries", "agent_id"),
    ("escrow_holds", "payer_agent_id"), ("escrow_holds", "receiver_agent_id"),
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
    # bounty_entries (P72-4) — `agent_id` is a handle and lives in
    # _RENAME_COLUMNS above. `nous_user_id` deliberately does NOT: it is an
    # identity-provider subject, not an Emporia handle, and rewriting it on
    # rename would break the one-entry-per-account rule it exists to enforce —
    # the same reasoning that keeps payer_stable_id out, noted below.
    ("bounty_entries", "listing_id"), ("bounty_entries", "nous_user_id"),
    # escrow_holds (P99): the on-chain record of a hold. Addresses are wallets,
    # not handles, and the PaymentInfo they sit in is what the payer signed —
    # rewriting either would make the hold uncapturable.
    ("escrow_holds", "hold_hash"), ("escrow_holds", "session_id"),
    ("escrow_holds", "payer_address"), ("escrow_holds", "receiver_address"),
    ("escrow_holds", "network"), ("escrow_holds", "amount_atomic"),
    ("escrow_holds", "payment_info_json"), ("escrow_holds", "state"),
    ("escrow_holds", "authorize_tx"), ("escrow_holds", "capture_tx"),
    ("escrow_holds", "void_tx"), ("escrow_holds", "created_at"),
    ("escrow_holds", "updated_at"),
    ("bounty_entries", "entered_at"),
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
    ("listings", "federated_from"),
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
    ("settlements", "platform_fee_atomic"), ("settlements", "winner_payout_atomic"),
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
                    # P110: a re-registration with no valid Nous claim keeps the
                    # verification already proven for this key. Nous access
                    # tokens last an hour and the MCP server re-registers on
                    # every start, so this used to downgrade every agent to
                    # key_only by the next day. Safe because the key cannot
                    # change here (refused above): only the key's holder can
                    # re-register, and a claim for a different Nous user
                    # still replaces the old one.
                    "nous_user_id=COALESCE(excluded.nous_user_id, authorized_agents.nous_user_id), "
                    "trust_level=CASE WHEN excluded.nous_user_id IS NULL "
                    "THEN authorized_agents.trust_level ELSE excluded.trust_level END, "
                    "identity_providers_json=CASE WHEN excluded.nous_user_id IS NULL "
                    "THEN authorized_agents.identity_providers_json "
                    "ELSE excluded.identity_providers_json END",
                    (agent_id, public_key_hex, verified_display_name,
                     datetime.now(UTC).isoformat(), stripe_account_id,
                     nous_user_id, trust_level, json.dumps(providers_verified),
                     derived_stable_id),
                )
                stored = conn.execute(
                    "SELECT stable_id, trust_level, nous_user_id FROM authorized_agents "
                    "WHERE agent_id = ?", (agent_id,),
                ).fetchone()
                stable_id = stored["stable_id"]
                # P110: what the row holds, which a claim-less re-register keeps.
                trust_level, nous_user_id = stored["trust_level"], stored["nous_user_id"]
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
    globals()["_BOUNTY_RECONCILE_TASK"] = asyncio.create_task(_bounty_reconcile_loop())
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
        _bounty_task = globals().get("_BOUNTY_RECONCILE_TASK")
        if _bounty_task is not None:
            _bounty_task.cancel()
            with suppress(asyncio.CancelledError):
                await _bounty_task
            globals()["_BOUNTY_RECONCILE_TASK"] = None
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
    allow_headers=["Content-Type", "Authorization", "X-Emporia-Agent-Id", "PAYMENT-SIGNATURE",
                   "X-Emporia-Authorization", "PAYMENT-ESCROW"],
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
        # P93: an x402 settlement's receipt, recorded by _record_x402_settlement().
        payment_response = getattr(request.state, "x402_payment_response", None)
        if payment_response:
            response.headers["PAYMENT-RESPONSE"] = payment_response
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

_BOUNTY_RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("EMPORIA_BOUNTY_RECONCILE_INTERVAL_SECONDS", "300")
)
_BOUNTY_RECONCILE_TASK: "asyncio.Task | None" = None
BOUNTY_RECONCILE_STATS: dict[str, Any] = {
    "runs": 0, "last_run_at": None, "last_error_at": None,
    "consecutive_failures": 0, "settled": 0, "awaiting_payment": 0,
    "refunded": 0, "defaulted": 0, "unfunded": 0,
}

_PAYOUT_RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("EMPORIA_PAYOUT_RECONCILE_INTERVAL_SECONDS", "300")
)

PAYOUT_RECONCILE_STATS: dict[str, Any] = {
    "runs": 0,
    "last_run_at": None,
    "last_error_at": None,
    "consecutive_failures": 0,
    "outcomes": {"owed": 0, "recovered": 0, "paid": 0, "unknown": 0,
                 "still_blocked": 0, "failed": 0},
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

    The lookup, not the `Idempotency-Key`, is the guard against paying twice:
    the key is pruned after 24 hours. P88 corrected what this used to say —
    that every row here is older than that "by construction". It is not; this
    runs every few minutes, so a row can be retried minutes after it failed.
    The consequence runs the other way too: Stripe saves the *first* response
    to a key once execution begins, errors included, so a transfer refused for
    an unavailable balance replays that refusal from cache on every retry until
    the key is pruned, and only then is actually re-attempted. Paid late, never
    twice. P93 made that rare at the source: a payout funded by one charge now
    names it as `source_transaction` and draws on it while it is still
    pending. Retries from here still go without one, since a row records its
    PaymentIntents but not which charge funded the transfer.
    """
    counts = {"owed": 0, "recovered": 0, "paid": 0, "unknown": 0,
              "still_blocked": 0, "failed": 0}

    # P51r: no credentials, nothing to reconcile — and asking anyway raises out
    # of `_stripe_key()`, which is a permanent, benign condition that would
    # otherwise be reported as a failing loop forever. Found by running the
    # relay, not by a test: every test in the suite reaches Stripe through a
    # mock, so none of them can observe what an unconfigured relay does.
    if not (os.getenv("STRIPE_SECRET_KEY") or "").strip():
        return counts

    for row in _owed_payout_rows():
        counts["owed"] += 1
        # P51r: per-row isolation. N12-1 established that a raise must not end
        # the *loop*; this is the same rule one level down, and the level that
        # actually matters here — without it a single unreconcilable row aborts
        # the sweep for every row behind it, permanently and in row order.
        try:
            outcome = await _reconcile_one_payout(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Payout reconcile failed for settlement %s — other rows continue",
                row["settlement_id"],
            )
            counts["failed"] += 1
            continue
        counts[outcome] += 1
    return counts


async def _reconcile_one_payout(row: dict[str, Any]) -> str:
    """One owed settlement. Returns the outcome key it should be counted under.

    Split out of `_reconcile_owed_payouts()` so that "what happens to one row"
    and "one row must not take the others down with it" are separate, testable
    things rather than one function where the second is a property of how
    carefully the first was written.
    """
    from emporia.payments import (
        find_settlement_transfer, payout_winner, stripe_account_mode_mismatch,
    )

    known, transfer_id = await find_settlement_transfer(
        transfer_group=row["session_id"], dedupe_key=row["dedupe_key"],
    )
    if not known:
        # Could not ask. Never rounded down to "not paid".
        return "unknown"
    if transfer_id:
        # It landed and we lost the answer. No money moves here.
        _record_payout_outcome(row["settlement_id"], "transferred", transfer_id)
        return "recovered"

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
        return "still_blocked"

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
            "Payout transfer failed for settlement %s: %s", row["settlement_id"], e
        )
        _record_payout_outcome(row["settlement_id"], "transfer_failed", None)
        return "failed"
    _record_payout_outcome(row["settlement_id"], "transferred", transfer["transfer_id"])
    return "paid"


async def _bounty_reconcile_loop() -> None:
    """Runs the bounty reconciler on an interval (P80).

    P79 wrote the reconciler and never called it. That is not a missing
    convenience: the automatic default flag is the *only* thing backing a
    promised prize, so a reconciler nobody runs means a funder can take an
    agent's work and never pay, and nothing anywhere notices. It also left
    every held bounty reading "running" on the board after it had been paid.

    Same shape as `_payout_reconcile_loop()` above, including N12-1's rule that
    one raise must not end the loop for the life of the process — a loop that
    died silently would be indistinguishable from the defect it exists to fix.
    """
    from emporia.routes.bounties import reconcile_bounties

    while True:
        await asyncio.sleep(_BOUNTY_RECONCILE_INTERVAL_SECONDS)
        try:
            counts = await reconcile_bounties()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bounty reconcile failed — will retry next interval")
            BOUNTY_RECONCILE_STATS["last_error_at"] = datetime.now(UTC).isoformat()
            BOUNTY_RECONCILE_STATS["consecutive_failures"] += 1
            continue
        BOUNTY_RECONCILE_STATS["runs"] += 1
        BOUNTY_RECONCILE_STATS["last_run_at"] = datetime.now(UTC).isoformat()
        BOUNTY_RECONCILE_STATS["consecutive_failures"] = 0
        for key, ids in counts.items():
            if ids:
                BOUNTY_RECONCILE_STATS[key] += len(ids)
                logger.info("bounty_reconcile", extra={"transition": key, "count": len(ids)})


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
            # P111: failed escrow captures/voids, retried while the hold allows.
            from emporia.routes.sessions import reconcile_escrow_holds
            counts.update(await reconcile_escrow_holds())
            # P112: service sessions whose deadline passed with nobody acting.
            from emporia.routes.sessions import sweep_idle_sessions, sweep_service_deadlines
            counts.update(await sweep_service_deadlines())
            # P129: any other session nobody has touched for a day.
            counts.update(await sweep_idle_sessions())
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
            outcomes = PAYOUT_RECONCILE_STATS["outcomes"]
            outcomes[key] = outcomes.get(key, 0) + count


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
    # P95: a wager's creator stakes too; the direct-PaymentIntent alternative
    # to an `Authorization: Payment <spt>` credential, as on join.
    payment_intent_id: str | None = None


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
    """The single definition for `payment_rails`/`payment_methods` — one rule,
    two callers (P3 item 5, unified in P70).

    `has_stripe=None` selects the relay-wide variant (`GET /health`);
    `has_stripe=<bool>` the per-agent variant (`GET /agents/{id}`). They now
    differ in exactly one way: `stripe_connect` is a statement about *this
    agent* (it has a stored Connect account and can receive payouts), so it
    exists only in the per-agent answer. Every other rail is a relay-wide
    capability and must read identically from both doors.

    **P70 fixed a divergence P3 deliberately preserved.** P3 factored two
    hand-built lists into this one function but reproduced both formulas
    byte-for-byte, because "no wire change" was that pass's contract — which
    left one flag combination answering differently depending on which
    endpoint you asked. With a Stripe key set, the MPP profile not ready and
    `EMPORIA_MPP_TEMPO_ENABLED=1`, `/health` advertised `"mpp"` (its
    `if payment_methods:` test is truthy on `["tempo"]`) while
    `GET /agents/{id}` omitted it: an `elif` chain resolved `stripe_enabled`
    first and never reached the tempo branch. Same relay, same config, two
    answers to "can I pay by MPP here?".

    The `elif` chain was the bug. `mpp` (is the MPP transport usable at all?)
    and `stripe_pi` (is there a Stripe key?) are independent conditions, and
    chaining them made the second silently suppress the first. Each rail now
    has exactly one condition, tested independently — which is also what makes
    adding a rail a local edit rather than a matching pair of edits in two
    formulas that no test required to agree.

    Note the ordering dependency this relies on: `_stripe_profile_ready()`
    already requires `STRIPE_SECRET_KEY`, so `stripe_profile_ready` implies
    `stripe_enabled` and the two conditions cannot contradict each other.

    Pure advertisement: nothing in the payment path reads either list to route
    a charge. `payment_methods` stays Stripe's MPP-specific sub-vocabulary
    (`stripe`/`tempo`, both settlement backends reachable *through* MPP's
    challenge/response transport); `x402` is a rail the way `stripe_pi` is —
    a settlement backend with no MPP transport underneath it — and so is
    advertised whenever the rail is enabled, not filtered by whether this
    particular creator has a wallet set. That per-creator, per-network
    filtering happens where it must, in the 402 challenge itself
    (`_x402_pay_to_by_network()`, P1b-4).
    """
    stripe_enabled = bool(os.getenv("STRIPE_SECRET_KEY"))
    stripe_profile_ready = _stripe_profile_ready()
    from emporia.payments import _x402_enabled

    payment_methods: list[str] = []
    if stripe_profile_ready:
        payment_methods.append("stripe")
    if TEMPO_ENABLED:
        payment_methods.append("tempo")

    # One condition per rail. No elif — the conditions are independent.
    payment_rails = ["free"]
    if payment_methods:
        payment_rails.append("mpp")
    if stripe_enabled:
        payment_rails.append("stripe_pi")
    if has_stripe and stripe_profile_ready:
        payment_rails.append("stripe_connect")  # can receive payouts
    if _x402_enabled():
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
    """Every game this relay hosts, with its rules for agents (P133)."""
    from emporia.module_sdk import list_available_modules
    return {"modules": list_available_modules()}


# ============================================================================
# Agent Registration — challenge / proof-of-key-possession
# ============================================================================

_CHALLENGE_TTL_SECONDS = 300  # nonces expire after 5 minutes




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

















_X402_ADDRESS_PATTERNS = {
    # P1b-2: an EVM address and a Solana address are different key spaces
    # (secp256k1 vs. ed25519), so a wallet is set per network_family rather
    # than as one universal address — see AGENT_REGISTRY.get/set_x402_pay_to.
    "evm": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "svm": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),  # base58, Bitcoin alphabet (no 0OIl)
}


class RenameAgentRequest(BaseModel):
    new_agent_id: str






def _stripe_onboarding_return_url(request: Request) -> str:
    base = (os.getenv("EMPORIA_DASHBOARD_BASE_URL") or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/stripe-onboarding-return"




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

    # P74: refuse a price the rail cannot charge, here rather than at Stripe.
    # The previous guard was `expected_amount <= 0`, so a resource priced at a
    # penny passed and the agent met a raw provider error for a decision the
    # relay could have made itself. The operator set an unsellable price; say so.
    #
    # The `0 <` half is belt-and-braces, not load-bearing: both branches above
    # already reject a free resource with "does not require a paid stake", so
    # zero cannot reach here today. Stated rather than left to look protective —
    # a stash-verify that folded zero in broke nothing, which is how I know.
    from emporia.payments import STRIPE_MIN_CHARGE_CENTS
    if 0 < expected_amount < STRIPE_MIN_CHARGE_CENTS:
        raise HTTPException(400, (
            f"This resource is priced at {expected_amount}¢, below the "
            f"{STRIPE_MIN_CHARGE_CENTS}¢ minimum this payment rail can charge, so it "
            f"cannot be paid for at all. That is a pricing mistake by the relay "
            f"operator, not something the caller can work around — price it at "
            f"{STRIPE_MIN_CHARGE_CENTS}¢ or more, or make it free."
        ))

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
    "platform_fee_atomic", "winner_payout_atomic",
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


_ESCROW_HOLD_PUBLIC_COLUMNS = (
    "hold_hash, session_id, payer_agent_id, payer_address, receiver_agent_id, "
    "receiver_address, network, amount_atomic, state, authorize_tx, capture_tx, void_tx, "
    "created_at, updated_at"
)


@app.get("/payments/escrow-holds")
async def list_escrow_holds(request: Request):
    """x402 escrow holds (P99), for the dashboard (P105). Read-only.

    The relay operator sees every hold; any other authenticated agent sees the
    holds it paid or is paid by, and nothing else. `payment_info_json` stays
    internal — the transactions are the public record, and each is linked.
    """
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    owner = _relay_operator_id()
    with DB_LOCK, get_db() as conn:
        if owner and caller == owner:
            rows = conn.execute(
                f"SELECT {_ESCROW_HOLD_PUBLIC_COLUMNS} FROM escrow_holds "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_ESCROW_HOLD_PUBLIC_COLUMNS} FROM escrow_holds "
                "WHERE payer_agent_id = ? OR receiver_agent_id = ? "
                "ORDER BY created_at DESC LIMIT 200",
                (caller, caller),
            ).fetchall()
    return {"holds": [dict(r) for r in rows]}


# Listings and Events moved to emporia/routes/{listings,events}.py (P56).
# The routers are included at the bottom of this file.



# ============================================================================
# Federation
# ============================================================================



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


def _peer_owns_listing(existing: Any, peer_url: str) -> bool:
    """May the peer at `peer_url` replace this existing listing row? (P87)

    Only the peer it was pulled from. A local row (`federated_from` NULL and
    `origin_relay` this relay) is never peer-writable. A row pulled before P87
    has no `federated_from`, so it is adopted by a peer whose URL is the row's
    own origin — the ordinary case, where a relay is reached at the URL it
    reports — and by no other.
    """
    if existing["federated_from"]:
        return normalize_peer_url(existing["federated_from"]) == peer_url
    origin = existing["origin_relay"] or ""
    if not origin or normalize_peer_url(origin) == normalize_peer_url(RELAY_BASE_URL):
        return False
    return normalize_peer_url(origin) == peer_url


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
    rejected_foreign = 0
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
            # P83: ownership, the rule N12-3 put on lobby imports and this
            # importer never got. A signature proves who sent the batch, not
            # what they may say about someone else's row: `INSERT OR REPLACE`
            # let any configured peer overwrite a *local* listing by reusing its
            # id — a bounty's payment_intent_id, state and funder included.
            #
            # P87: owned by the peer it was pulled from, not by the origin that
            # peer claimed. `origin` is self-reported, so P83's origin check let
            # one peer claim another's origin and overwrite that peer's rows.
            # `peer_url` is the URL we fetched and whose key we pinned.
            existing = conn.execute(
                "SELECT origin_relay, federated_from FROM listings WHERE listing_id = ?",
                (listing["listing_id"],),
            ).fetchone()
            if existing and not _peer_owns_listing(existing, peer_url):
                rejected_foreign += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO listings "
                "(listing_id, title, description, listing_type, agent_id, payment_mode, "
                "price_usd, module_type, origin_relay, expires_at, metadata_json, created_at, "
                "federated_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    peer_url,
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
    if rejected_foreign:
        sync_record["rejected_not_owner"] = rejected_foreign
    _FEDERATION_LAST_SYNC[peer_url] = sync_record
    return imported




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






# ============================================================================
# Sessions
# ============================================================================

















class ConfirmDeliveryRequest(BaseModel):
    agent_id: str
    rationale: str = ""
    signature: str | None = None


class DisputeDeliveryRequest(BaseModel):
    agent_id: str
    reason: str
    rationale: str = ""
    signature: str | None = None








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


# Gaming lobby moved to emporia/routes/gaming.py (P57).
# `_game_registry` stayed: the federation routes above use it too (P57).

_game_registry = GameRegistry(DATABASE_PATH)



# ============================================================================
# Rooms
# Public: open to all registered agents.
# Private: hidden from listing; gated by invite OR Stripe payment.
# Encryption flag: relay stores opaque ciphertext, skips guardrails scan.
# Negotiation routing: counter_offer in a 2-member private room is brokered.
# Session link: a game session can have an associated room for in-session chat.
# ============================================================================







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
                        source_payment_intent_id=payment_ref.reference,
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
    # P59: the third copy of the rule. It now delegates like the other two —
    # the exemption here is role-based rather than creator-id, which is exactly
    # why `access_active()` takes `is_exempt` as a caller-computed boolean
    # instead of trying to work out who is privileged on its own.
    return entitlements.access_active(
        "agora", topic_id, agent_id,
        is_member=True,
        gate_has_expiry=gate_type == "paid_invite",
        is_exempt=member["role"] in ("owner", "moderator"),
        conn=conn,
    )


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








class AgoraInviteRequest(BaseModel):
    invitee_id: str        # who is being invited
























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


_DASHBOARD_NOT_BUILT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Emporia relay — dashboard not built</title>
<style>body{font:14px/1.6 system-ui,sans-serif;margin:3rem auto;max-width:44rem;padding:0 1rem}
code{background:#eee;padding:.15rem .35rem;border-radius:3px}</style></head><body>
<h1>Emporia relay is running</h1>
<p>The operator dashboard has not been built, so there is nothing to serve at
<code>/ui/</code> yet. Build it once:</p>
<pre><code>cd dashboard &amp;&amp; npm install &amp;&amp; VITE_RELAY_URL="" npm run build</code></pre>
<p>then reload this page. The relay itself is up and serving the API —
<a href="/health">/health</a>, <a href="/docs">/docs</a>.</p>
</body></html>"""


@app.get("/")
async def root_redirect():
    """P53: only redirect somewhere that exists.

    `/ui` is mounted at import time and only when `dashboard/dist` is present,
    but this redirect used to fire unconditionally — so on a relay installed by
    the documented two-step (pip install, then build the dashboard), the first
    URL anyone opens 307'd into a bare `{"detail":"Not Found"}`. The relay was
    healthy; its front door led nowhere and said nothing.

    Checked per request rather than at import so the page starts working the
    moment the build lands, without a restart. `payments._x402_enabled()` is
    the same principle one rail over: a partly configured thing should behave
    like a configured-off thing, not fail halfway.
    """
    if not _DASHBOARD_DIST.exists():
        return HTMLResponse(_DASHBOARD_NOT_BUILT_HTML, status_code=200)
    return RedirectResponse(url="/ui/")


# DM moved to emporia/routes/dm.py (P57). Router included at the bottom.
# These two stayed: the room and Agora join paths use them too (P57).


_NONCE_RE = re.compile(r"[0-9a-fA-F]{32,64}")


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




# Main
# ============================================================================

def main():
    uvicorn.run(app, host="0.0.0.0", port=RELAY_PORT)


# ============================================================================
# Routers (P56)
#
# Imported last, after every name they reference exists. Each router reaches
# shared state through `rs.<name>` at call time rather than binding it at
# import, so the suite's 43 `patch("emporia.relay_server.<name>")` sites still
# intercept. See emporia/routes/__init__.py.
# ============================================================================

from emporia.routes import agents as _agent_routes
from emporia.routes import bounties as _bounty_routes
from emporia.routes import standing as _standing_routes  # noqa: E402
from emporia.routes import agoras as _agora_routes  # noqa: E402
from emporia.routes import dm as _dm_routes  # noqa: E402
from emporia.routes import gaming as _gaming_routes  # noqa: E402
from emporia.routes import events as _events_routes  # noqa: E402
from emporia.routes import federation as _federation_routes  # noqa: E402
from emporia.routes import listings as _listings_routes  # noqa: E402
from emporia.routes import rooms as _room_routes  # noqa: E402
from emporia.routes import sessions as _session_routes  # noqa: E402

app.include_router(_listings_routes.router)
app.include_router(_events_routes.router)
app.include_router(_dm_routes.router)
app.include_router(_gaming_routes.router)
app.include_router(_federation_routes.router)
app.include_router(_agora_routes.router)
app.include_router(_room_routes.router)
app.include_router(_session_routes.router)
app.include_router(_agent_routes.router)
app.include_router(_bounty_routes.router)
app.include_router(_standing_routes.router)
