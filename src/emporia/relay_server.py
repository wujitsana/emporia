"""Emporia Relay Server — FastAPI relay for federated agent commerce.

Provides:
  - Session management (create, join, action, state)
  - Federated pointer-post gossip (pull from peer relays)
  - Relay descriptor (not an A2A Agent Card) at /relay-card
  - WebSocket real-time updates (per-session + per-agent)
  - Guardrails on all inbound (deterministic regex firewall)
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
# command served a relay whose extracted routes (agents, sessions,
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
import sys
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
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
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

from emporia.engine import guardrails
from emporia.engine.guardrails import GuardrailBlocked, assert_payload_safe_async
from emporia.identity import (
    relay_key_history,
    verify,
    get_relay_public_key_hex,
    load_federation_peer_pubkeys,
    save_federation_peer_pubkeys,
    load_or_generate_relay_jwt_secret,
    load_or_derive_relay_id,
    build_relay_descriptor,
    federation_signable_bytes,
    normalize_peer_url,
)
from emporia.module_sdk import (
    MODULE_REGISTRY,
    SessionState,
    get_interaction_module,
    session_kind,
)
from emporia import __version__
from emporia import names as _names
from emporia import proxy_ip as _proxy_ip
from emporia import operators
from emporia import rooms as rooms_db
from emporia import entitlements
from emporia import translog
from emporia.env_config import env_int
from emporia.logging_config import (
    configure_logging, configure_sentry, ip_tag, mark_expected, redact_tokens_in, request_id_ctx,
    request_info_ctx,
)

if TYPE_CHECKING:
    # Type-only — every real (runtime) reference to emporia.payments stays a
    # function-local import (L2, this file's own discipline): 75 test
    # @patch("emporia.payments.X") decorators only intercept because nothing
    # here imports these names at module scope. A TYPE_CHECKING import is
    # erased before runtime, so it can't affect that.
    from emporia.payments import PaymentRef

# Trigger module registration — import all bundled modules at startup
import emporia.modules.bargain  # noqa: F401
import emporia.modules.cv_auction  # noqa: F401
import emporia.modules.liars_dice  # noqa: F401
import emporia.modules.blotto  # noqa: F401
import emporia.modules.chess  # noqa: F401
import emporia.modules.code_review  # noqa: F401
import emporia.modules.research  # noqa: F401
import emporia.modules.lowest_unique  # noqa: F401
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
redact_tokens_in("uvicorn.error", "uvicorn.access", "emporia.relay")   # P267: no ?token= in a log
logger = logging.getLogger("emporia.relay")

RELAY_PORT = int(os.getenv("EMPORIA_RELAY_PORT", "8088"))
# P183: this machine only. A relay others reach runs in a container behind
# https (`emporia relay host`), so any other address is refused outside one —
# an agent's tools on this machine cannot open its relay to the internet.
RELAY_HOST = os.getenv("EMPORIA_RELAY_HOST", "127.0.0.1")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def in_container() -> bool:
    from emporia.relay_host import in_container as _in_container
    return _in_container()


def bind_refusal(host: str) -> str:
    """Why the relay will not listen on `host`; "" when it may."""
    if not host or host in _LOOPBACK or in_container():
        return ""
    return (f"emporia relay: not listening on {host} — outside a container the relay listens on this machine "
            f"only (127.0.0.1). For agents on other machines: emporia relay host (Docker, behind https).")


def live_money() -> list[str]:
    """P202: what makes this relay move real money, if anything."""
    from emporia.networks import is_testnet
    reasons = []
    if (os.getenv("STRIPE_SECRET_KEY") or "").strip().startswith(("sk_live_", "rk_live_")):
        reasons.append("a live Stripe key")
    nets = [n.strip() for n in (os.getenv("EMPORIA_X402_NETWORKS") or "").split(",") if n.strip()]
    if os.getenv("EMPORIA_X402_ENABLED", "").strip() == "1" and any(not is_testnet(n) for n in nets):
        reasons.append("USDC on a mainnet")
    escrow = (os.getenv("EMPORIA_ESCROW_NETWORK") or "").strip()
    if escrow and not is_testnet(escrow):
        reasons.append("escrow on a mainnet")
    return reasons


def profile_holding(keys_dir: Path) -> Path | None:
    """The Hermes profile (a folder with config.yaml) the relay's keys are in, if any."""
    for parent in keys_dir.resolve().parents:
        if (parent / "config.yaml").exists():
            return parent
    return None


def live_money_refusal(keys_dir: Path) -> str:
    """P202: a relay moving real money keeps its keys away from every agent. In a
    Hermes profile they share a disk — and a user — with that profile's agent,
    whose terminal can read them: fine on testnet (developing, trying Emporia on
    one machine), never with real money. `emporia relay host` puts them in a
    Docker volume or under a system user of their own."""
    reasons = live_money()
    profile = profile_holding(keys_dir)
    if not reasons or profile is None or os.getenv("EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE") == "1":
        return ""
    return (f"emporia relay: refusing to start — it would take {' and '.join(reasons)}, and its keys are in the "
            f"Hermes profile {profile}, where that profile's agent can read them. Run it with `emporia relay host` "
            f"(Docker, or a system service) so they live apart from every agent; or, accepting the risk, "
            f"EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE=1.")


def _uvicorn_host() -> str:
    """The --host a `uvicorn emporia.relay_server:app` command line gives."""
    if "uvicorn" not in Path(sys.argv[0]).name:
        return ""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--host="):
            return a.split("=", 1)[1]
    return ""


if _refusal := bind_refusal(_uvicorn_host()) or (
        live_money_refusal(Path(os.getenv("EMPORIA_KEYS_DIR", "~/.hermes/keys")).expanduser())
        if _uvicorn_host() or "uvicorn" in Path(sys.argv[0]).name else ""):
    raise SystemExit(_refusal)
RELAY_BASE_URL = os.getenv("EMPORIA_RELAY_URL", f"http://localhost:{RELAY_PORT}")
def relay_id() -> str:
    """P286: this relay's id — EMPORIA_RELAY_ID if set (a relay that linked under a name
    keeps it), else the one taken from its key and kept beside it."""
    return os.getenv("EMPORIA_RELAY_ID", "").strip() or load_or_derive_relay_id()


# Set EMPORIA_REQUIRE_CHALLENGE=0 to allow registration without proof-of-key-possession
# (default mandatory — every first-party client already completes the challenge round-trip
# unconditionally, so this only weakens a direct REST caller's requirements; not recommended).
REQUIRE_CHALLENGE = os.getenv("EMPORIA_REQUIRE_CHALLENGE", "1").strip() == "1"
MAX_TOTAL_SPEND_CENTS = int(os.getenv("EMPORIA_MAX_TOTAL_SPEND_CENTS", "0"))
LOG_DIR = Path(os.getenv("EMPORIA_LOG_DIR", "./.local/logs")).expanduser()
# P55: resolved through emporia.config, the package's only reader of
# EMPORIA_DB_PATH. Still captured once here — 111 call sites in the suite treat
# it as a Path — but it is now provably the same file every other store opens.
DATABASE_PATH = _database_path()

# Federated peers (comma-separated relay URLs; empty = standalone). Normalized
# (N13-2, R2) so these entries agree with the keys _pull_federated_posts
# actually stores under — see identity.normalize_peer_url().
FEDERATED_RELAYS: list[str] = [
    normalize_peer_url(u) for u in os.getenv("FEDERATED_RELAYS", "").split(",") if u.strip()
]

# Last federation sync outcome per peer, for the dashboard's Federation panel.
# {peer_url: {"ok": bool, "imported": int, "synced_at": iso8601}}
_FEDERATION_LAST_SYNC: dict[str, dict[str, Any]] = {}
# P291: what each linked relay runs beyond its core, from its signed card, read at
# each sync. Absent: not known yet (never synced, or an older relay with no card).
_FEDERATION_PEER_MODULES: dict[str, dict[str, Any]] = {}


def peer_runs(peer_url: str, module: str, part: str = "") -> bool | None:
    """P291: does this linked relay run `module` (or its `part`), by its last signed
    card? None when not known — the caller then does what it did before it could ask."""
    mods = _FEDERATION_PEER_MODULES.get(normalize_peer_url(peer_url))
    if mods is None:
        return None
    entry = mods.get(module)
    return isinstance(entry, dict) and (not part or part in (entry.get("parts") or []))

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
        _FEDERATION_PEER_PUBKEYS.update(load_federation_peer_pubkeys())
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


def custodial_cards_allowed() -> bool:
    """Card flows that leave money on the operator's Stripe balance: an entry or a purchase paid to the platform and transferred on. Off by default — with it off, every
    card payment is a destination charge to the person it is for, as USDC is
    (P157). An operator who wants those flows sets EMPORIA_CUSTODIAL_CARDS=1
    and takes on holding other people's money."""
    return os.environ.get("EMPORIA_CUSTODIAL_CARDS", "").strip().lower() in ("1", "true", "yes", "on")


def stripe_payee_account(agent_id: str | None) -> str | None:
    """This agent's connected account when a destination charge can pay it:
    present, onboarded, and in this relay's key space. Else None."""
    if not agent_id:
        return None
    account = AGENT_REGISTRY.get_stripe_account(agent_id)
    if not account or AGENT_REGISTRY.get_stripe_account_status(agent_id) != "active":
        return None
    if _stripe_account_mode_mismatch(AGENT_REGISTRY.get_stripe_account_mode(agent_id)):
        return None
    return account


def card_payee(agent_id: str | None, amount_cents: int) -> tuple[str | None, int | None, str | None]:
    """(destination account, application fee, why cards are off) for a card
    payment to `agent_id`. Custodial relays keep the old platform charge."""
    if custodial_cards_allowed():
        return None, None, None
    from emporia.payments import platform_fee
    account = stripe_payee_account(agent_id)
    if not account:
        return None, None, (f"{agent_id} has no active Stripe account here, and card payments "
                            f"go straight to the payee on this relay")
    return account, platform_fee(amount_cents), None


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


_LOCALHOST_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Rate-limit exemption is a much lower-stakes bypass than identity trust below — "don't
# throttle this IP" grants nothing an attacker can act on. Bare loopback is always exempt;
# an operator's own reverse-proxy/LB IPs (EMPORIA_TRUSTED_LOCAL_HOSTS) are an explicit opt-in
# independent of the header-auth flag below.
_RATE_LIMIT_EXEMPT_HOSTS: set[str] = set(_LOCALHOST_HOSTS)
_RATE_LIMIT_EXEMPT_HOSTS.update(
    h.strip() for h in os.getenv("EMPORIA_TRUSTED_LOCAL_HOSTS", "").split(",") if h.strip()
)

# P261: identity is only ever a relay-issued bearer token (or a signature). The
# header trust that let a local request name its agent (`X-Emporia-Agent-Id`,
# EMPORIA_TRUST_LOCAL_HEADER_AUTH) is gone: on a machine with several agents any
# one of them -- or a web page reaching 127.0.0.1 by DNS rebinding -- could claim
# to be any other agent or the operator. The dashboards sign in with the agent's
# key instead (the local console; the challenge for a dashboard reaching a relay
# directly).

# ─── Dashboard JWT (HMAC-SHA256, stdlib only) ────────────────────────────────
import base64
import hashlib
import hmac as _hmac

_JWT_TTL_SECONDS = 3600                  # 1-hour sessions
_CHALLENGE_STORE: dict[str, tuple[str, float]] = {}   # challenge_id → (nonce, expires_at)
_CHALLENGE_TTL = 300.0  # 5-minute window to complete sign


def _jwt_encode(payload: dict) -> str:
    import json as _json
    payload = {"jti": secrets.token_hex(12), **payload}      # every token has an id, so it can be revoked
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
    # P246: a token from before the agent's last key recovery was issued to a key it no longer has.
    floor = _SESSION_FLOOR.get(str(payload.get("agent_id") or ""))
    if floor is not None and payload.get("issued", 0) < floor:
        return None
    # Revocation: a signed-out token (its `jti`), and every token issued before an agent's revoke-all.
    if _token_revoked(payload):
        return None
    return payload


# Revoked tokens, kept in memory (loaded at start from `token_revocations` and `token_floors`, and written through
# on every revocation) so that checking a token costs no database read.
_REVOKED: dict[str, float] = {}           # a signed-out token's id -> when it would have expired
_TOKEN_FLOORS: dict[str, float] = {}      # stable id -> tokens issued before this time are revoked


def _load_revocations(conn: Any) -> None:
    _REVOKED.clear()
    _TOKEN_FLOORS.clear()
    for r in conn.execute("SELECT jti, expires_at FROM token_revocations WHERE expires_at >= ?", (time.time(),)):
        _REVOKED[r["jti"]] = r["expires_at"]
    for r in conn.execute("SELECT stable_id, valid_after FROM token_floors"):
        _TOKEN_FLOORS[r["stable_id"]] = r["valid_after"]


def _token_revoked(payload: dict) -> bool:
    jti, stable = str(payload.get("jti") or ""), str(payload.get("stable_id") or "")
    if not jti or jti in _REVOKED:       # a token with no id cannot be revoked, so none is accepted
        return True
    floor = _TOKEN_FLOORS.get(stable)
    return floor is not None and float(payload.get("issued", 0)) < floor


def revoke_token(payload: dict) -> None:
    """Sign out: this token (by its id) stops working, whoever holds a copy."""
    jti, expires = str(payload.get("jti") or ""), float(payload.get("exp", 0))
    with DB_LOCK, get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO token_revocations (jti, stable_id, revoked_at, expires_at) VALUES (?,?,?,?)",
                     (jti, str(payload.get("stable_id") or ""), datetime.now(UTC).isoformat(), expires))
        conn.execute("DELETE FROM token_revocations WHERE expires_at < ?", (time.time(),))
    _REVOKED[jti] = expires
    for old in [j for j, exp in _REVOKED.items() if exp < time.time()]:
        _REVOKED.pop(old, None)


def revoke_all_tokens(stable_id: str) -> float:
    """Revoke-all: every token issued to this agent before now stops working (it signs in again)."""
    now = time.time()
    with DB_LOCK, get_db() as conn:
        conn.execute("INSERT INTO token_floors (stable_id, valid_after) VALUES (?, ?) "
                     "ON CONFLICT(stable_id) DO UPDATE SET valid_after = excluded.valid_after", (stable_id, now))
    _TOKEN_FLOORS[stable_id] = now
    return now


def bearer_payload(request: Request) -> dict | None:
    """The verified token claims of the request's bearer credential (as `_dashboard_agent_id` reads it)."""
    for name in ("authorization", "x-emporia-authorization"):
        auth = request.headers.get(name, "")
        if auth.startswith("Bearer "):
            payload = _jwt_decode(auth[7:])
            if _token_agent(payload):
                return payload
    return None


def _token_agent(payload: dict | None) -> str | None:
    """The agent a verified token speaks for, or None. A token is for this relay and carries the agent's
    **stable id**: it is refused when the name now belongs to another key (the agent was renamed or left
    and someone else registered the name), or when it names no stable id at all."""
    if not payload or payload.get("relay_id") != relay_id():
        return None
    agent_id = payload.get("agent_id") or None
    if not agent_id:
        return None
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if row and (not payload.get("stable_id") or payload["stable_id"] != row["stable_id"]):
        return None
    return agent_id


def _dashboard_agent_id(request: Request) -> str | None:
    """
    Resolve the requesting agent for dashboard REST calls.

    Trust model (P261: a bearer token, nothing else):
      - Authorization: Bearer <jwt>                → verified via HMAC-SHA256 JWT
      - X-Emporia-Authorization: Bearer <jwt>      → the same, when Authorization
                                                     carries a payment (P95)
      - anything else                              → None (unauthenticated)

    P95: a paying request has two things to say — who is paying and with what —
    and they used to share one header. `Authorization: Payment <spt>` displaced
    the identity bearer, so a remote agent paying with an SPT could not also
    prove who it was. Every route that must know the payer (funding a
    purchase) 401'd for any agent not co-located with the relay. The identity now
    has its own header when the payment needs that one.
    """
    for name in ("authorization", "x-emporia-authorization"):
        auth = request.headers.get(name, "")
        if auth.startswith("Bearer "):
            if agent := _token_agent(_jwt_decode(auth[7:])):
                return agent
    return None


def _ws_agent_id(ws: WebSocket) -> str | None:
    """
    Resolve the connecting agent's identity for a WebSocket upgrade.

    As _dashboard_agent_id(), for WS: browsers cannot set headers on a WS
    handshake, so the token comes as a subprotocol (`_ws_token`), never a URL parameter:
      - Sec-WebSocket-Protocol: emporia.v1, emporia.token.<jwt>  → verified as the REST JWT is
      - otherwise     → None (unauthenticated). P261: a bare ?agent_id= proves nothing.
    """
    token = _ws_token(ws)
    if token:
        return _token_agent(_jwt_decode(token))
    return None


WS_SUBPROTOCOL = "emporia.v1"
_WS_TOKEN_PREFIX = "emporia.token."


def _ws_token(ws: WebSocket) -> str:
    """The bearer token a WebSocket handshake carries: in `Sec-WebSocket-Protocol`, as the offered subprotocol
    `emporia.token.<jwt>` beside `emporia.v1` (a browser sets no other header on a handshake). Never in the
    URL, where a proxy or a log would keep it."""
    for offered in str(ws.headers.get("sec-websocket-protocol", "")).split(","):
        offered = offered.strip()
        if offered.startswith(_WS_TOKEN_PREFIX):
            return offered[len(_WS_TOKEN_PREFIX):]
    return ""


async def _ws_accept(ws: WebSocket) -> None:
    """Accept, choosing `emporia.v1` when the client offered it (the token subprotocol is never echoed)."""
    offered = [p.strip() for p in str(ws.headers.get("sec-websocket-protocol", "")).split(",")]
    await ws.accept(subprotocol=WS_SUBPROTOCOL if WS_SUBPROTOCOL in offered else None)


def operator_stable_id() -> str:
    """The relay operator's **stable id** (`EMPORIA_OPERATOR_STABLE_ID`, set by `emporia relay` setup for the
    operator's agent): who the operator is, whatever name the agent has. "" when none is configured."""
    return os.getenv("EMPORIA_OPERATOR_STABLE_ID", "").strip().lower()


def _relay_operator_id() -> str:
    """The relay operator's name here: the registered agent whose stable id is the configured one, or ""
    when none is configured or the operator has not registered. The operator is a key, not a name:
    whoever registers the name `EMPORIA_AGENT_ID` first does not become it (that name is reserved, see
    `reserved_name_problem`), and every operator check goes through this one function."""
    stable = operator_stable_id()
    if not stable:
        return ""
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT agent_id FROM authorized_agents WHERE stable_id = ?", (stable,)).fetchone()
    return row["agent_id"] if row else ""


def house_stable_ids() -> frozenset[str]:
    """Stable ids allowed to take a house name (`EMPORIA_HOUSE_STABLE_IDS`, comma-separated)."""
    return frozenset(x.strip().lower() for x in os.getenv("EMPORIA_HOUSE_STABLE_IDS", "").split(",") if x.strip())


def house_names() -> frozenset[str]:
    """The house agents' names here: the registered agents whose stable id is in `EMPORIA_HOUSE_STABLE_IDS`.
    A house agent is a key, not a name, so a rename keeps it and nobody gains it by taking a name."""
    ids = sorted(house_stable_ids())
    if not ids:
        return frozenset()
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(f"SELECT agent_id FROM authorized_agents WHERE stable_id IN ({','.join('?' * len(ids))})",  # noqa: S608
                            ids).fetchall()
    return frozenset(r["agent_id"] for r in rows)


def vouched_stable_ids() -> frozenset[str]:
    """Stable ids the relay's operator vouches for (`EMPORIA_VOUCHED_STABLE_IDS`, comma-separated)."""
    return frozenset(x.strip().lower() for x in os.getenv("EMPORIA_VOUCHED_STABLE_IDS", "").split(",") if x.strip())


def vouched_names() -> frozenset[str]:
    """The vouched agents' names here: the registered agents whose stable id is in `EMPORIA_VOUCHED_STABLE_IDS`.
    Keyed by key like the house, so a rename keeps it and nobody gains it by taking a name."""
    ids = sorted(vouched_stable_ids())
    if not ids:
        return frozenset()
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(f"SELECT agent_id FROM authorized_agents WHERE stable_id IN ({','.join('?' * len(ids))})",  # noqa: S608
                            ids).fetchall()
    return frozenset(r["agent_id"] for r in rows)


def reserved_name_problem(agent_id: str, stable_id: str) -> str:
    """Why `agent_id` may not be registered by the key whose stable id is `stable_id`, or "": the operator's
    configured name (`EMPORIA_AGENT_ID`) belongs to the operator's key. Nobody else may
    take it, so a name on a fresh relay is never a way into its admin. (A house agent is a key in
    `EMPORIA_HOUSE_STABLE_IDS`, not a name, so no name is reserved for it.)"""
    operator = operator_stable_id()
    if not operator:
        return ""       # no operator is pinned: no name is anyone's to reserve, and nobody is the operator
    if stable_id == operator:
        return ""
    named = os.getenv("EMPORIA_AGENT_ID", "").strip()
    if named and agent_id == named:
        return (f"{agent_id!r} is this relay's reserved operator name: only the operator's key "
                "(EMPORIA_OPERATOR_STABLE_ID) may register it")
    return ""


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
# of posts with room to spare, small enough that a malicious or hijacked
# peer can't OOM the relay by returning an arbitrarily large body.
def _int_or(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


MAX_FEDERATION_RESPONSE_BYTES = _int_or(os.getenv("EMPORIA_MAX_FEDERATION_RESPONSE_BYTES"), 8 * 1024 * 1024)


def _own_host() -> str:
    from emporia.addresses import host_of
    return host_of(RELAY_BASE_URL)


def _clamp_limit(limit: int, max_limit: int = _MAX_LIMIT) -> int:
    """Bound a caller-supplied `limit` query param to [1, max_limit] (N3-6). Several
    LIMIT-bearing endpoints already did this inline as `min(limit, 200)`; the N3 review
    (O2) named 8 more that took `limit: int` with no cap at all, and applying this
    uniformly turned up 4 further uncapped sites the review's own count had missed
    (GET /rooms, GET /rooms/{id}/messages, GET /dm/{id}/messages) — an
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
    # P141: only a room a player (or the operator) linked decides it. Anyone
    # could link a room before, so an outsider's public room made a private
    # session — a paid deliverable — readable by everyone. Rows linked that way
    # before the fix are ignored here.
    if room and room.creator_id in (*session.get("participants", []), _relay_operator_id()):
        return "public" if room.room_type == "public" else "participants"
    try:
        module = get_interaction_module(session["module_type"])
    except ValueError:
        return "participants"  # unknown module type — fail closed, not open
    return module.DEFAULT_VISIBILITY


def _tempo_cfg() -> dict[str, Any] | None:
    from emporia import tempo
    return tempo.config()


def _escrow_network() -> str | None:
    from emporia import escrow_chain
    cfg = escrow_chain.load_config()
    return cfg.network if cfg else None


def session_paid_by(session_id: str) -> list[str]:
    """P153: the rails this session's payments were actually paid on ("stripe",
    "x402") — what a page shows as "paid by" once money has moved."""
    with DB_LOCK, get_db() as conn:
        rows = conn.execute("SELECT DISTINCT rail FROM payments WHERE session_id = ? "
                            "AND payment_type = 'session_price'", (session_id,)).fetchall()
    return sorted(r[0] for r in rows if r[0])


def session_sealed(session: dict[str, Any]) -> bool:
    """P149: a private session whose players have all published encryption
    keys — its free text (the module's PRIVATE_TEXT_FIELDS, and table talk)
    must then arrive sealed to them, so the relay holds ciphertext only. The
    same rule as DMs, encrypted by default once both keys exist (P122)."""
    if _session_visibility(session) != "participants":
        return False
    players = session.get("participants") or []
    return bool(players) and all(AGENT_REGISTRY.get_encryption_key(p)[0] for p in players)


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

class _Connection(sqlite3.Connection):
    """P266: `with get_db() as conn:` commits (or rolls back) *and closes*. A
    plain sqlite3 connection's `with` only ends the transaction, so every
    request left a connection for the garbage collector to close."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def get_db() -> sqlite3.Connection:
    # P266: WAL is a property of the database file, set once in init_db, not on
    # every connection. A writer in another process (the `emporia` command, a
    # second relay worker) makes this one wait a little rather than fail.
    conn = sqlite3.connect(DATABASE_PATH, factory=_Connection, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # P267: so a row that INSERT OR REPLACE pushes out fires its delete trigger
    # and its removal reaches the transparency log (SQLite skips it otherwise).
    # Safe: the log's triggers write only to translog_outbox, which has none.
    conn.execute("PRAGMA recursive_triggers = ON")
    return conn


def init_db() -> None:
    """Make the relay's tables (`schema_sql.SCHEMA_SQL`, one schema) and stamp its version. The relay starts
    fresh: a database from another schema version is refused by name, never upgraded."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, get_db() as conn:
        _assert_schema_version(conn)
        conn.execute("PRAGMA journal_mode=WAL")      # once; it stays with the file
        conn.executescript(SCHEMA_SQL)
        _load_session_floors(conn)      # P246
        _load_revocations(conn)
        translog.init_schema(conn)      # the log's own tables and triggers (P256)
        _stamp_schema_version(conn)


# P61: the schema's own version, stamped into the database header via
# `PRAGMA user_version`. Bump it whenever the schema changes — a test computes
# a fingerprint of every table and column and fails if the shape moved without
# this number moving, so it cannot quietly stop meaning anything. It lives in
# emporia.schema (P139) so `emporia doctor` can read it without the relay.
from emporia.schema import SCHEMA_VERSION  # noqa: E402
from emporia.schema_sql import SCHEMA_SQL  # noqa: E402


def _assert_schema_version(conn: Any) -> None:
    """Refuse a database written under another schema version, by name. The relay starts fresh: there is
    one schema and no way to carry an older database to it, so the old file is moved aside and the relay
    makes a new one (agents register again). A newer file is refused too, rather than served by a binary
    that does not know it."""
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
    if has_tables and found < SCHEMA_VERSION:
        raise RuntimeError(
            f"This relay starts fresh: its database is at schema version {found}, and this relay makes and "
            f"reads only version {SCHEMA_VERSION}. Move the old database aside ({DATABASE_PATH} and its "
            f"-wal/-shm files), then start the relay again; agents register on their next start.")
    if found > SCHEMA_VERSION:
        raise RuntimeError(
            f"This database was written by a newer Emporia relay: it is at schema version {found}, and this "
            f"relay understands only {SCHEMA_VERSION}. Run the newer relay, or move the database aside.")


def _stamp_schema_version(conn: Any) -> None:
    """Record the schema version. Interpolated rather than bound: `PRAGMA user_version = ?` is not a
    parameterised statement in SQLite, and `SCHEMA_VERSION` is an int constant."""
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


def _payment_metadata_matches(
    metadata: dict[str, Any],
    *,
    resource_type: str,
    resource_id: str,
) -> bool:
    return metadata.get("resource_type") == resource_type and metadata.get("resource_id") == resource_id


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
    pay for. Both PI-minting paths (`create_price_intent`, `confirm_spt`) already stamp
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
    cents: int | None = None,
) -> None:
    """Hold the facilitator's settlement result for the `PAYMENT-RESPONSE` header (P93).

    x402 v2's HTTP transport returns the outcome of a settlement to the payer
    in `PAYMENT-RESPONSE`: base64 JSON `{success, transaction, network, payer}`,
    plus `errorReason` when it failed, on the 200 and on the 402 alike. CORS
    has exposed the header since P1b; nothing ever set it, so an x402 client
    had no receipt for a payment that had moved its money. Recorded on
    `request.state` here and attached by `request_context`, so every paid
    route gets it without each one building its own response.

    Also the one place the relay logs the outcome as a named event for the operator
    (`payment_settled` / `payment_failed`: rail, network, cents when known; never the payer's
    address or the receipt).
    """
    if bool(settle_result.get("success")):
        logger.info("payment_settled", extra={"rail": "x402", "network": str(network or ""), "cents": cents})
    else:
        logger.warning("payment_failed", extra={
            "rail": "x402", "network": str(network or ""), "cents": cents,
            "reason": str(settle_result.get("errorReason") or "unknown")[:120]})
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
    pay_to_override: str | None = None,
    verify_only: bool = False,
) -> "PaymentRef | None":
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
    # `pay_to_override`: the relay's fee address, for the fee leg of a split entry (fees.py)
    pay_to = pay_to_override or (
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

    if verify_only:
        return None     # a split entry checks both payments before it settles either

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
                                    network=network or "", payer=authorizer, cents=amount_cents)
            return PaymentRef(RAIL_X402, settled)
        _reopen_x402_intent(
            intent_key=intent_key, network=network or "", asset=asset or "",
            authorizer=authorizer, nonce=nonce, valid_before=valid_before,
        )

    try:
        settle_result = await x402_settle_payment(x402_payload, expected)
        _record_x402_settlement(request, settle_result, network=network or "", payer=authorizer,
                                cents=amount_cents)
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


async def _pay_split_x402(
    request: Request, creator_payload: dict[str, Any], fee_payload: dict[str, Any] | None, *,
    creator_id: str | None, creator_cents: int, fee_cents: int, currency: str, challenge_resource: str,
    payer_id: str, resource_type: str, resource_id: str, fee_pay_to: dict[str, str], refuse,
) -> "PaymentRef | JSONResponse":
    """A paid entry: the creator's share and the relay's fee, two x402 payments in one action. Both are
    checked before either settles, and the entry is granted only when both have. The relay is the payee of
    neither's money in transit: each settles straight to its own address, so nothing is ever held."""
    from emporia.payments import RAIL_X402, PaymentRef
    if fee_payload is None:
        return refuse(f"this entry is two payments: {creator_cents}¢ to its creator and the relay's fee of "
                      f"{fee_cents}¢; the fee payment (PAYMENT-SIGNATURE-FEE) is missing")
    network = (creator_payload.get("accepted") or {}).get("network")
    fee_network = (fee_payload.get("accepted") or {}).get("network")
    if not network or network != fee_network:
        return refuse("the creator's payment and the fee payment must be on the same network")
    fee_to = fee_pay_to.get(network)
    if not fee_to:
        return refuse(f"the relay takes its fee on no address for {network}")
    fee_type = f"{resource_type}:fee"
    common = dict(currency=currency, challenge_resource=challenge_resource, payer_id=payer_id,
                  resource_id=resource_id, request=request)
    legs = (
        dict(x402_payload=creator_payload, creator_id=creator_id, amount_cents=creator_cents,
             resource_type=resource_type, **common),
        dict(x402_payload=fee_payload, creator_id=None, amount_cents=fee_cents, resource_type=fee_type,
             pay_to_override=fee_to, **common),
    )
    for leg in legs:     # free and non-mutating: nothing moves until both are right
        await _verify_x402_payment(verify_only=True, **leg)
    creator_ref = await _verify_x402_payment(**legs[0])
    fee_ref = await _verify_x402_payment(**legs[1])
    return PaymentRef(RAIL_X402, creator_ref.reference, fee_reference=fee_ref.reference, fee_cents=fee_cents)


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
    card_unavailable: str | None = None,
    x402_split: bool = False,
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

    `card_unavailable` (P157): why a card cannot pay here (the payee has no
    account a destination charge can reach, on a relay that will not hold the
    money itself). The 402 then offers no card, and a card credential is
    declined rather than charged.

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
    (see `docs/archive/roadmap-hackathon-era.md`'s composable-payment-options principle). This was a
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

    # A paid entry (`x402_split`) is two x402 payments in one act (fees.py): the creator's share to the
    # creator and the relay's fee to its published fee address. No fee address, no fee: one payment.
    from emporia import fees
    fee_cents = fees.fee_cents(amount_cents) if x402_split else 0
    creator_cents = amount_cents - fee_cents

    def _fee_pay_to_by_network() -> dict[str, str]:
        from emporia.payments import _x402_networks, x402_network_family
        found = fees.addresses()
        return {n: found[f] for n in _x402_networks() if (f := x402_network_family(n)) in found}

    def _x402_fee_offer() -> dict[str, Any] | None:
        if not fee_cents or not _x402_enabled():
            return None
        return build_x402_challenge(fee_cents, challenge_resource, _fee_pay_to_by_network(), currency=currency)

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
        pay_to = _x402_pay_to_by_network(creator_id)
        if fee_cents:
            # a network the fee cannot be paid on is not offered: the fee is not optional
            fee_networks = _fee_pay_to_by_network()
            pay_to = {n: a for n, a in pay_to.items() if n in fee_networks}
        return build_x402_challenge(creator_cents, challenge_resource, pay_to, currency=currency)

    def _tempo_offer() -> dict[str, Any] | None:
        """P156: a Tempo charge paid straight to the creator, or None."""
        from emporia import tempo
        cfg = tempo.config()
        # Config and currency first: no wallet read on a relay without Tempo (P4-1's rule).
        if not cfg or not creator_id or currency != "usd":
            return None
        wallet = AGENT_REGISTRY.get_x402_pay_to(creator_id, "evm")
        if not wallet:
            return None
        return tempo.build_offer(amount_cents=amount_cents, resource=challenge_resource,
                                 recipient=wallet, payer_id=agent_id,
                                 secret=load_or_generate_relay_jwt_secret(), cfg=cfg)

    def _no_credential_402(
        message: str, offer: dict[str, Any] | None, *, declined_reason: str | None = None,
    ) -> JSONResponse:
        challenge_headers = ({} if card_unavailable else
                             build_mpp_challenge(amount_cents, challenge_resource, currency=currency))
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
            if x402_split:
                # the fee, shown before any payment: who gets what, and the second payment's requirement
                content["terms"] = fees.terms(amount_cents)
                if fee_offer := _x402_fee_offer():
                    content["x402_fee"] = fee_offer["body"]
                    challenge_headers["PAYMENT-REQUIRED-FEE"] = fee_offer["headers"]["PAYMENT-REQUIRED"]
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
        if card_unavailable:
            content["card"] = {"offered": False, "reason": card_unavailable}
        tempo_offer = _tempo_offer()
        if tempo_offer:
            content["tempo"] = tempo_offer   # P156
        if (offer or tempo_offer) and creator_id:
            # P262: who is paid, in their own signed words — the payer checks it.
            payee = AGENT_REGISTRY.payee_statement(creator_id)
            content["payee"] = payee
            if tempo_offer:
                tempo_offer["payee"] = payee
        return JSONResponse(status_code=402, content=content, headers=challenge_headers)

    tempo_credential = request.headers.get("PAYMENT-TEMPO")
    if tempo_credential:
        # P156: the payer chose Tempo — verify on chain, or decline plainly.
        from emporia import tempo
        cfg = tempo.config()
        wallet = AGENT_REGISTRY.get_x402_pay_to(creator_id, "evm") if creator_id else None
        if not cfg or not wallet or currency != "usd":
            return _no_credential_402(f"Tempo payment declined: not offered here. {challenge_message}", None)
        try:
            tx_hash = await asyncio.to_thread(
                tempo.verify, tempo_credential, payer_id=agent_id, resource=challenge_resource,
                amount_cents=amount_cents, recipient=wallet,
                secret=load_or_generate_relay_jwt_secret(), cfg=cfg)
        except tempo.TempoError as e:
            return _no_credential_402(f"Tempo payment not accepted: {e}. {challenge_message}", None)
        from emporia.payments import RAIL_TEMPO
        return PaymentRef(reference=tx_hash, rail=RAIL_TEMPO)

    x402_payload = extract_x402_payload(request.headers.get("PAYMENT-SIGNATURE"))
    if x402_payload is not None:
        offer = _x402_offer()
        if offer and fee_cents:
            return await _pay_split_x402(
                request, x402_payload, extract_x402_payload(request.headers.get("PAYMENT-SIGNATURE-FEE")),
                creator_id=creator_id, creator_cents=creator_cents, fee_cents=fee_cents,
                currency=currency, challenge_resource=challenge_resource, payer_id=agent_id,
                resource_type=resource_type, resource_id=resource_id,
                fee_pay_to=_fee_pay_to_by_network(),
                refuse=lambda why: _no_credential_402(f"x402 payment declined: {why}. {challenge_message}", offer,
                                                      declined_reason="fee_payment"),
            )
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

    if card_unavailable and (spt_token or req_payment_intent_id):
        resp = _no_credential_402(f"Card payment declined: {card_unavailable}. {challenge_message}",
                                  _x402_offer())
        body = json.loads(resp.body)
        body["declined"] = {"rail": "stripe", "reason": "no_payee_account"}
        return JSONResponse(status_code=402, content=body,
                            headers={k: v for k, v in resp.headers.items() if k.lower() != "content-length"})

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
            return PaymentRef(RAIL_STRIPE, result["payment_intent_id"], destination_account)
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
            return PaymentRef(RAIL_STRIPE, req_payment_intent_id, destination_account)
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
        cur = conn.execute(
            "INSERT INTO payments "
            "(payment_id, session_id, room_id, agent_id, amount_cents, currency, "
            "payment_intent_id, payment_type, status, created_at, rail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            (payment_id, session_id, room_id, agent_id, amount_cents, currency,
             payment_intent_id, payment_type, created_at, rail),
        )
        # P256: in the log now, as a commitment: who paid whom is not public
        translog.log_row(conn, "payment", cur.lastrowid)
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
    out = []
    for r in rows:
        try:
            out.append(_session_row_to_dict(r))
        except ValueError as e:     # a damaged row is skipped and logged, never the whole sweep
            logger.warning("deadline_sweep_unreadable_session",
                           extra={"session_id": r["session_id"], "module_type": module_type, "error": str(e)})
    return out


def idle_sessions(before: str, exclude_module: str, limit: int = 200) -> list[dict[str, Any]]:
    """Waiting or active sessions untouched since `before`, oldest first, other
    than `exclude_module` (P129's idle sweep; service sessions have deadlines)."""
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            # A purchase waiting for its funding: a half-funded one holds money until the sweep voids it.
            "SELECT * FROM sessions WHERE status IN ('waiting', 'active', 'awaiting_funding') "
            "AND updated_at < ? "
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
    """A held price went back to its payer (P130). Only from `confirmed`, so a
    payment already refunded or captured keeps what it says."""
    with DB_LOCK, get_db() as conn:
        conn.execute("UPDATE payments SET status = 'released' WHERE payment_intent_id = ? "
                     "AND status = 'confirmed'", (payment_intent_id,))


def get_session_payments(session_id: str) -> list[dict[str, Any]]:
    with DB_LOCK, get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE session_id = ? AND payment_type = 'session_price'",
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
        cur = conn.execute(
            "INSERT INTO settlements "
            "(settlement_id, session_id, winner_id, total_price_cents, platform_fee_cents, "
            "winner_payout_cents, platform_fee_bps, payment_intent_ids, "
            "transfer_id, transfer_status, status, created_at, payer_id, dedupe_key, rail, "
            "platform_fee_atomic, winner_payout_atomic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(dedupe_key) DO NOTHING",
            (
                settlement_id,
                session_id,
                settlement.get("winner_id"),
                settlement.get("total_price_cents", 0),
                settlement.get("platform_fee_cents", 0),
                settlement.get("winner_payout_cents", 0),
                settlement.get("platform_fee_bps", _fee_bps_now()),
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
        if cur.rowcount:
            translog.log_row(conn, "settlement", cur.lastrowid)
    # x402 already logged its own outcome (with the network) when the facilitator settled it
    if cur.rowcount and rail != "x402":
        status = settlement.get("transfer_status", "pending_connect")
        logger.log(
            logging.WARNING if status == "transfer_failed" else logging.INFO,
            "payment_failed" if status == "transfer_failed" else "payment_settled",
            extra={"rail": rail, "network": "", "cents": settlement.get("total_price_cents", 0),
                   "transfer_status": status},
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
# Deliberately NOT built here (see docs/archive/roadmap-hackathon-era.md): a stable content-addressed id distinct
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
    ("game_chat", "agent_id"),
    ("escrow_holds", "payer_agent_id"), ("escrow_holds", "receiver_agent_id"),
    ("access_removals", "removed_by"), ("actions", "agent_id"),
    ("agent_inbox", "agent_id"),
    ("agora_invites", "agent_id"), ("agora_invites", "invited_by"),
    ("agora_members", "agent_id"),
    ("agora_topics", "creator_id"),
    ("dm_messages", "sender_id"),
    ("dm_threads", "agent_a"), ("dm_threads", "agent_b"), ("dm_threads", "local_agent"),
    ("deletions", "deleted_by"),
    ("messages", "from_agent"), ("messages", "to_agent"),
    ("participants", "agent_id"), ("payments", "agent_id"),
    ("session_leaves", "agent_id"), ("dm_blocks", "agent_id"), ("dm_blocks", "blocked"),   # P273
    ("pending_digest", "agent_id"), ("pending_digest", "actor"), ("game_watches", "agent_id"),
    ("follow_notices", "agent_id"), ("follow_notices", "by_agent"),
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
    # P243: who did a signed room act, and to whom. The signed payload beside
    # them is a record and keeps the handle it was signed with.
    ("room_acts", "by_agent"), ("room_acts", "target"),
    # P278: the same for a topic's signed acts.
    ("topic_acts", "by_agent"), ("topic_acts", "target"),
    ("authorized_agents", "agent_id"),
    # P297: who liked, and whose thing it was. The signature beside them keeps the handle it was signed with.
    ("reactions", "agent_id"), ("reactions", "to_agent"),
    # Feed posts and follows: the author, the follower and the followed. A pulled pointer's author is an
    # address, which no local rename matches. The signatures keep the stable id or handle signed with.
    ("posts", "author_id"), ("follows", "agent_id"), ("follows", "target"),
    # Pledges: who promised, to whom, and who attested or contested. The signatures keep the handles signed with.
    ("pledges", "maker_id"), ("pledges", "to_agent"), ("pledge_acts", "agent_id"),
    # Labs v2: who made a lab and which maintainer signed a report. The signatures keep the stable ids signed with.
    ("labs", "creator_id"), ("lab_reports", "maintainer_id"),
    # emporia:rand:v1: whose commitment, and who won. The signature keeps the handle it was signed with.
    ("rand_commits", "agent_id"), ("rand_results", "winner"),
)

# Agent ids inside JSON arrays. Not rewritable in SQL, and deliberately not
# done with a string replace: `"alice"` is a substring of `"alice_bot"`, so a
# textual swap would corrupt a neighbour's membership. Parsed, mapped, re-dumped.
_RENAME_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rooms", "members_json"),
    ("sessions", "participants_json"),
)

# Every remaining column, declared not to hold an agent handle. Notable
# judgements: `authorized_agents.operator_label` is an opaque operator label,
# not a handle; `access_*.resource_id` is a room or topic id.
_NON_AGENT_COLUMNS: frozenset[tuple[str, str]] = frozenset({
    # The protocol pass: a post's thread keys (its signed reply_to and root without author hints), whether it is a
    # copy of another relay's record, its expiration tag; a room message's signed stable id; a session's kind.
    ("posts", "reply_key"), ("posts", "root_key"), ("posts", "is_copy"), ("posts", "expires_at"),
    ("room_messages", "sender_stable_id"), ("sessions", "kind"),
    # P297: a like's thing (a post, comment, message or session id), its kind, when, and its signature.
    *(("reactions", c) for c in ("target", "kind", "target_id", "value", "active", "at", "signature")),
    # Feed posts: the signed content, its id, origin and key; follows: the signed act; opened_from: which
    # object a room or topic was opened from.
    *(("posts", c) for c in ("post_id", "author_stable_id", "author_public_key_hex", "origin_relay",
                             "federated_from", "nonce", "kind", "label", "text", "tags_json", "topic",
                             "reply_to", "quote_of", "root", "at", "signature", "key_log_json", "is_deleted",
                             "recorded_at")),
    *(("follows", c) for c in ("following", "at", "signature")),
    # Notices: what is waiting to be batched (a kind, what happened, a clipped title, when), the games an agent
    # watches, and when it was last told of a follower.
    *(("pending_digest", c) for c in ("kind", "ref", "label", "at")),
    ("game_watches", "session_id"), ("game_watches", "at"), ("follow_notices", "at"),
    # The signed card's bio: the agent's own text.
    ("authorized_agents", "about"),
    # The card's X link: named by stable id (a rename changes nothing), handles and post ids are text.
    *(("agent_links", c) for c in ("stable_id", "site", "handle", "post_id", "claim", "at", "signature", "recorded_at")),
    *(("x_confirmations", c) for c in ("seq", "confirmer", "target", "handle", "post_id", "claim_sig", "seen_at",
                                       "signature", "recorded_at")),
    *(("opened_from", c) for c in ("kind", "ident", "from_post", "reply_post_id", "at")),
    # Labs v2: the idea posts a report names, credited once each (the author is read from the post).
    *(("lab_report_ideas", c) for c in ("post_id", "report_id", "lab_slug")),
    # The randomness service: a cached drand round, its signature and when it was fetched.
    *(("beacon_rounds", c) for c in ("round", "signature", "fetched_at")),
    *(("rand_commits", c) for c in ("session_id", "seat", "hash", "beacon_round", "at", "signature")),
    *(("rand_results", c) for c in ("session_id", "beacon_round", "beacon_signature", "termination",
                                    "outcome_hash", "recorded_at")),
    # Season board: a season's frozen final standing and the relay's signature over it.
    *(("season_finals", c) for c in ("season", "body_json", "signature", "frozen_at")),
    # Pledges: what was promised on which session, its terms and note, and the signed acts on it.
    *(("pledges", c) for c in ("pledge_id", "kind", "session_id", "terms_json", "note", "nonce",
                               "made_at", "signature", "public", "recorded_at")),
    *(("pledge_acts", c) for c in ("pledge_id", "act", "verdict", "at", "signature", "recorded_at")),
    # Labs v2: a lab's terms (stable ids are what is signed) and a maintainer's report with the claims it
    # embeds. Reports are public in every lab, even a private or encrypted one: refs and agents only.
    *(("labs", c) for c in ("slug", "name", "room_id", "subject_kind", "subject_ref", "repo_url", "branch",
                            "proof_commit", "creator_stable", "maintainers_json", "licence", "verified",
                            "proof_json", "checked_at", "signed_at", "signature", "created_at")),
    *(("lab_reports", c) for c in ("report_id", "lab_slug", "ref_kind", "ref_id", "event", "commit_sha",
                                   "author_claim", "author_stable", "credited_json", "reviewers_json", "ideas_json", "note",
                                   "signed_at", "signature", "created_at")),
    # Identity round: an operator's label history, and the token revocation records, are keyed by stable id.
    *(("model_commits", c) for c in ("session_id", "step", "stable_id", "model_commit")),
    *(("model_reveals", c) for c in ("session_id", "step", "stable_id", "model", "salt", "status", "at", "signature")),
    *(("operator_log", c) for c in ("stable_id", "previous", "label", "recorded_at")),
    *(("token_revocations", c) for c in ("jti", "stable_id", "revoked_at", "expires_at")),
    *(("token_floors", c) for c in ("stable_id", "valid_after")),
    # escrow_holds (P99): the on-chain record of a hold. Addresses are wallets,
    # not handles, and the PaymentInfo they sit in is what the payer signed —
    # rewriting either would make the hold uncapturable.
    ("escrow_holds", "hold_hash"), ("escrow_holds", "session_id"),
    ("game_chat", "message_id"), ("game_chat", "session_id"), ("game_chat", "content"),
    ("game_chat", "created_at"),
    ("escrow_holds", "payer_address"), ("escrow_holds", "receiver_address"),
    ("escrow_holds", "network"), ("escrow_holds", "amount_atomic"),
    ("escrow_holds", "payment_info_json"), ("escrow_holds", "state"),
    ("escrow_holds", "authorize_tx"), ("escrow_holds", "capture_tx"),
    ("escrow_holds", "void_tx"), ("escrow_holds", "created_at"),
    ("escrow_holds", "updated_at"),
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
    # agora_invites
    ("agora_invites", "topic_id"), ("agora_invites", "created_at"),
    # agora_members
    ("agora_members", "topic_id"), ("agora_members", "role"), ("agora_members", "joined_at"),
    # agora_topics
    ("agora_topics", "topic_id"), ("agora_topics", "slug"), ("agora_topics", "name"),
    ("agora_topics", "description"), ("agora_topics", "visibility"),
    ("agora_topics", "gate_type"), ("agora_topics", "entry_fee_cents"),
    ("agora_topics", "created_at"), ("agora_topics", "post_count"),
    ("agora_topics", "subscriber_count"), ("agora_topics", "flair_options"),
    ("agora_topics", "access_duration_days"), ("agora_topics", "encrypted"),
    # authorized_agents
    ("authorized_agents", "public_key_hex"), ("authorized_agents", "display_name"),
    ("authorized_agents", "registered_at"), ("authorized_agents", "is_active"),
    ("authorized_agents", "stripe_account_id"), ("authorized_agents", "operator_label"),
    ("authorized_agents", "client_name"), ("authorized_agents", "client_version"),
    ("authorized_agents", "declared_model"), ("authorized_agents", "declared_model_at"),
    ("authorized_agents", "x402_pay_to_json"), ("authorized_agents", "x402_pay_to_proofs_json"),
    ("authorized_agents", "encryption_public_key"),
    ("authorized_agents", "encryption_key_signature"),
    ("authorized_agents", "stripe_account_status"),
    ("authorized_agents", "stripe_account_mode"), ("authorized_agents", "stable_id"),
    # dm_messages
    ("dm_messages", "message_id"), ("dm_messages", "thread_id"), ("dm_messages", "content"),
    ("dm_messages", "msg_type"), ("dm_messages", "created_at"), ("dm_messages", "signature"),
    ("dm_messages", "nonce"), ("dm_messages", "chain_hash"),
    # dm_threads
    ("dm_threads", "thread_id"), ("dm_threads", "created_at"),
    ("dm_threads", "last_message_at"), ("dm_threads", "encrypted"),
    # P185: the other side of a cross-relay thread — an address on another
    # relay, never a handle here, so a rename here does not rewrite it.
    ("dm_threads", "remote_address"), ("dm_threads", "peer_url"), ("dm_threads", "started_by_local"),
    ("remote_agents", "address"), ("remote_agents", "peer_url"), ("remote_agents", "public_key_hex"),
    ("remote_agents", "encryption_public_key"), ("remote_agents", "encryption_key_signature"),
    ("remote_agents", "updated_at"), ("authorized_agents", "accepts_remote_dms"),
    ("session_offers", "session_id"), ("session_offers", "stable_id"),
    ("session_offers", "public_key_hex"), ("session_offers", "signature"),
    ("session_offers", "offered_at"),
    ("agent_key_log", "stable_id"), ("agent_key_log", "step_no"), ("agent_key_log", "old_key"),
    ("agent_key_log", "new_key"), ("agent_key_log", "at"), ("agent_key_log", "signed_by"),
    ("agent_key_log", "signature"),
    ("agent_key_log", "commitment"), ("agent_key_log", "recovery_public_key"),
    ("agent_key_log", "compromised_since"), ("agent_key_log", "recorded_at"),
    ("agent_key_log_superseded", "stable_id"), ("agent_key_log_superseded", "seq"),
    ("agent_key_log_superseded", "step_json"), ("agent_key_log_superseded", "superseded_at"),
    ("remote_agents", "stable_id"), ("remote_agents", "key_log_json"),
    ("authorized_agents", "encryption_key_at"), ("remote_agents", "encryption_key_at"),   # P271: times
    ("participants", "join_at"), ("participants", "join_signature"),                      # P272
    ("session_leaves", "session_id"), ("session_leaves", "at"), ("session_leaves", "signature"),   # P273
    ("session_leaves", "left_at"), ("dm_blocks", "created_at"), ("dm_blocks", "blocked_stable_id"),
    # P256: the transparency log is append-only and never rewritten — a rename
    # changing a leaf would break every proof. Its records name agents by stable_id.
    ("translog_leaves", "idx"), ("translog_leaves", "ref_key"), ("translog_leaves", "record"),
    ("translog_states", "size"), ("translog_states", "root"), ("translog_states", "at"),
    ("translog_states", "relay_public_key"), ("translog_states", "signature"),
    ("translog_nodes", "level"), ("translog_nodes", "idx"), ("translog_nodes", "hash"),
    ("translog_seen", "key"), ("translog_seen", "digest"),
    # P269: one row per log kept; states and hand-overs name the relay's key, never an agent
    ("translog_states", "epoch"), ("translog_epochs", "epoch"), ("translog_epochs", "started_at"),
    ("translog_epochs", "reason"), ("translog_epochs", "previous"), ("translog_epochs", "handover"),
    # P263/P268: the log's outbox — row keys and room/thread ids waiting to be folded in (a key can hold
    # an agent_id, but the outbox is drained within minutes and a rename queues the old and new keys)
    ("translog_outbox", "seq"), ("translog_outbox", "kind"), ("translog_outbox", "ref"),
    ("translog_outbox", "backfill"),
    ("room_acts", "act_id"), ("room_acts", "room_id"), ("room_acts", "act"), ("room_acts", "at"),
    ("room_acts", "nonce"), ("room_acts", "payload_json"), ("room_acts", "signature"),
    ("topic_acts", "act_id"), ("topic_acts", "topic_id"), ("topic_acts", "act"), ("topic_acts", "at"),
    ("topic_acts", "nonce"), ("topic_acts", "payload_json"), ("topic_acts", "signature"),
    ("authorized_agents", "relay_list_json"), ("authorized_agents", "carried_records_json"),
    ("authorized_agents", "network_hash"), ("authorized_agents", "network_group"), ("relay_secrets", "name"), ("relay_secrets", "value"),
    ("deletions", "kind"), ("deletions", "item"), ("deletions", "role"), ("deletions", "deleted_at"),
    ("deletions", "signature"), ("deletions", "stable_id"), ("agora_topics", "is_deleted"), ("room_messages", "deleted_at"),
    ("dm_messages", "deleted_at"), ("game_chat", "deleted_at"),
    ("room_messages", "reply_to"), ("room_messages", "root"), ("game_chat", "kind"), ("game_chat", "tags_json"),
    ("game_chat", "reply_to"), ("game_chat", "quote_of"), ("game_chat", "root"), ("game_chat", "step"),
    # messages
    ("messages", "message_id"), ("messages", "session_id"), ("messages", "type"),
    ("messages", "payload_json"), ("messages", "signature"), ("messages", "created_at"),
    # participants
    ("participants", "session_id"), ("participants", "joined_at"),
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
    ("rooms", "linked_session_salt"),
    # sessions
    ("sessions", "session_id"), ("sessions", "module_type"), ("sessions", "config_json"),
    ("sessions", "payment_rules_json"), ("sessions", "state_json"), ("sessions", "step_number"),
    ("sessions", "status"), ("sessions", "created_at"), ("sessions", "updated_at"),
    # settlements
    ("settlements", "settlement_id"), ("settlements", "session_id"),
    ("settlements", "total_price_cents"), ("settlements", "platform_fee_cents"),
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
    # Names a person would trust on sight: the house, the tools, the host, the makers.
    "house", "bot", "hermes", "nous", "moderator", "mod", "staff", "team", "help", "owner", "founder",
    "bank", "wallet", "escrow", "treasury", "signer", "relay-operator", "emporia-bot", "hermes-agent",
})

# What a client may call itself on the signed card: `wire.normalize_client` (a host name, `custom:<name>`, `custom`).


def _clean_display_name(display_name: str) -> str:
    """The display name as stored (see `names.clean_display_name`); a ValueError names what is wrong."""
    return _names.clean_display_name(display_name)


def _handle_lookalike_problem(conn: Any, agent_id: str) -> str:
    """Why a new handle is too close to a reserved name, this relay's own names or an existing handle, or ""."""
    reserved = set(RESERVED_AGENT_IDS)
    for name in (os.getenv("EMPORIA_AGENT_ID", "").strip(), *_house_names(), _own_host().split(".")[0]):
        if name:
            reserved.add(name)
    existing = [r["agent_id"] for r in conn.execute("SELECT agent_id FROM authorized_agents").fetchall()]
    return _names.lookalike_problem(agent_id, existing, reserved)


def _house_names() -> list[str]:
    return sorted(house_names())


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
    public key (P47): SHA-256 with a `v2` label since P294 (`wire.stable_id_for`).

    `agent_id` was three things at once — primary key, display handle, and
    identity — which works right up until any one of them has to change. This
    separates the third out.

    Why the pubkey and not the alternatives docs/archive/roadmap-hackathon-era.md proposed:

    * a content address over the name (the hackathon's `content_address_for`,
      removed in P294) changes on rename: it addresses a named thing, a
      different job.
    * the operator label is one-to-many by design (one person runs many agents),
      so it cannot identify an agent.

    The pubkey was already the ownership proof — registration requires one and
    refuses a taken handle presented under a different key. This writes it down.

    Derived so it is independently verifiable from public material; **stored**
    thereafter (see `AgentRegistry.register`) so a key rotation preserves
    identity rather than renaming the agent for doing the right thing. Hex is
    lowercased before hashing: the same key written two ways is one key.
    """
    from emporia.wire import stable_id_for as _stable_id_for
    return _stable_id_for(public_key_hex)


def agent_key_log(conn: Any, stable_id: str | None) -> list[dict[str, str]]:
    """P245: an agent's key log as this relay holds it — its signed steps, oldest
    first, in the form `wire.key_log_windows` reads. [] for an agent that never
    changed key. P246: a step is a key change, a commitment to a recovery key or
    a recovery."""
    if not stable_id:
        return []
    out = []
    for r in conn.execute("SELECT * FROM agent_key_log WHERE stable_id = ? ORDER BY step_no",
                          (stable_id,)).fetchall():
        if r["signed_by"] == "commit":
            out.append({"by": "commit", "commitment": r["commitment"], "at": r["at"], "signature": r["signature"]})
        elif r["signed_by"] == "recovery":
            out.append({"by": "recovery", "old": r["old_key"], "new": r["new_key"],
                        "recovery_public_key": r["recovery_public_key"],
                        "compromised_since": r["compromised_since"], "at": r["at"], "signature": r["signature"]})
        else:
            out.append({"old": r["old_key"], "new": r["new_key"], "at": r["at"], "by": r["signed_by"],
                        "signature": r["signature"]})
    return out


def agent_key_log_superseded(conn: Any, stable_id: str | None) -> list[dict[str, Any]]:
    """P246: the steps a recovery undid, as they were signed, oldest first."""
    if not stable_id:
        return []
    return [json.loads(r["step_json"]) for r in conn.execute(
        "SELECT step_json FROM agent_key_log_superseded WHERE stable_id = ? ORDER BY seq", (stable_id,)).fetchall()]


def _write_key_log_step(conn: Any, stable_id: str, step_no: int, step: dict[str, Any]) -> None:
    cur = conn.execute(
        "INSERT INTO agent_key_log (stable_id, step_no, old_key, new_key, at, signed_by, signature, commitment, "
        "recovery_public_key, compromised_since, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (stable_id, step_no, step.get("old", ""), step.get("new", ""), step["at"], step.get("by", "key"),
         step["signature"], step.get("commitment", ""), step.get("recovery_public_key", ""),
         step.get("compromised_since", ""), time.time()))
    # P256: every key step, in the transparency log, in the same transaction.
    # A recovery deletes the steps it undoes from agent_key_log; their leaves stay.
    translog.log_row(conn, "agent_key", cur.lastrowid)


#: P246: when each agent's last recovery (P267: or key change) was recorded, by
#: handle. A bearer token issued before then was issued to a key it no longer has.
_SESSION_FLOOR: dict[str, float] = {}

# `routes.standing._who`'s cache; emptied by `forget_who` when an agent registers, renames or changes label.
_WHO_CACHE: dict[str, Any] = {}


def forget_who() -> None:
    _WHO_CACHE.clear()


def _load_session_floors(conn: Any) -> None:
    _SESSION_FLOOR.clear()
    for r in conn.execute(
            "SELECT a.agent_id, MAX(l.recorded_at) AS t FROM authorized_agents a JOIN agent_key_log l "
            "ON l.stable_id = a.stable_id WHERE l.signed_by IN ('recovery', 'key') GROUP BY a.agent_id").fetchall():
        _SESSION_FLOOR[r["agent_id"]] = r["t"]


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
        operator_label: str = "",
        client_name: str = "",
        client_version: str = "",
        about: str = "",
    ) -> dict:
        """Register an agent. Returns its profile dict (stable id, operator label, declared client)."""
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

        operator_label = (operator_label or "").strip().lower()
        if operator_label and not operators.valid_label(operator_label):
            raise ValueError(f"operator label must look like {operators.LABEL_EXAMPLE}")
        from emporia.wire import normalize_client
        client_name = normalize_client(client_name)
        client_version = (client_version or "").strip()[:32]
        from emporia.wire import about_problem
        if why := about_problem(about):
            raise ValueError(why)
        verified_display_name = _clean_display_name(display_name)

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
                    if why := _handle_lookalike_problem(conn, agent_id):
                        raise ValueError(why)
                    # P245: also a key some agent now holds after rotating — its
                    # stable id is from an earlier key, so it would not match above.
                    twin = conn.execute(
                        "SELECT agent_id FROM authorized_agents WHERE stable_id = ? OR public_key_hex = ?",
                        (derived_stable_id, public_key_hex),
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
                now_iso = datetime.now(UTC).isoformat()
                before = conn.execute("SELECT operator_label FROM authorized_agents WHERE agent_id = ?",
                                      (agent_id,)).fetchone()
                conn.execute(
                    "INSERT INTO authorized_agents "
                    "(agent_id, public_key_hex, display_name, registered_at, is_active, "
                    "stripe_account_id, operator_label, client_name, client_version, stable_id, about) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
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
                    # P26-5: COALESCE keeps an existing Stripe account id whenever this registration
                    # didn't mint one (a live-mode re-register writes NULL, and the next onboarding
                    # would otherwise create a second real Connected Account).
                    "stripe_account_id=COALESCE(excluded.stripe_account_id, authorized_agents.stripe_account_id), "
                    # The signed card's label and client: a registration with none keeps what the row has
                    # (a change of label is logged below, never silent).
                    "operator_label=CASE WHEN excluded.operator_label = '' THEN authorized_agents.operator_label "
                    "ELSE excluded.operator_label END, "
                    "client_name=CASE WHEN excluded.client_name = '' THEN authorized_agents.client_name "
                    "ELSE excluded.client_name END, "
                    "client_version=CASE WHEN excluded.client_name = '' THEN authorized_agents.client_version "
                    "ELSE excluded.client_version END, "
                    # the card is the agent's own word: a registration that carries no bio clears it
                    "about=excluded.about",
                    (agent_id, public_key_hex, verified_display_name, now_iso, stripe_account_id,
                     operator_label, client_name, client_version, derived_stable_id, about or ""),
                )
                stored = conn.execute(
                    "SELECT stable_id, operator_label, client_name, client_version FROM authorized_agents "
                    "WHERE agent_id = ?", (agent_id,),
                ).fetchone()
                stable_id = stored["stable_id"]
                previous = (before["operator_label"] or "") if before else ""
                forget_who()
                if (stored["operator_label"] or "") != previous or (not before and stored["operator_label"]):
                    conn.execute(
                        "INSERT INTO operator_log (stable_id, previous, label, recorded_at) VALUES (?,?,?,?)",
                        (stable_id, previous, stored["operator_label"] or "", now_iso))
            return {
                "agent_id": agent_id,
                # P47: read back rather than returned from `derived_stable_id`.
                # On a re-register those two differ whenever the row was created
                # before this key — which is exactly the case (rotation, an
                # upgraded legacy row) where returning the freshly derived value
                # would report an identity the database does not hold.
                "stable_id": stable_id,
                "display_name": verified_display_name,
                "about": about or "",
                "operator_label": stored["operator_label"] or "",
                "client": {"name": stored["client_name"] or "", "version": stored["client_version"] or ""},
            }
        except ValueError:
            raise  # key-conflict or validation errors — preserve message for HTTP status mapping
        except Exception as e:
            raise ValueError(f"Registration failed: {e}") from e

    def rotate_key(self, agent_id: str, new_public_key_hex: str, step: dict) -> dict:
        """Replace an agent's Ed25519 signing key, keeping its identity (P47),
        and write the step to its key log (P245).

        Not reachable through `register()` by design: that path refuses a taken
        handle presented under a different key, and it has to — that refusal is
        what makes a handle ownable at all. Rotation is therefore its own
        operation, and the caller (`POST /agents/{id}/key-rotations`) is
        responsible for having checked the step: signed by the *current* key,
        fresh, and later than the step before.

        `stable_id` is untouched. That is the point: an agent that rotates its
        key for good reasons must not be renamed by the relay for doing so.
        What was signed with the old key stays checkable: anyone can read the
        log and ask which key was the agent's when something was signed
        (`wire.key_at`).

        The new key must be nobody's: not any agent's current key, not the key
        any agent's identity was made from, and not one this agent already
        held — the same one-key-one-identity rule registration enforces.
        """
        try:
            if len(bytes.fromhex(new_public_key_hex)) != 32:
                raise ValueError()
        except ValueError:
            raise ValueError("public_key_hex must be a 64-char hex string (Ed25519 public key)")
        new_public_key_hex = new_public_key_hex.lower()

        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT stable_id, public_key_hex FROM authorized_agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Agent not found: {agent_id}")
            if row["public_key_hex"] != step["old"] or step["new"] != new_public_key_hex \
                    or step["old"] == step["new"]:
                raise ValueError("that step does not replace this agent's current key")
            twin = conn.execute(
                "SELECT agent_id FROM authorized_agents WHERE (stable_id = ? OR public_key_hex = ?) "
                "AND agent_id != ?",
                (stable_id_for(new_public_key_hex), new_public_key_hex, agent_id),
            ).fetchone()
            if twin:
                raise ValueError(f"That key is already registered as '{twin['agent_id']}'")
            log = agent_key_log(conn, row["stable_id"])
            held = {k for s in [*log, *agent_key_log_superseded(conn, row["stable_id"])]
                    for k in (s.get("old"), s.get("new")) if k}
            if new_public_key_hex in held or stable_id_for(new_public_key_hex) == row["stable_id"]:
                raise ValueError("this agent already held that key")
            _write_key_log_step(conn, row["stable_id"], len(log), step)
            conn.execute(
                "UPDATE authorized_agents SET public_key_hex = ? WHERE agent_id = ?",
                (new_public_key_hex, agent_id),
            )
            # P267: a key changed because the old one may have leaked takes the
            # old key's sign-ins with it, as a recovery does (P246).
            _SESSION_FLOOR[agent_id] = time.time()
        return {"agent_id": agent_id, "stable_id": row["stable_id"],
                "public_key_hex": new_public_key_hex}

    def add_recovery_commitment(self, agent_id: str, step: dict) -> list[dict]:
        """P246: write a commitment step (signed by the agent's current key) to its
        key log, and return the log. Refused unless the whole log with it is valid:
        which also means no commitment is live, so a thief with the key cannot
        replace the owner's, and none repeats an earlier one."""
        from emporia import wire
        with DB_LOCK, get_db() as conn:
            row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
                               (agent_id,)).fetchone()
            if not row:
                raise ValueError(f"Agent not found: {agent_id}")
            log = agent_key_log(conn, row["stable_id"])
            if wire.live_commitment(log):
                raise ValueError("this agent already has a recovery commitment; a recovery spends it")
            if not wire.key_log_windows(row["stable_id"], [*log, step], verify, row["public_key_hex"]):
                raise ValueError("this commitment does not fit the agent's key log (not signed by its current "
                                 "key, or one used before)")
            _write_key_log_step(conn, row["stable_id"], len(log), step)
        return [*log, step]

    def recover_key(self, agent_id: str, step: dict) -> dict:
        """P246: give an agent the new key a recovery step names, and rewrite its key
        log. The step is signed by the recovery key the agent committed to, so
        nothing here depends on the key being replaced. The log with the step
        must be valid (`wire.key_log_state`); the key changes made at or after
        its `compromised_since` are moved to `agent_key_log_superseded`. What the
        old key published (encryption key, relay list) is cleared: the agent
        signs it again with the new one. Bearer tokens issued so far stop working."""
        from emporia import wire
        with DB_LOCK, get_db() as conn:
            row = conn.execute("SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
                               (agent_id,)).fetchone()
            if not row:
                raise ValueError(f"Agent not found: {agent_id}")
            sid, new = row["stable_id"], step["new"]
            log = agent_key_log(conn, sid)
            if not wire.key_log_windows(sid, [*log, step], verify, row["public_key_hex"]):
                raise ValueError("this recovery does not follow from the agent's key log: it needs a live "
                                 "commitment that this recovery key matches, `old` must be the key the agent had "
                                 "just before `compromised_since`, and the key must be new to this agent")
            since = wire.when_of(step["compromised_since"])
            kept = [x for x in log if wire.when_of(x["at"]) < since]
            undone = log[len(kept):]
            rewritten = [*kept, step]
            windows = wire.key_log_windows(sid, rewritten, verify, new)
            if not windows or windows[-1]["key"] != new:
                raise ValueError("the key log would not lead from the agent's stable id to the new key")
            twin = conn.execute(
                "SELECT agent_id FROM authorized_agents WHERE (stable_id = ? OR public_key_hex = ?) "
                "AND agent_id != ?", (stable_id_for(new), new, agent_id)).fetchone()
            if twin:
                raise ValueError(f"That key is already registered as '{twin['agent_id']}'")
            held = {k for x in agent_key_log_superseded(conn, sid) for k in (x.get("old"), x.get("new")) if k}
            if new in held:
                raise ValueError("this agent already held that key")
            n = conn.execute("SELECT COUNT(*) FROM agent_key_log_superseded WHERE stable_id = ?", (sid,)).fetchone()[0]
            for i, x in enumerate(undone):
                conn.execute("INSERT INTO agent_key_log_superseded (stable_id, seq, step_json, superseded_at) "
                             "VALUES (?,?,?,?)", (sid, n + i, json.dumps(x), step["at"]))
            conn.execute("DELETE FROM agent_key_log WHERE stable_id = ? AND step_no >= ?", (sid, len(kept)))
            _write_key_log_step(conn, sid, len(kept), step)
            conn.execute(
                "UPDATE authorized_agents SET public_key_hex = ?, encryption_public_key = '', "
                "encryption_key_signature = '', encryption_key_at = '', relay_list_json = NULL WHERE agent_id = ?",
                (new, agent_id))
            _SESSION_FLOOR[agent_id] = time.time()
        return {"agent_id": agent_id, "stable_id": sid, "public_key_hex": new, "key_log": rewritten,
                "superseded": undone}

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
        with DB_LOCK, get_db() as _conn:
            if why := _handle_lookalike_problem(_conn, new_agent_id):
                if not _names.skeleton(new_agent_id) == _names.skeleton(agent_id):    # renaming to a spelling of itself is fine
                    raise ValueError(why)

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
            forget_who()
            if agent_id in _SESSION_FLOOR:      # P246: a recovery's cut-off goes with the agent
                _SESSION_FLOOR[new_agent_id] = _SESSION_FLOOR.pop(agent_id)

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

    def set_x402_pay_to(self, agent_id: str, network_family: str, address: str,
                        proof: dict[str, str] | None = None) -> None:
        """P262: `proof` is the agent's `{at, signature}` over the wallet
        (`wire.signed_payout_wallet_payload`) — what payers check. The route
        always passes one; a wallet stored without one is offered, and every
        payer refuses to pay it."""
        with DB_LOCK, get_db() as conn:
            row = conn.execute(
                "SELECT x402_pay_to_json, x402_pay_to_proofs_json FROM authorized_agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            wallets = json.loads(row["x402_pay_to_json"] or "{}") if row else {}
            proofs = json.loads(row["x402_pay_to_proofs_json"] or "{}") if row else {}
            wallets[network_family] = address
            if proof:
                proofs[network_family] = {"address": address, "at": proof["at"], "signature": proof["signature"]}
            else:
                proofs.pop(network_family, None)
            conn.execute(
                "UPDATE authorized_agents SET x402_pay_to_json = ?, x402_pay_to_proofs_json = ? WHERE agent_id = ?",
                (json.dumps(wallets), json.dumps(proofs), agent_id),
            )

    def payee_statement(self, agent_id: str) -> dict[str, Any] | None:
        """P262: who an offer pays, in the payee's own words — its signed payout
        wallets, its keys and key log — for the payer to check
        (`payee.check`). None for an unknown agent."""
        try:
            with DB_LOCK, get_db() as conn:
                row = conn.execute(
                    "SELECT agent_id, public_key_hex, stable_id, x402_pay_to_proofs_json FROM authorized_agents "
                    "WHERE agent_id = ?", (agent_id,)).fetchone()
                if not row:
                    return None
                log = agent_key_log(conn, row["stable_id"])
            proofs = json.loads(row["x402_pay_to_proofs_json"] or "{}")
        except Exception:
            return None
        return {"agent_id": row["agent_id"], "stable_id": row["stable_id"] or "",
                "public_key_hex": row["public_key_hex"], "key_log": log,
                "wallets": proofs if isinstance(proofs, dict) else {}}

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

    def set_encryption_key(self, agent_id: str, public_key_hex: str, signature: str, at: str = "") -> None:
        with DB_LOCK, get_db() as conn:
            conn.execute(
                "UPDATE authorized_agents SET encryption_public_key = ?, "
                "encryption_key_signature = ?, encryption_key_at = ? WHERE agent_id = ?",
                (public_key_hex, signature, at, agent_id),
            )

    def encryption_key_at(self, agent_id: str) -> str:
        """P271: when the agent signed its published encryption key's binding ('' for v1)."""
        with DB_LOCK, get_db() as conn:
            row = conn.execute("SELECT encryption_key_at FROM authorized_agents WHERE agent_id = ?",
                               (agent_id,)).fetchone()
        return (row["encryption_key_at"] or "") if row else ""

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

    def check_rate_limit(self, agent_id: str, limit: int | None = None) -> bool:
        now = time.time()
        minute_ago = now - 60
        self.rate_limits[agent_id] = [t for t in self.rate_limits[agent_id] if t > minute_ago]
        if len(self.rate_limits[agent_id]) >= (limit or self.max_per_minute):
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


#: An operator's agents together may write this many times one agent's per-minute budget (EMPORIA_OPERATOR_RATE_MULTIPLE).
OPERATOR_RATE_MULTIPLE = max(1.0, float(os.getenv("EMPORIA_OPERATOR_RATE_MULTIPLE", "3") or 3))


def _operator_key(agent_id: str) -> str:
    """The operator of `agent_id` (`operators.operator_of`), "" when it is not registered."""
    try:       # the cached grouping every other route uses (label or key, merged by registration network)
        from emporia.routes.standing import _who
        who = _who().get(agent_id)
    except Exception:  # noqa: BLE001 — a throttle that cannot look up an operator still throttles the agent
        return ""
    return who.operator if who else ""


def _enforce_agent_rate_limit(agent_id: str) -> None:
    """Per-agent write throttle (N11-4). The identity-keyed complement to the global
    IP middleware below — that middleware exempts localhost and the Docker gateway
    (_RATE_LIMIT_EXEMPT_HOSTS), which is the *default co-located deployment's caller*,
    so IP throttling alone enforces nothing there. Call immediately after resolving
    and validating `agent_id` as the authenticated caller, before any write. Raises
    429 if the agent has exceeded AGENT_REGISTRY.max_per_minute (RATE_LIMIT_PER_MINUTE)
    writes in the trailing 60s.
    """
    path = (request_info_ctx.get() or {}).get("path", "")
    if not AGENT_REGISTRY.check_rate_limit(agent_id):
        RATE_LIMIT_HITS["agent"] += 1
        logger.warning("rate_limited", extra={"limit": "agent", "per_minute": AGENT_REGISTRY.max_per_minute,
                                              "agent_id": agent_id, "path": path})
        raise HTTPException(429, "Rate limit exceeded — too many requests, slow down")
    # per operator as well: an operator's agents together get a few agents' worth, not a budget each
    operator = _operator_key(agent_id)
    if operator and not AGENT_REGISTRY.check_rate_limit(
            f"operator:{operator}", int(AGENT_REGISTRY.max_per_minute * OPERATOR_RATE_MULTIPLE)):
        RATE_LIMIT_HITS["agent"] += 1
        logger.warning("rate_limited", extra={"limit": "operator", "agent_id": agent_id, "path": path})
        raise HTTPException(429, "Rate limit exceeded for your operator's agents together — slow down")


#: Operator section: how often a caller was turned away by a rate limit since this relay
#: started — per IP (the middleware) and per agent (writes). Counts only, process-lifetime,
#: for the operator's health card (GET /operator/status); who was throttled is in the log.
RATE_LIMIT_HITS: dict[str, int] = {"ip": 0, "agent": 0}


# Live counters for the dashboard's Trust & Safety panel — process-lifetime,
# reset on relay restart. Not persisted; this is a demo signal, not an audit log
# (the hash-chained audit log in session_audit.py is the durable record).
SAFETY_STATS: dict[str, int] = {
    "guardrail_blocks": 0,
    "por_rejections": 0,
    "unsigned_actions_rejected": 0,
}


def _require_valid_msg_type(msg_type: str, sealed: bool = False) -> None:
    """The `msg_type` slot of a room message or DM (rooms v2): a post kind, or a room-only kind
    (`code`, `lab_task`), or `sealed` where the place is end-to-end encrypted -- and then nothing else.
    Call with the *resolved* value (after `or DEFAULT_MSG_TYPE`). "system" is server-owned, never a
    client's. The old types (`chat`, `collab`, the negotiation ones) are refused by name, saying what
    replaced them (`room_post.type_problem`)."""
    from emporia import room_post
    if why := room_post.type_problem(msg_type, sealed=sealed):
        raise HTTPException(400, why)


async def _assert_payload_safe_counted(payload: dict[str, Any]) -> None:
    """Wrap guardrails.assert_payload_safe_async to count blocks for the
    dashboard.

    P21-5 (B5): also the one chokepoint every scanning route already passes its
    whole payload through, so the per-field content-size bound
    (_require_payload_content_within_limit, checked before the scan — cheap
    should reject before expensive) now lives here instead of being enumerated
    by hand at up to 17 call sites, three of which had no bound at all."""
    _require_payload_content_within_limit(payload)
    try:
        await assert_payload_safe_async(payload)
    except GuardrailBlocked as e:
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
        "kind": row["kind"] if "kind" in row.keys() else "game",
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
            "current_agent, step_number, status, participants_json, created_at, updated_at, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, module_type, json.dumps(config), json.dumps(payment_rules),
             json.dumps(state.to_dict()), current_agent, step_number, status,
             json.dumps(participants), created_at, updated_at, session_kind(module_type)),
        )


def load_session(session_id: str) -> dict[str, Any] | None:
    with DB_LOCK, get_db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _session_row_to_dict(row) if row else None


def add_participant(session_id: str, agent_id: str,
                    join_at: str = "", join_signature: str = "") -> None:
    with DB_LOCK, get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO participants (session_id, agent_id, joined_at, join_at, "
            "join_signature) VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_id, datetime.now(UTC).isoformat(), join_at, join_signature),
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
    # P232: the live frame carries its inbox id, so whoever hands it to the
    # agent can mark it read; without it every event pushed live stayed unread
    # for ever (alpha and beta each held over 100 turn notices).
    inbox_id = f"ibx_{uuid.uuid4().hex[:12]}"
    dead: set[WebSocket] = set()
    for ws in ws_connections.get(key, set()):
        try:
            await ws.send_json({**message, "inbox_id": inbox_id})
        except Exception:
            dead.add(ws)
    for ws in dead:
        ws_connections[key].discard(ws)
    # Persist to inbox so agents can poll even without a WS connection
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

_PROCESS_LOCK: Any = None


def _hold_the_database() -> None:
    """P271: one relay process per database. Rate windows, sign-in floors,
    WebSocket registries and the log's signer live in this process's memory, so
    a second worker (`--workers 2`) or a second relay on the same file would
    split them silently: limits halved, a rotated key's old tokens still good in
    the other process. The lock is the file's, held for the process's life."""
    global _PROCESS_LOCK
    if _PROCESS_LOCK is not None:
        return
    try:
        import fcntl
    except ImportError:            # not POSIX: nothing to hold
        return
    path = DATABASE_PATH.with_name(DATABASE_PATH.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a")       # noqa: SIM115 - held until the process ends
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f"another relay process is already serving {DATABASE_PATH} — a relay runs as one "
                           "process (its limits and sign-ins live in memory): stop the other, or do not "
                           "start it with more than one worker") from None
    _PROCESS_LOCK = handle


class RelayModulesWrong(RuntimeError):
    """P291: the relay's modules do not hold together; it does not start that way."""


def _check_relay_modules() -> None:
    from emporia import relay_modules
    wrong = relay_modules.problems(FEDERATED_RELAYS)
    if wrong:
        raise RelayModulesWrong("this relay's modules are set up wrong — fix these, then start it again:\n  "
                                + "\n  ".join(wrong))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_relay_modules()   # P291: before anything is opened or started
    _hold_the_database()
    init_db()
    notice = _stripe_mpp_admin_notice()
    if notice:
        logger.warning("%s", notice)
    _require_stripe_webhook_secret_if_needed()
    global _SWEEP_TASK, _FEDERATION_SYNC_TASK, _PAYOUT_RECONCILE_TASK
    _SWEEP_TASK = asyncio.create_task(_sweep_loop())
    if translog.enabled():   # P258: EMPORIA_TRANSPARENCY_LOG=off turns it off
        globals()["_LOG_STATE_TASK"] = asyncio.create_task(_log_state_loop())
    else:
        _warn_if_log_left_off()
    # P51: money the relay owes a creator but never moved. Runs unconditionally
    # — unlike the federation loop, which is gated on peers being configured,
    # there is no deployment where "a payout could be owed" is knowably false.
    _PAYOUT_RECONCILE_TASK = asyncio.create_task(_payout_reconcile_loop())
    # P23-3: only started when peers are actually configured -- a standalone
    # relay (the common case) shouldn't spin a task that does nothing every
    # EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS forever.
    drop_unlinked_federated_rows()
    from emporia.routes.rooms import ask_unsigned_members_to_sign
    if told := ask_unsigned_members_to_sign():   # P275: members from before signed joins
        logger.info("room_members_asked_to_sign", extra={"count": told})
    from emporia import relay_access
    if FEDERATED_RELAYS and relay_access.federates():
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
        _log_task = globals().get("_LOG_STATE_TASK")
        if _log_task is not None:
            _log_task.cancel()
            with suppress(asyncio.CancelledError):
                await _log_task
            globals()["_LOG_STATE_TASK"] = None
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
# Per client address, whatever the route and whoever (or nobody) is signed in, so the
# unauthenticated ones are covered: registration challenge, sign-in, /health, public reads.
# Settings: EMPORIA_IP_RATE_{READ,WRITE,AUTH}_PER_MINUTE.
_RATE_LIMITS: dict[str, tuple[int, float]] = {
    "default":   (_int_or(os.getenv("EMPORIA_IP_RATE_READ_PER_MINUTE"), 120), 60.0),    # GET/HEAD
    "auth":      (_int_or(os.getenv("EMPORIA_IP_RATE_AUTH_PER_MINUTE"), 10), 60.0),     # challenge, register, sign-in, key recovery
    "write":     (_int_or(os.getenv("EMPORIA_IP_RATE_WRITE_PER_MINUTE"), 60), 60.0),    # POST/PUT/DELETE
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
    # Localhost, operator-listed proxy IPs, and test client: no rate limiting. Exempting an
    # IP from throttling grants nothing to act on; no address is trusted with an identity (P261).
    if ip in _RATE_LIMIT_EXEMPT_HOSTS or ip == "testclient":
        return None
    path = request.url.path
    # P246: the key-recovery route takes no credential but a signature, so it is throttled like sign-in.
    bucket = "auth" if path in _AUTH_PATHS or path.endswith("/key-recovery") else ("write" if request.method not in ("GET", "HEAD") else "default")
    limit, window = _RATE_LIMITS[bucket]
    now = time.time()
    key = (ip, bucket)
    q = _RATE_WINDOWS[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        RATE_LIMIT_HITS["ip"] += 1
        logger.warning(
            "rate_limited", extra={"limit": f"ip_{bucket}", "per_minute": limit, "ip_tag": ip_tag(ip),
                                   "path": path},
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
MAX_REQUEST_BYTES = _int_or(os.getenv("EMPORIA_MAX_REQUEST_BYTES"), 1024 * 1024)


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
    unconditionally scans its whole payload gets this for free — including
    the federation import.

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
# P270: open WebSockets per client address. The HTTP rate limiter never sees a
# WebSocket (it is an "http" middleware), and /ws/events and public sessions
# need no sign-in, so without this one address could hold sockets open until
# the relay ran out of them. Counted after uvicorn's forwarded-address handling,
# so behind the hosted relay's proxy it is each client, not the proxy.
#
# Three caps, each a relay setting: per address, per signed-in agent (a token in the
# handshake, as `_ws_agent_id` reads it) and for the whole relay. The settings were
# read with a doubled prefix until now, so the documented one never applied.
_WS_PER_ADDRESS = _int_or(os.getenv("EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS"), 64)
_WS_PER_AGENT = _int_or(os.getenv("EMPORIA_MAX_WEBSOCKETS_PER_AGENT"), 16)
_WS_TOTAL = _int_or(os.getenv("EMPORIA_MAX_WEBSOCKETS"), 256)
_WS_OPEN: dict[str, int] = defaultdict(int)
_WS_OPEN_AGENT: dict[str, int] = defaultdict(int)
_WS_OPEN_TOTAL = [0]


def _release(counts: dict[str, int], key: str) -> None:
    counts[key] -= 1
    if counts[key] <= 0:
        counts.pop(key, None)


class WebSocketLimit:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            return await self.inner(scope, receive, send)
        ip = (scope.get("client") or ("unknown", 0))[0]
        if ip in _RATE_LIMIT_EXEMPT_HOSTS or ip == "testclient":
            return await self.inner(scope, receive, send)
        try:
            agent = _ws_agent_id(WebSocket(scope, receive, send)) or ""
        except Exception:
            agent = ""
        over = ("address", _WS_OPEN[ip], _WS_PER_ADDRESS) if _WS_OPEN[ip] >= _WS_PER_ADDRESS else (
            ("agent", _WS_OPEN_AGENT[agent], _WS_PER_AGENT) if agent and _WS_OPEN_AGENT[agent] >= _WS_PER_AGENT else (
                ("relay", _WS_OPEN_TOTAL[0], _WS_TOTAL) if _WS_OPEN_TOTAL[0] >= _WS_TOTAL else None))
        if over:
            await receive()                                   # the connect, then refuse it
            logger.warning("ws_refused_too_many", extra={"cap": over[0], "open": over[1], "limit": over[2]})
            await send({"type": "websocket.close", "code": 1013})   # try again later
            return
        _WS_OPEN[ip] += 1
        _WS_OPEN_TOTAL[0] += 1
        if agent:
            _WS_OPEN_AGENT[agent] += 1
        try:
            await self.inner(scope, receive, send)
        finally:
            _release(_WS_OPEN, ip)
            _WS_OPEN_TOTAL[0] -= 1
            if agent:
                _release(_WS_OPEN_AGENT, agent)


app.add_middleware(WebSocketLimit)

# P271: since P261 nothing reads the relay from a browser (the console and the
# Hermes tab call it from their own process), so only its own URL by default;
# a dashboard served elsewhere (or `npm run dev`) is added with EMPORIA_CORS_ORIGINS.
_CORS_ORIGINS = {RELAY_BASE_URL}
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
    allow_headers=["Content-Type", "Authorization", "PAYMENT-SIGNATURE", "PAYMENT-SIGNATURE-FEE",
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
async def relay_access_guard(request: Request, call_next):
    """P186: a read-only or closed relay serves its members only (emporia.relay_access)."""
    from emporia import relay_access
    if relay_access.mode() != "open":
        why = relay_access.refusal(request.method, request.url.path, _dashboard_agent_id(request))
        if why:
            return JSONResponse({"detail": why}, status_code=403)
    return await call_next(request)


async def _ws_refused(ws: WebSocket) -> bool:
    """P186: the access mode, for a WebSocket — closed before it is accepted."""
    from emporia import relay_access
    if relay_access.mode() != "open" and relay_access.refusal("GET", ws.url.path, _ws_agent_id(ws)):
        await ws.close(code=1008)
        return True
    return False


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    token = request_id_ctx.set(rid)
    info: dict = {"path": request.url.path, "expected": False}
    info_token = request_info_ctx.set(info)
    start = time.monotonic()
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            # `unhandled_error` is the line `emporia relay watch` counts as a server error (the 500
            # itself is built by Starlette outside this middleware, so it has no request line).
            logger.exception(
                "unhandled_error",
                extra={"method": request.method, "path": request.url.path,
                       "exception_type": type(exc).__name__,
                       "client_ip": request.client.host if request.client else "unknown"},
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
        fields = {
            "method": request.method, "path": request.url.path,
            "status_code": response.status_code, "duration_ms": duration_ms,
            "client_ip": request.client.host if request.client else "unknown",
        }
        if info["expected"]:
            fields["expected"] = True
        logger.log(level, "request", extra=fields)
        response.headers["X-Request-Id"] = rid
        return response
    finally:
        request_info_ctx.reset(info_token)
        request_id_ctx.reset(token)


# Behind a front door (Cloudflare Tunnel, Caddy) the client is whoever the trusted proxy
# says, so every per-address limit above sees it. Registered last: it wraps outermost.
# Off unless both settings are given — see emporia/proxy_ip.py.
CLIENT_IP_HEADER = os.getenv("EMPORIA_CLIENT_IP_HEADER", "").strip().lower()
try:
    TRUSTED_PROXIES = _proxy_ip.parse_trusted(os.getenv("EMPORIA_TRUSTED_PROXIES", ""))
except ValueError as _e:
    raise SystemExit(f"emporia relay: EMPORIA_TRUSTED_PROXIES is not a list of addresses or networks: {_e}")
if CLIENT_IP_HEADER and CLIENT_IP_HEADER not in _proxy_ip.HEADERS:
    raise SystemExit(f"emporia relay: EMPORIA_CLIENT_IP_HEADER is one of {', '.join(_proxy_ip.HEADERS)}")
if CLIENT_IP_HEADER and not TRUSTED_PROXIES:
    raise SystemExit("emporia relay: EMPORIA_CLIENT_IP_HEADER needs EMPORIA_TRUSTED_PROXIES — the proxy "
                     "address(es) whose header is believed; a header from anyone else would be a claim")
if CLIENT_IP_HEADER:
    app.add_middleware(_proxy_ip.ClientIPFromProxy, header=CLIENT_IP_HEADER, trusted=TRUSTED_PROXIES)


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
    "evicted": {"rate_windows": 0, "ws_connections": 0, "agent_rate_limits": 0},
}


def _sweep_expired_state() -> dict[str, int]:
    """Evict empty/expired keys from every unbounded in-memory dict. Pure and
    synchronous so it can be tested directly without running the async loop;
    returns counts removed per dict for that purpose."""
    now = time.time()
    removed = {"rate_windows": 0, "ws_connections": 0, "agent_rate_limits": 0}

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


# P256: how often the transparency log's state is signed — every
# EMPORIA_LOG_STATE_INTERVAL_SECONDS, or sooner once EMPORIA_LOG_STATE_ENTRIES
# leaves are waiting, and only when the log grew.
_LOG_STATE_INTERVAL_SECONDS = float(os.getenv("EMPORIA_LOG_STATE_INTERVAL_SECONDS", "300"))
_LOG_STATE_ENTRIES = int(os.getenv("EMPORIA_LOG_STATE_ENTRIES", "1000"))
_LOG_STATE_TASK: asyncio.Task | None = None


def _warn_if_log_left_off() -> str:
    """P281: the log is off by default now. A relay that kept one under the old
    default, and never set EMPORIA_TRANSPARENCY_LOG, would stop it without a
    word, and agents that kept its states would see it gone. Say so, loudly."""
    try:
        with DB_LOCK, get_db() as conn:
            size = conn.execute("SELECT MAX(size) FROM translog_states").fetchone()[0]
            # a log rebuilt on upgrade (P269) keeps its last signed state in the epoch it handed over to
            prev = conn.execute("SELECT previous FROM translog_epochs WHERE previous IS NOT NULL "
                                "ORDER BY epoch DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return ""
    if not size and prev:
        try:
            size = json.loads(prev[0]).get("size")
        except (ValueError, AttributeError):
            size = None
    if not size:
        return ""
    row = (size,)
    notice = (f"this relay kept a transparency log ({row[0]} entries signed) but it is off now: since P281 the "
              "log is off unless EMPORIA_TRANSPARENCY_LOG=on. Set it (emporia setup log on) to keep it; "
              "agents and linked relays that check it will see it gone until then")
    logger.warning("transparency_log_left_off", extra={"signed_size": row[0], "detail": notice})
    return notice


def sign_log_state() -> dict[str, Any] | None:
    """Fold what changed into the log and sign its state. P263: the outbox is
    drained in batches, each its own short transaction, so a large backlog (a
    relay's first fold) never holds the database for long."""
    from emporia import identity
    if not translog.enabled():
        return None
    while True:
        with DB_LOCK, get_db() as conn:
            if translog.drain(conn) < translog.DRAIN_BATCH:
                break
    with DB_LOCK, get_db() as conn:
        state = translog.sign_state(conn, identity.sign_raw_relay, identity.get_relay_public_key_hex(),
                                    identity.relay_key_history())
    if state:
        logger.info("log_state_signed", extra={"size": state["size"], "root": state["root"]})
    return state


async def _log_state_loop() -> None:
    last = time.monotonic()
    while True:
        await asyncio.sleep(min(30.0, _LOG_STATE_INTERVAL_SECONDS))
        try:
            with DB_LOCK, get_db() as conn:
                held = translog.latest_state(conn)
                waiting = translog.size(conn) - (held["size"] if held else 0) + translog.waiting(conn)
            if waiting >= _LOG_STATE_ENTRIES or time.monotonic() - last >= _LOG_STATE_INTERVAL_SECONDS:
                await asyncio.to_thread(sign_log_state)
                last = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Signing the log state failed — will retry")


def _expire_dm_content() -> int:
    """P276: with EMPORIA_DM_RETENTION_DAYS set, erase DM content past it, in
    batches of 500 per sweep (`deletion.expire_dms`)."""
    from emporia import deletion
    days = deletion.dm_retention_days()
    if not days:
        return 0
    with DB_LOCK, get_db() as conn:
        gone = deletion.expire_dms(conn, days, _relay_operator_id() or "relay")
    if gone:
        logger.info("dm_content_expired", extra={"count": gone, "days": days})
    return gone


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
            _expire_dm_content()
            from emporia import notices
            await notices.flush_digests()     # the batched follow / topic notices that are due
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

class RegisterAgentRequest(BaseModel):
    agent_id: str
    public_key_hex: str
    display_name: str = ""
    operator_label: str = ""       # the opaque operator label on the signed card ("" = none)
    client_name: str = ""          # "hermes-plugin" | "sdk" | "custom"
    client_version: str = ""
    about: str = ""                # the agent's own line about itself on the signed card ("" = none)
    challenge_id: str = ""
    challenge_signature: str = ""  # base64url Ed25519 sig of emporia:sign-in:v2 {nonce, relay, agent_id, public_key}
    relay: str = ""                # the origin the agent signed for (it must be one of this relay's)
    # P16: publish the X25519 encryption key in the same round trip. Optional —
    # an agent that doesn't do encrypted DMs sends neither field.
    encryption_public_key_hex: str = ""
    encryption_key_signature: str = ""
    encryption_key_at: str = ""      # P271: the v2 binding's time


class SetX402WalletRequest(BaseModel):
    network_family: str  # "evm" | "svm"
    address: str
    at: str           # P262: near this relay's clock
    signature: str    # Ed25519, by the agent's key, over wire.signed_payout_wallet_payload()


class SetEncryptionKeyRequest(BaseModel):
    encryption_public_key_hex: str
    signature: str  # Ed25519 over wire.signed_encryption_key_payload()
    at: str = ""    # P271: the v2 binding's time; a changed key needs one newer than the last


class CreateSessionRequest(BaseModel):
    module_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    payment_rules: dict[str, Any] | None = None
    creator_gateway_url: str = ""
    # P279: signed by the creator — the game's id is wire.session_id_for(nonce),
    # the signature over wire.signed_session_create_payload(that id, creator, module, at).
    nonce: str = ""
    at: str = ""
    signature: str = ""
    # emporia:rand:v1: the creator's signed commitment, for a game that draws from the beacon.
    rand_commit: dict[str, Any] | None = None


class JoinSessionRequest(BaseModel):
    agent_id: str
    agent_gateway_url: str = ""
    at: str = ""          # P272: the join, signed by the joining agent
    signature: str = ""   # Ed25519 over wire.signed_session_join_payload()
    # emporia:rand:v1: in a game that draws from the beacon, the joiner's signed commitment
    # {hash, beacon_round, at, signature} over wire.signed_rand_commit_payload(...).
    rand_commit: dict[str, Any] | None = None


class SubmitActionRequest(BaseModel):
    agent_id: str
    action_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    peer_text_rationale: str = ""
    signature: str | None = None
    # P148: optional table talk sent with the move (signed with it).
    say: str = ""
    # A declared model, committed with the move (`model_commit.commit(model, salt)`), revealed at the end.
    model_commit: str = ""
    # tags of the `say` line (the declared model), signed with the move; none for a sealed line
    say_tags: list[list[str]] = Field(default_factory=list)


#: What POST /messages carries: an offer and its answers, from one agent to another (relay audit 11-11, F1).
NEGOTIATION_MESSAGE_TYPES = frozenset({"challenge", "counter_offer", "accept", "reject"})


class SendMessageRequest(BaseModel):
    to_agent: str
    session_id: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signature: str | None = None


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
    # P277: the creator's salt for the signed commitment to that session (wire.linked_session_commitment).
    linked_session_salt: str = ""
    # P2-3: how long one payment's access lasts for a paid room. None
    # (default) = no expiry — access lasts as long as the room does. Ignored for
    # non-paid rooms (membership there was never entitlement-gated).
    access_duration_days: int | None = None
    # P243: signed by the creator over every setting (wire.signed_room_payload).
    nonce: str
    signature: str


class JoinRoomRequest(BaseModel):
    agent_id: str
    payment_intent_id: str | None = None  # required for paid gate
    at: str = ""          # P273: the join, signed by the joining agent
    signature: str = ""   # Ed25519 over wire.signed_room_join_payload()


class InviteToRoomRequest(BaseModel):
    invitee_id: str
    # P243: signed by the room's creator (wire.signed_room_invite_payload).
    at: str
    signature: str


class SendRoomMessageRequest(BaseModel):
    content: str
    msg_type: str = ""                # a post kind | code | lab_task, or "sealed" in an encrypted room
    signature: str | None = None
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
    # P243: signed by the room's creator (wire.signed_room_kick_payload).
    at: str
    signature: str


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
    Tempo switched on, `/health` advertised `"mpp"` (its
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
async def health(request: Request):
    from emporia import relay_access
    access = relay_access.mode()
    if access == "closed" and (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        # P186: a closed relay says only that it is up — not what it runs or takes.
        # P240: and its own URL, which its members sign their posts for.
        return {"status": "ok", "service": BRAND, "access": access, "relay_url": RELAY_BASE_URL}
    session_count = 0
    try:
        with DB_LOCK, get_db() as conn:
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
    from emporia import deletion
    from emporia.payments import _x402_enabled, _x402_facilitator_url, _x402_networks
    return {
        "status": "ok",
        "service": BRAND,
        "access": access,
        # P187: the host in this relay's agents' addresses (alpha@host).
        "host": _own_host(),
        # P240: this relay's own URL — what an agent signs as a post's origin.
        "relay_url": RELAY_BASE_URL,
        "relay_id": relay_id(),
        "version": __version__,
        # P139: lets `emporia doctor` tell a relay running older code than the
        # checkout (restart it) from one that is current.
        "schema_version": SCHEMA_VERSION,
        # P291: what it runs beyond the core, the same field as on its card
        "relay_modules": _relay_modules(),
        # P276: DM content older than this many days is erased here (0: kept) — archive before then.
        "dm_retention_days": deletion.dm_retention_days(),
        # P144: the key the relay signs action receipts and federation batches
        # with — agents pin it at first sight to check receipts against.
        "relay_public_key": get_relay_public_key_hex(),
        # P191: each earlier key handing over to the next, signed by the one
        # handing over — how a peer that pinned an old key follows to this one.
        "relay_key_history": relay_key_history(),
        # P153: where a purchase can be held in USDC escrow (None: card only),
        # so a page can say what it accepts before anyone pays.
        "x402_escrow_network": _escrow_network(),
        # P156: where entries can be paid in Tempo stablecoin, straight to the creator.
        "tempo_network": (_tempo_cfg() or {}).get("network"),
        "modules": list(MODULE_REGISTRY.keys()),
        "session_count": session_count,
        "guardrails_mode": guardrails.DEFAULT_MODE,
        # First run: what a new agent's `emporia doctor` explains — the season this relay runs and
        # whether starter limits bind new agents (levels.py).
        "season": _season_public(),
        "starter_limits": _starter_limits_on(),
        **_fees_card(),
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
        # P208: for this machine's doctor — whether its keys guard real money,
        # and whether it runs in a container (who could reach them differs).
        "live_money": live_money(),
        "in_container": in_container(),
        "stripe_profile_ready": stripe_profile_ready,
        "stripe_profile_id": _stripe_profile_id(),
        "stripe_api_version": os.getenv("STRIPE_API_VERSION", "2026-04-22.preview"),
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
        # P160: whether cards are taken on games (P159) and whether this relay
        # holds card money at all (P157), so a client says what is accepted.
        "custodial_cards": custodial_cards_allowed(),
    }


def _fee_bps_now() -> int:
    from emporia import fees
    return fees.fee_bps()


def _fees_card() -> dict:
    """`fee_bps`, `fee_address`: the fee on paid entries (fees.py), the same for everyone."""
    from emporia import fees
    return fees.card()


def _season_public() -> dict:
    from emporia import progression
    return progression.season()


def _starter_limits_on() -> bool:
    from emporia import levels
    return levels.starter_limits_on()


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


@app.get("/operator/overview")
async def operator_overview(request: Request):
    """P179: the relay operator's view of their relay — every setting with how
    to change it, and activity as aggregates (counts and sums; never a message
    or an address). Operator only. See relay_admin."""
    from emporia import relay_admin
    _require_operator(_dashboard_agent_id(request), "operator overview")

    def collect() -> dict[str, Any]:
        with DB_LOCK, get_db() as conn:
            stats = relay_admin.stats(conn)
        return {"settings": relay_admin.settings(), "stats": stats}
    return await asyncio.to_thread(collect)


def _relay_guardians() -> dict | None:
    from emporia.identity import relay_guardians
    return relay_guardians()


def _relay_modules() -> dict[str, dict]:
    """P291: the modules this relay runs and their parts — card and /health alike."""
    from emporia import relay_modules
    return relay_modules.advertise(FEDERATED_RELAYS)


#: The card's `version` is the federation protocol version. 3 (Feed v2): pointer posts replaced listings and
#: adverts in the relay-to-relay sync. A linked relay on 2 or less is refused by name (see _old_protocol).
CARD_VERSION = 3


def _old_protocol(card: Any) -> str:
    """Why a peer's card names a protocol too old to sync with, or ""."""
    v = card.get("version") if isinstance(card, dict) else None
    if isinstance(v, int) and v < CARD_VERSION:
        return (f"it runs relay protocol {v}, which shared listings and adverts; this relay shares pointer posts "
                f"(emporia:post:v2, protocol {CARD_VERSION}). Upgrade it to sync")
    return ""


@app.get("/relay/card")
async def relay_identity_card(request: Request):
    """P286: who this relay is, signed with its key — what an agent card is for an agent.
    Cached and passed on by agents, peers and directories, and checkable by anyone
    (`identity.verify_relay_card`): the URL is where it is, the key and id are who it is.
    A closed relay shows it only on this machine, as /health does."""
    from emporia import relay_access
    access = relay_access.mode()
    if access == "closed" and (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(404, "closed relay")
    card = {
        "kind": "emporia-relay-card", "version": CARD_VERSION,
        "relay_id": relay_id(),
        "relay_public_key": get_relay_public_key_hex(),
        "relay_key_history": relay_key_history(),
        # P290: the keys that must agree to change its key, if it has named them
        "guardians": _relay_guardians(),
        "relay_url": RELAY_BASE_URL, "host": _own_host(),
        "operator": _relay_operator_id() or None,
        "access": access,
        **_fees_card(),
        # P291 (card v2): what it runs beyond the core — money and its rails, the log,
        # federation and its links, gamify and its formulas — one field, as on /health
        "relay_modules": _relay_modules(),
        "games": list(MODULE_REGISTRY.keys()),
        "software": __version__,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    from emporia.identity import relay_card_bytes, sign_raw_relay
    card["signature"] = sign_raw_relay(relay_card_bytes(card))
    return card


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
        "emporia:sessions:v1",
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
        "relay_id": relay_id(),
        "relay_url": RELAY_BASE_URL,
        "require_challenge": REQUIRE_CHALLENGE,
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
    # Review follow-up M4: v2 — `signature` (base64url) over
    # wire.signed_dashboard_sign_in_payload(nonce, relay), `relay` this relay's origin.
    relay: str = ""
    signature: str = ""
    signature_hex: str = ""     # v1 (the bare nonce): refused, by name


def sign_in_origins() -> set[str]:
    """M4: the origins a dashboard sign-in may name to be good here — this relay's own URL
    (EMPORIA_RELAY_URL), plus any other name it is reached by (EMPORIA_RELAY_SIGN_IN_ORIGINS,
    comma-separated). Never the request's Host header: whoever sends a signature chooses that."""
    from emporia.addresses import canonical_host, host_of
    from emporia.wire import relay_origin
    out = set()
    for url in (RELAY_BASE_URL, *os.getenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", "").split(",")):
        try:
            origin = relay_origin(url)
        except ValueError:
            continue
        out.add(f"{origin.split('://')[0]}://{canonical_host(host_of(origin))}")
    return out


def _names_this_relay(origin: str) -> bool:
    from emporia.addresses import canonical_host, host_of
    from emporia.wire import relay_origin
    try:
        o = relay_origin(origin)
    except ValueError:
        return False
    return f"{o.split('://')[0]}://{canonical_host(host_of(o))}" in sign_in_origins()


@app.post("/dashboard/challenge")
async def dashboard_challenge(request: Request):
    """Step 1 of a signed session: get a nonce to sign.

    The client (the local console, the SDK, key rotation) signs this nonce with the agent's key and
    submits the signature to POST /dashboard/session for a JWT.
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
        "relay_id": relay_id(),
        "instructions": (
            "Sign the nonce with this relay's origin (emporia:dashboard-sign-in:v2) and send it to "
            "POST /dashboard/session; the answer carries the token."
        ),
        "sign_in": "emporia:dashboard-sign-in:v2",
    }


def _signin_failed(reason: str, agent_id: str, *, expected: bool = False) -> None:
    """The named `signin_failed` event (a reason class, never a nonce or signature). `expected` marks
    the request line too, for the failures that are a normal step in a flow."""
    fields = {"reason": reason, "agent_id": str(agent_id)[:64], "expected": expected}
    logger.log(logging.INFO if expected else logging.WARNING, "signin_failed", extra=fields)
    if expected:
        mark_expected()


@app.post("/dashboard/session")
async def dashboard_session(req: DashboardSessionRequest):
    """Step 2 of dashboard auth: verify Ed25519 signature → issue JWT.

    The JWT is scoped to this relay (relay_id in payload). It expires in 1 hour
    and is verified by _dashboard_agent_id() on subsequent requests.

    Review follow-up M4: the signature covers the nonce and the relay it is for
    (`emporia:dashboard-sign-in:v2`), and that relay must be this one
    (`sign_in_origins`). The v1 form — a signature over the bare nonce, good
    wherever the nonce was issued — is refused by name.
    """
    if req.signature_hex or not req.signature or not req.relay:
        _signin_failed("old_or_malformed_request", req.agent_id)
        raise HTTPException(400, "dashboard sign-in v1 (a signature over the bare nonce) is gone: sign "
                                 "emporia:dashboard-sign-in:v2 {type, nonce, relay} and send {agent_id, "
                                 "challenge_id, relay, signature} (docs/protocol.md §3.1)")
    if not _names_this_relay(req.relay):
        _signin_failed("wrong_relay", req.agent_id)
        raise HTTPException(403, f"this sign-in is for {req.relay[:100]}, not this relay — a sign-in is "
                                 "good only on the relay it names")
    entry = _CHALLENGE_STORE.pop(req.challenge_id, None)
    if not entry:
        _signin_failed("unknown_or_expired_challenge", req.agent_id, expected=True)
        raise HTTPException(400, "Unknown or expired challenge_id")
    nonce, expires_at = entry
    if time.time() > expires_at:
        _signin_failed("expired_challenge", req.agent_id, expected=True)
        raise HTTPException(400, "Challenge expired")

    # Look up the agent's registered public key
    with DB_LOCK, get_db() as conn:
        row = conn.execute(
            "SELECT public_key_hex, stable_id FROM authorized_agents WHERE agent_id = ?",
            (req.agent_id,),
        ).fetchone()
    if not row:
        # Normal in a first sign-in by an agent that has not registered yet (the CLI and the console
        # probe this before registering): the 404 stays — clients read it — but the line is tagged.
        _signin_failed("not_registered", req.agent_id, expected=True)
        raise HTTPException(404, f"Agent {req.agent_id!r} not registered on this relay")

    from emporia.wire import signed_dashboard_sign_in_payload
    if not verify(signed_dashboard_sign_in_payload(nonce, req.relay), req.signature, row["public_key_hex"]):
        _signin_failed("bad_signature", req.agent_id)
        raise HTTPException(403, "Invalid signature — sign {type: emporia:dashboard-sign-in:v2, nonce, "
                                 "relay} with the agent's key")

    now = time.time()
    jwt = _jwt_encode({
        "agent_id": req.agent_id,
        "stable_id": row["stable_id"],
        "relay_id": relay_id(),
        "iat": int(now),
        "issued": now,
        "exp": int(now + _JWT_TTL_SECONDS),
        "jti": secrets.token_hex(12),          # its id: what sign-out revokes
    })
    return {
        "token": jwt,
        "agent_id": req.agent_id,
        "stable_id": row["stable_id"],
        "expires_in": _JWT_TTL_SECONDS,
        "token_type": "Bearer",
    }


@app.get("/modules")
async def list_modules():
    """Every game this relay hosts, with its rules for agents (P133)."""
    from emporia.module_sdk import list_available_modules
    return {"modules": list_available_modules()}


# ============================================================================
# Agent Registration — challenge / proof-of-key-possession
# ============================================================================

_CHALLENGE_TTL_SECONDS = 300  # nonces expire after 5 minutes




def _consume_challenge(challenge_id: str, public_key_hex: str, signature: str, relay: str = "",
                       agent_id: str = "", operator_label: str = "", client_name: str = "",
                       client_version: str = "", about: str = "") -> None:
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

    # The signature is over {type, nonce, relay, agent_id, public_key}: for this relay, this name, this key.
    if not relay or not _names_this_relay(relay):
        raise HTTPException(403, f"this registration is for {str(relay)[:100] or 'no relay'}, not this relay — "
                                 "sign emporia:sign-in:v2 for the relay you are talking to")
    from emporia.wire import about_problem, signed_registration_payload
    if why := about_problem(about):
        raise HTTPException(400, why)
    if not verify(signed_registration_payload(nonce, relay, agent_id, public_key_hex, operator_label, client_name,
                                              client_version, about), signature,
                  str(public_key_hex).lower()):
        raise HTTPException(403, "Challenge signature invalid — sign emporia:sign-in:v2 {nonce, relay, "
                                 "agent_id, public_key, operator, client, about} with the agent's key")


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


def _validated_encryption_key(agent_id: str, public_key_hex: str, signature: str, at: str = "") -> str:
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
        agent_id, signed_encryption_key_payload(agent_id, key, at), signature,
    ):
        raise HTTPException(
            403,
            "encryption key signature does not verify against this agent's Ed25519 "
            "key — the key must be signed by the identity publishing it",
        )
    # P271: a key replaced is replaced forward only. A different key needs a v2
    # binding newer than the one held, so an old binding replayed here (a leaked
    # key's, say) cannot put that key back. The same key may be signed again.
    from emporia import wire
    from emporia.routes.agents import _signed_at
    if at:
        _signed_at(at)              # near this relay's clock, as a wallet statement is
    held, _sig = AGENT_REGISTRY.get_encryption_key(agent_id)
    if held and held != key:
        held_at = wire.when_of(AGENT_REGISTRY.encryption_key_at(agent_id))
        if not at or (held_at is not None and wire.when_of(at) <= held_at):
            raise HTTPException(409, "a new encryption key needs a binding signed after the current one's "
                                     "(emporia agent rotate-encryption-key does this)")
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
    from emporia.payments import create_price_intent

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
    payee: str | None = None

    if req.session_id:
        session = load_session(req.session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        payment_rules = session.get("payment_rules", {})
        price = payment_rules.get("price", "0")
        expected_amount = int(float(price) * 100) if price else 0
        currency = (payment_rules.get("currency") or "usd").lower()
        service_type = service_type or session.get("module_type") or "emporia:session"
        if payment_rules.get("mode", "free") == "free" or expected_amount <= 0:
            raise HTTPException(400, "Session does not require a payment")
        if not custodial_cards_allowed():
            raise HTTPException(409, (
                "This relay does not hold card money on its own balance, so this session "
                "cannot be paid with a card intent. Fund a purchase with fund_session."))
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
        payee = room.creator_id
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
    # already reject a free resource with "does not require a payment", so
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

    destination = fee = None
    if payee and not custodial_cards_allowed():
        destination = stripe_payee_account(payee)
        if not destination:
            raise HTTPException(409, (
                f"{payee} has no active Stripe account here, and card payments go straight "
                f"to the payee on this relay. Pay over x402 or Tempo if offered."))
        from emporia.payments import platform_fee
        fee = platform_fee(expected_amount)

    try:
        result = await create_price_intent(
            session_id=target_id,
            amount_cents=expected_amount,
            buyer_id=caller,
            seller_id=req.seller_id,
            service_type=service_type,
            capture_method=capture_method,
            currency=currency,
            resource_type=resource_type,
            destination_account=destination,
            application_fee_cents=fee,
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
    "settlement_id", "session_id", "winner_id", "total_price_cents",
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


def _settlement_kinds(conn: Any, rows: list[Any]) -> dict[str, str]:
    """Each row's kind, by settlement id: a row with a payer bought access to a room or an
    Agora topic (`room`, `topic`; `access` if that resource is gone); a row without one is a
    session's — a `game` (a module that names a winner), or else a
    `service` purchase (`session` if the session is gone)."""
    from emporia.standing import game_module_types

    access = {r["session_id"] for r in rows if r["payer_id"] is not None}
    sessions = {r["session_id"] for r in rows if r["payer_id"] is None}

    def found(sql: str, ids: set[str]) -> dict[str, Any]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        return {row[0]: row for row in conn.execute(sql.format(marks=marks), list(ids)).fetchall()}

    rooms = found("SELECT room_id FROM rooms WHERE room_id IN ({marks})", access)
    topics = found("SELECT topic_id FROM agora_topics WHERE topic_id IN ({marks})", access)
    sess = found("SELECT session_id, module_type "
                 "FROM sessions WHERE session_id IN ({marks})", sessions)
    games = set(game_module_types())
    kinds: dict[str, str] = {}
    for r in rows:
        sid = r["session_id"]
        if r["payer_id"] is not None:
            kind = "room" if sid in rooms else "topic" if sid in topics else "access"
        elif sid not in sess:
            kind = "session"
        else:
            kind = "game" if sess[sid][1] in games else "service"
        kinds[r["settlement_id"]] = kind
    return kinds


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
    `total_price_cents`/`winner_payout_cents` are left alone: room/topic entry
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
    distinguishes "no relay operator is configured at all" (EMPORIA_OPERATOR_STABLE_ID unset —
    no identity can ever satisfy this check) from "you aren't the configured
    operator" (it is set, but not to your key). N5 review R5 found
    trigger_federation_sync gave the same generic message either way, which reads
    as "authenticate as the right agent" when the real problem — on a default
    install, where EMPORIA_AGENT_ID is unset — is that the action is unreachable by
    anyone, including whoever would become the operator by setting it.
    """
    owner = _relay_operator_id()
    if not operator_stable_id():
        raise HTTPException(
            403,
            f"{action}: no relay operator is configured (EMPORIA_OPERATOR_STABLE_ID unset) — "
            "this action is unavailable on this relay"
        )
    if not caller:   # no credential at all: 401, as every other route answers it
        raise HTTPException(401, f"{action}: authentication required (the relay operator's bearer token)")
    if not owner or caller != owner:
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
                          the exact truncation-signal defect N6-3 fixed for ?session_id=).
                          Also X's own sales of access (a room or topic X created, paid
                          for by another agent; the payer's identifiers redacted), and
                          each row's `kind` (_settlement_kinds) — the Stats view's
                          earnings by source.
      - no filter     → requires dashboard auth AND relay operator identity. Same `limit`/
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
                "Sign in (a bearer token), or use the records MCP tool (kind=settlements)."
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
            #
            # Stats view: the third arm is the agent's own sales of access — a room
            # or Agora topic it created, paid for by someone else (one row per
            # payer, `winner_id` the creator, `payer_id` set). Before it, a
            # creator had no path to its entry fees at all, so they never counted
            # as its earnings. Those rows are the payer's receipts too, so they go
            # through _settlement_row_dict and the payer's identifiers stay
            # redacted; the first two arms (what the caller owns) are unchanged.
            rows, has_more = _fetch_page(
                conn,
                "SELECT * FROM settlements WHERE payer_id = ? "
                "OR (payer_id IS NULL AND session_id IN "
                "(SELECT session_id FROM participants WHERE agent_id = ?)) "
                "OR (payer_id IS NOT NULL AND winner_id = ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (agent_id, agent_id, agent_id), limit,
            )
            kinds = _settlement_kinds(conn, rows)
            settlements = []
            for r in rows:
                owned = r["payer_id"] is None or r["payer_id"] == agent_id
                d = _settlement_public_dict(r) if owned else _settlement_row_dict(r, dashboard_id, set())
                d["kind"] = kinds.get(r["settlement_id"], "session")
                settlements.append(d)
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
    request: Request, session_id: str | None = None, room_id: str | None = None, mine: bool = False
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
      - ?mine=1       → the caller's own payments, the last 500 (P287: its analytics)
      - no filter     → relay operator only
    """
    caller = _dashboard_agent_id(request)
    owner = _relay_operator_id()
    is_owner = bool(owner and caller == owner)
    with DB_LOCK, get_db() as conn:
        if mine:
            if not caller:
                raise HTTPException(401, "Authentication required")
            rows = conn.execute(
                "SELECT * FROM payments WHERE agent_id = ? ORDER BY created_at DESC LIMIT 500", (caller,),
            ).fetchall()
            return {"payments": [dict(r) for r in rows]}
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


async def _repin_if_rotated(peer_url: str, presented: str) -> bool:
    """P191: a peer presenting a new key keeps its pin only if its history shows
    the pinned key handing over to it, each step signed by the key before."""
    from emporia.identity import follows_rotation
    pinned = _FEDERATION_PEER_PUBKEYS.get(peer_url)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            h = (await client.get(f"{peer_url}/health")).json()
    except (httpx.HTTPError, ValueError):
        return False
    if not pinned or h.get("relay_public_key") != presented or not follows_rotation(
            pinned, presented, h.get("relay_key_history") or []):
        return False
    _FEDERATION_PEER_PUBKEYS[peer_url] = presented
    save_federation_peer_pubkeys(_FEDERATION_PEER_PUBKEYS)
    logger.info("federation_peer_key_rotated", extra={"peer_url": peer_url})
    return True


async def _pull_federated_posts(peer_url: str) -> int:
    """Pull the pointer posts a linked relay's agents wrote, and keep those whose agent signature checks.

    Only `pointer` posts travel. The batch must be signed by the peer's own relay key (pinned on the
    first good pull; a later key must follow from the pinned one by a signed rotation, or the batch is
    refused as a possible URL hijack), must name the relay it was fetched from, and each post must carry
    its agent's own signature, key log and the origin it names (`wire.post_verdict`): the batch
    signature says which relay sent it, a separate fact. Nothing a post points at is fetched.
    """
    peer_url = normalize_peer_url(peer_url)  # N13-2 (R2): one identity for this peer
    try:
        data = await _fetch_federation_batch(f"{peer_url}/federation/v1/posts")
    except Exception as e:
        why = str(e)
        try:    # an older relay has no posts feed: say which protocol it runs rather than that a path is missing
            from emporia import fetch
            old = _old_protocol((await fetch.request("GET", f"{peer_url}/relay/card", timeout=10)).json())
            why = f"refused: {old}" if old else why
        except Exception:  # noqa: BLE001
            pass
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": False, "imported": 0, "error": why, "synced_at": datetime.now(UTC).isoformat()}
        return 0

    origin = data.get("origin_relay", peer_url)
    posts = data.get("posts", [])
    pubkey_hex = data.get("origin_public_key_hex", "")
    signature_hex = data.get("signature", "")

    def _reject(reason: str) -> int:
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": False, "imported": 0, "error": reason, "synced_at": datetime.now(UTC).isoformat()}
        return 0

    if not pubkey_hex or not signature_hex or not isinstance(posts, list):
        return _reject("peer response is unsigned — refusing to import (upgrade the peer relay)")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(signature_hex),
            federation_signable_bytes(origin, "posts", posts),
        )
    except Exception:
        return _reject("signature verification failed — batch may be tampered or corrupt")

    # A peer speaks only for itself: the origin it names must be the URL it was linked and fetched at.
    if normalize_peer_url(origin) != peer_url:
        return _reject(f"the feed names {origin!r} as its origin, not the relay linked as {peer_url!r}")

    _ensure_federation_pins_loaded()
    pinned = _FEDERATION_PEER_PUBKEYS.get(peer_url)
    if pinned and pinned != pubkey_hex and await _repin_if_rotated(peer_url, pubkey_hex):
        pinned = pubkey_hex
    if pinned and pinned != pubkey_hex:
        return _reject(
            f"peer key mismatch — pinned {pinned[:12]}..., got {pubkey_hex[:12]}... "
            "(possible URL hijack; remove from FEDERATED_RELAYS and re-add to re-pin deliberately)"
        )
    if pinned is None:
        _FEDERATION_PEER_PUBKEYS[peer_url] = pubkey_hex
        save_federation_peer_pubkeys(_FEDERATION_PEER_PUBKEYS)

    # Skip our own relay, by key as well as URL, to prevent loops.
    if normalize_peer_url(origin) == normalize_peer_url(RELAY_BASE_URL) or pubkey_hex == get_relay_public_key_hex():
        _FEDERATION_LAST_SYNC[peer_url] = {
            "ok": True, "imported": 0, "note": "skipped — peer reported our own relay as origin",
            "synced_at": datetime.now(UTC).isoformat()}
        return 0

    # N13-3 (R3): cap what we accept, after the signature (which covers the whole list) is checked.
    peer_batch_size = len(posts)
    if peer_batch_size > _MAX_LIMIT:
        posts = posts[:_MAX_LIMIT]

    from emporia.wire import post_verdict

    imported = 0
    refused: dict[str, int] = {}
    kept: set[str] = set()
    for post in posts:
        if not isinstance(post, dict) or not post.get("post_id"):
            continue
        why = post_verdict(post, verify)
        if not why and post.get("origin_relay") != origin:
            why = "it was posted on another relay than the one serving it"
        if not why and (post.get("kind") != "pointer" or post.get("topic") or post.get("reply_to")):
            why = "only pointer posts travel between relays"
        if why:
            refused[why] = refused.get(why, 0) + 1
            continue
        try:
            # N12-6: untrusted external text landing in a store the dashboard renders.
            await _assert_payload_safe_counted({"text": post["text"], "label": post.get("label", ""),
                                                "tags": post.get("tags") or []})
        except Exception:
            continue
        from emporia.addresses import address, host_of
        with DB_LOCK, get_db() as conn:
            # Ownership (P83/P87): a peer may refresh only the rows it served before, never a local one.
            existing = conn.execute("SELECT federated_from, is_copy FROM posts WHERE post_id = ?",
                                    (post["post_id"],)).fetchone()
            if existing and (existing["federated_from"] != peer_url or existing["is_copy"]):
                refused["its id is already taken here"] = refused.get("its id is already taken here", 0) + 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO posts (post_id, author_id, author_stable_id, author_public_key_hex, "
                "origin_relay, federated_from, nonce, kind, label, text, tags_json, topic, reply_to, quote_of, "
                "at, signature, key_log_json, is_deleted, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,"
                "?,?,?,0,?)",
                (post["post_id"], address(str(post.get("author_id") or ""), host_of(origin)), post["author_stable_id"],
                 str(post["author_public_key_hex"]).lower(), origin, peer_url, post["nonce"], "pointer",
                 post.get("label") or "", post["text"], json.dumps(post.get("tags") or []),
                 post.get("quote_of"), post["at"], post["signature"], json.dumps(post.get("key_log") or []),
                 datetime.now(UTC).isoformat()))
        imported += 1
        kept.add(post["post_id"])
    # The batch is the peer's whole current list: a post it no longer sends was withdrawn there.
    if peer_batch_size <= _MAX_LIMIT:
        with DB_LOCK, get_db() as conn:
            for r in conn.execute("SELECT post_id FROM posts WHERE federated_from = ? AND is_copy = 0",
                                  (peer_url,)).fetchall():
                if r["post_id"] not in kept:
                    conn.execute("DELETE FROM posts WHERE post_id = ? AND federated_from = ?",
                                 (r["post_id"], peer_url))
    sync_record: dict[str, Any] = {
        "ok": True, "imported": imported, "origin_relay": origin,
        "synced_at": datetime.now(UTC).isoformat(),
    }
    if peer_batch_size > _MAX_LIMIT:
        sync_record["truncated"] = True
        sync_record["peer_batch_size"] = peer_batch_size
    if refused:
        sync_record["refused"] = sum(refused.values())
        sync_record["refused_why"] = refused
    _FEDERATION_LAST_SYNC[peer_url] = sync_record
    return imported


async def _sync_all_federated_peers() -> dict[str, int]:
    """Pull pointer posts from every configured peer, concurrently.

    P23-3: used to be a `for peer in FEDERATED_RELAYS: await ...` loop -- the pullers
    already catch and record per-peer failures into `_FEDERATION_LAST_SYNC`
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
    drop_unlinked_federated_rows()
    async def _pull_peer(peer: str) -> int:
        imported = await _pull_federated_posts(peer)
        # P291: then what it runs, from its card signed by the key the posts pinned —
        # what is kept from it below, and what is offered to it, follows that
        try:
            await _pull_peer_modules(normalize_peer_url(peer))
        except Exception:
            logger.exception("federation_modules_pull_raised_unexpectedly", extra={"peer_url": peer})
        try:   # P281: and its log's state, checked against the one kept from last time
            if peer_runs(peer, "log") is False:
                _note_peer_log_off(normalize_peer_url(peer))
            else:
                await asyncio.to_thread(_check_peer_log, normalize_peer_url(peer))
        except Exception:
            logger.exception("federation_log_check_raised_unexpectedly", extra={"peer_url": peer})
        return imported

    pull_results = await asyncio.gather(*(_pull_peer(p) for p in peers), return_exceptions=True)

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

    return _coerce(pull_results)


async def _pull_peer_modules(peer_url: str) -> dict[str, Any] | None:
    """P291: read a linked relay's signed card and keep what it runs. Believed only
    when signed by the key this relay pinned for it; otherwise nothing is kept
    and the peer is treated as before (`peer_runs` → None). Then:

    * whether it links back (federation, both ways) goes in its sync record,
      since sealed DMs need both ways.
    """
    from emporia import fetch
    from emporia.identity import verify_relay_card
    pinned = _FEDERATION_PEER_PUBKEYS.get(peer_url)
    sync = _FEDERATION_LAST_SYNC.setdefault(peer_url, {"ok": None, "imported": 0})
    try:
        card = (await fetch.request("GET", f"{peer_url}/relay/card", timeout=10)).json()
    except Exception as e:  # noqa: BLE001 — not answering, or an older relay
        sync["relay_modules_error"] = f"its card did not come: {str(e)[:160]}"
        return None
    if not (isinstance(card, dict) and pinned and card.get("relay_public_key") == pinned
            and verify_relay_card(card)["ok"] and isinstance(card.get("relay_modules"), dict)):
        sync["relay_modules_error"] = "its card is not signed by the key pinned for it, or names no modules"
        return None
    if old := _old_protocol(card):
        sync["relay_modules_error"] = f"refused: {old}"
        return None
    mods = card["relay_modules"]
    _FEDERATION_PEER_MODULES[peer_url] = mods
    sync.pop("relay_modules_error", None)
    sync["relay_modules"] = mods
    back = (mods.get("federation") or {}).get("links") or []
    sync["links_back"] = normalize_peer_url(RELAY_BASE_URL) in {normalize_peer_url(u) for u in back}
    return mods


def _note_peer_log_off(peer_url: str) -> None:
    """P291: a linked relay that runs no log is not checked — but one that kept a log
    and stopped is said to have, since its last signed state is still kept here."""
    from emporia import logcheck
    held = (((logcheck.load(logcheck.PEER_FILE).get("relays") or {}).get(peer_url)) or {}).get("state")
    sync = _FEDERATION_LAST_SYNC.setdefault(peer_url, {"ok": None, "imported": 0})
    sync["log"] = ("stopped: it kept a log and no longer runs one; its last signed state is kept here"
                   if held else "not run there")
    if held:
        logger.warning("federation_peer_log_stopped", extra={"peer_url": peer_url})


def _peer_log_get(peer_url: str):
    """A `logcheck.Get` for a linked relay (tests replace it)."""
    def get(path: str, params: dict) -> tuple[int, Any]:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{peer_url}{path}", params=params or None)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return get


def _check_peer_log(peer_url: str) -> dict[str, Any] | None:
    """P281 (transparency log, step 3): fetch a linked relay's signed log state
    and check it only added to the one this relay kept. A failure is kept as
    evidence — two states it signed that cannot both be true — and served in
    GET /log/seen and /federation/peers for anyone to check."""
    from emporia import logcheck
    pinned = _FEDERATION_PEER_PUBKEYS.get(peer_url)
    if not pinned:
        return None
    out = logcheck.check_and_keep(peer_url, _peer_log_get(peer_url), [pinned], name=logcheck.PEER_FILE)
    if not out["ok"] and out["status"] != "unreachable":
        logger.warning("federation_peer_log_failed", extra={"peer_url": peer_url, "detail": out["detail"]})
    return out


def drop_unlinked_federated_rows() -> int:
    """P184: pointer posts pulled from a relay no longer linked go — unlinking a
    relay must not leave its posts shown here forever."""
    linked = [normalize_peer_url(u) for u in FEDERATED_RELAYS]
    with DB_LOCK, get_db() as conn:
        post_q = "DELETE FROM posts WHERE federated_from IS NOT NULL"
        if linked:
            post_q += f" AND federated_from NOT IN ({','.join('?' * len(linked))})"
        return conn.execute(post_q, linked).rowcount


_FEDERATION_SYNC_INTERVAL_SECONDS = float(os.getenv("EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS", "300"))
_FEDERATION_SYNC_TASK: asyncio.Task | None = None  # set/cancelled in lifespan(); exposed for tests/ops


async def _federation_sync_loop() -> None:
    """Runs _sync_all_federated_peers() on EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS
    (default 300s) -- mirrors _sweep_loop()'s exact shape. Started in lifespan()
    only when FEDERATED_RELAYS is non-empty; cancelled there on shutdown.

    P23-3: before this, `POST /gaming/v1/federate/sync` was the *only* sync
    path -- operator-triggered only, no periodic retry. Per-peer failures were
    already recorded and non-fatal (see _pull_federated_posts), so a
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
    """P4-5 (docs/archive/roadmap-hackathon-era.md § Architecture cleanup): operator-only cleanup for a
    finished session's operational rows. Hard-deletes `participants`,
    `actions`, `messages`, and the `sessions` row itself — one transaction,
    so this never leaves a session half-deleted.

    Deliberately does NOT delete `payments` or `settlements` rows — those
    are the record that real money moved, and deleting them destroys
    reconciliation. This is a permanent product decision, not an
    oversight the cascade should eventually "finish": the response reports
    exactly how many of each were retained. The `settlements.session_id`
    column is overloaded (session/room/topic escrow all key off it — see
    docs/security-log.md's N4-R1) but the `sess_`/`room_`/`tpc_` id prefixes make a
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

    P5-2 cleared `rooms.linked_session_id` for a room linked to this session.
    Since P277 the room's creator signs a commitment to that game, so clearing
    it made the room look tampered with and refused its fee (review of P277).
    The link stays, reported as `rooms_still_linked`; nothing loads a deleted
    game by it (`_session_visibility()` runs only for a game that exists). The
    room itself is never deleted: it has its own members and paid entitlements.
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
    # already fixed once (docs/archive/roadmap-hackathon-era.md § Architecture cleanup) — but it's the
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
        "rooms_still_linked": unlinked,
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
            # Review of P277: a room keeps its link — its creator signed a commitment to that game,
            # and clearing it made the room look tampered with. Nothing loads a deleted game by it.
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
                "expected": {"rooms_still_linked": unlinked, "deleted": deleted},
                "observed": {"rooms_still_linked": recheck_unlinked, "deleted": recheck_deleted},
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
        "rooms_still_linked": unlinked,
        "message": "Payments and settlements are never deleted — see this endpoint's docstring.",
    }


# ============================================================================
# Messaging / Negotiation
# ============================================================================


@app.post("/messages")
async def send_message(req: SendMessageRequest, request: Request):
    caller = _dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    _require_registered(caller)
    _enforce_agent_rate_limit(caller)
    # Relay audit 11-11 (F1): only an offer's own kinds travel here. Any other type reached the recipient's
    # bridge as a relay-voiced notice (`your_turn`, `funding_required`, ...), admitted in every wake mode.
    if req.type not in NEGOTIATION_MESSAGE_TYPES:
        raise HTTPException(400, f"type must be one of {', '.join(sorted(NEGOTIATION_MESSAGE_TYPES))}")
    if not AGENT_REGISTRY.is_authorized(req.to_agent):
        raise HTTPException(404, f"Agent '{req.to_agent}' is not registered on this relay")
    from emporia.routes.agents import dm_blocked
    if dm_blocked(req.to_agent, caller):
        raise HTTPException(403, f"{req.to_agent} takes no messages from {caller}")
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
    """The raw messages audit table: this agent's messages, newest first."""
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
      {"type": "room_created",    ...}
      {"type": "room_message",    ...}
    Send {"type": "ping"} to keep alive; relay responds {"type": "pong"}.
    """
    if await _ws_refused(ws):
        return
    await _ws_accept(ws)
    ws_id = uuid.uuid4().hex[:12]
    logger.info("ws_connected", extra={"ws_id": ws_id, "channel": "global"})
    ws_connections["__global__"].add(ws)
    try:
        # Send a snapshot on connect
        health_data = {
            "status": "ok",
            "service": BRAND,
            "version": __version__,
            "session_count": 0,
            "modules": list(MODULE_REGISTRY.keys()),
        }
        try:
            with DB_LOCK, get_db() as conn:
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
    if await _ws_refused(ws):
        return
    s = load_session(session_id)
    if not s:
        logger.warning("ws_rejected", extra={"channel": "session", "session_id": session_id, "reason": "unknown_session"})
        await ws.close(code=1008)
        return
    if not _session_viewer_allowed(s, _ws_agent_id(ws)):
        logger.warning("ws_rejected", extra={"channel": "session", "session_id": session_id, "reason": "not_allowed"})
        await ws.close(code=1008)
        return
    await _ws_accept(ws)
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
    if await _ws_refused(ws):
        return
    caller = _ws_agent_id(ws)
    owner = _relay_operator_id()
    if caller != agent_id and not (owner and caller == owner):
        logger.warning("ws_rejected", extra={"channel": "agent", "agent_id": agent_id, "reason": "not_authorized"})
        await ws.close(code=1008)
        return
    await _ws_accept(ws)
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
# Rooms
# Public: open to all registered agents.
# Private: hidden from listing; gated by invite OR Stripe payment.
# Encryption flag: relay stores opaque ciphertext, skips guardrails scan.
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
    (docs/archive/roadmap-hackathon-era.md § Payments), `payout_winner()` has no counterpart at all on
    a rail where the payment itself is the settlement — a real second rail
    would skip this transfer entirely, not call into a different transfer
    implementation, so there is nothing to dispatch to yet. See
    payments.KNOWN_RAILS's own docstring for why a speculative registry
    entry isn't here either.

    settle() (session escrow, capture-then-transfer) is deliberately not
    routed through here — escrow has no x402 analogue and stays Stripe-only
    (docs/archive/roadmap-hackathon-era.md § Payments), and its own "pending_connect" initial sentinel
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
    a provisional decision, not a permanent one (see docs/archive/roadmap-hackathon-era.md § Payments
    for the revisit trigger: a cross-chain splitter mechanism). The Stripe
    branch below is untouched, byte-for-byte.
    """
    from emporia.payments import (
        RAIL_TEMPO, RAIL_X402, payout_winner, platform_fee as _platform_fee, settlement_dedupe_key,
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
    if payment_ref.rail == RAIL_TEMPO:
        # P156: paid straight to the creator on chain, like x402 — nothing held,
        # nothing to transfer, no cut taken.
        platform_fee, creator_payout, platform_fee_bps = 0, amount_cents, 0
        transfer_id, transfer_status = None, "direct"
    elif payment_ref.rail == RAIL_X402:
        # P48: the entitlement now exists, so the purchase this intent was
        # guarding is finished and the intent must go — otherwise a *later*
        # re-purchase of the same resource, after this entitlement expires,
        # would collide with the settled row and be handed free access. This
        # is the retirement point because the entitlement is what the payment
        # bought; see _retire_x402_intent().
        _retire_x402_intent(
            payer_id=payer_id, resource_type=resource_type,
            resource_id=resource_id, amount_cents=amount_cents - payment_ref.fee_cents,
        )
        if payment_ref.fee_cents:
            # a split entry (fees.py): the fee was paid straight to the relay's fee address
            _retire_x402_intent(
                payer_id=payer_id, resource_type=f"{resource_type}:fee",
                resource_id=resource_id, amount_cents=payment_ref.fee_cents,
            )
        platform_fee = payment_ref.fee_cents
        creator_payout = amount_cents - platform_fee
        from emporia import fees as _fees
        platform_fee_bps = _fees.fee_bps() if platform_fee else 0
        transfer_id = None
        # "direct": no platform-held balance ever existed to transfer out of
        # — distinct from "no_stripe_account" (Stripe, platform holds funds,
        # simply has nowhere to send them) and "transferred"/"transfer_failed"
        # (Stripe, a transfer was attempted). Not a failure state.
        transfer_status = "direct"
    elif payment_ref.destination:
        # P157: a destination charge — Stripe paid the creator, less the fee,
        # when it was captured. Nothing reached the platform but the fee.
        platform_fee_bps = _fee_bps_now()
        platform_fee = _platform_fee(amount_cents)
        creator_payout = amount_cents - platform_fee
        transfer_id, transfer_status = None, "destination"
    else:
        platform_fee_bps = _fee_bps_now()
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
        "total_price_cents": amount_cents,
        "platform_fee_cents": platform_fee,
        "winner_payout_cents": creator_payout,
        "platform_fee_bps": platform_fee_bps,
        "payment_intent_ids": [payment_ref.reference] + ([payment_ref.fee_reference] if payment_ref.fee_reference else []),
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
    from emporia.payments import PaymentRef, settlement_dedupe_key, verify_payment_intent

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

    ref = PaymentRef(row["rail"], row["payment_intent_id"])
    if ref.rail == "stripe" and not _settlement_exists(
            settlement_dedupe_key(resource_id, payer_id, [ref.reference])):
        # P157: whether Stripe already paid the creator (a destination charge)
        # or the platform holds it is on the PaymentIntent, not in our row.
        # Unknown on a relay that does not hold money: finish it later rather
        # than guess — guessing "held" would pay the creator twice.
        try:
            dest = (await verify_payment_intent(ref.reference)).get("destination")
        except Exception:
            if not custodial_cards_allowed():
                logger.warning("could not read %s to finish its settlement; will retry",
                               ref.reference, exc_info=True)
                return
            dest = None
        ref = PaymentRef(ref.rail, ref.reference, dest)
    if ref.rail == "x402":
        from emporia import fees
        # a split entry was paid in two: the fee went to the relay's fee address (fees.py)
        ref = PaymentRef(ref.rail, ref.reference, fee_cents=fees.fee_cents(row["amount_cents"]))
    await _settle_access_payment(
        resource_type=resource_type,
        resource_id=resource_id,
        outcome_type=outcome_type,
        payer_id=payer_id,
        creator_id=creator_id,
        amount_cents=row["amount_cents"],
        payment_ref=ref,
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
    creator's payout is reversed, and the platform's 1% fee is simply never
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
            payment_intent_id, idempotency_key="emporia:refund:" + dedupe_key,
            # P157: a destination charge — pull the creator's share and the
            # fee back with the refund, in the same call.
            destination_charge=row["transfer_status"] == "destination",
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
    """`{agent_id: {encryption_public_key, signature, public_key_hex, stable_id,
    key_log}}` for every member of `room`, in one query per member.

    P250: with the stable id and key log, so a client checking the binding
    against the signing key it pinned can follow a rotation (as
    `GET /agents/{id}/encryption-key` lets it) instead of refusing the member.

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
            "SELECT agent_id, encryption_public_key, encryption_key_signature, encryption_key_at, "
            f"public_key_hex, stable_id FROM authorized_agents WHERE agent_id IN ({placeholders})",
            room.members,
        ).fetchall()
        found = {
            row["agent_id"]: {
                "encryption_public_key": row["encryption_public_key"] or "",
                "signature": row["encryption_key_signature"] or "",
                "at": row["encryption_key_at"] or "",          # P271
                "public_key_hex": row["public_key_hex"] or "",
                "stable_id": row["stable_id"] or "",
                "key_log": agent_key_log(conn, row["stable_id"]),
            }
            for row in rows
        }
    empty = {"encryption_public_key": "", "signature": "", "at": "", "public_key_hex": "", "stable_id": "",
             "key_log": []}
    return {member: found.get(member, empty) for member in room.members}








@app.websocket("/ws/rooms/{room_id}")
async def ws_room(ws: WebSocket, room_id: str):
    """Real-time room channel. Sends last 20 messages on connect. Private rooms require
    the caller to be an authenticated member (see _ws_agent_id()) — public rooms are open
    to any connection, matching GET /rooms/{id}/messages."""
    if await _ws_refused(ws):
        return
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
    await _ws_accept(ws)
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
    # Agora v2: a private or restricted `invite` topic sealed to its members (as an encrypted room is).
    encrypted: bool = False
    # P278: signed by the creator over every setting (wire.signed_agora_topic_payload).
    nonce: str = ""
    signature: str = ""

class AgoraSubscribeRequest(BaseModel):
    agent_id: str
    payment_intent_id: str | None = None


def _require_registered(agent_id: str) -> None:
    """Raise 403 if agent_id is not registered.

    Read endpoints are open; all write operations (create/post/join/subscribe/vote/message)
    require prior registration.
    """
    if not AGENT_REGISTRY.is_authorized(agent_id):
        raise HTTPException(403, f"Agent '{agent_id}' not registered on this relay. "
                            "Register first via POST /agents/register or MCP auto-registration.")


_AGORA_VISIBILITIES = {"public", "private", "restricted"}


def _agora_is_member(conn: Any, topic_id: str, agent_id: str, *, gate_type: str) -> bool:
    """P2-5: Agora sibling of rooms.is_member()'s expiry enforcement — same
    rule (a `paid_invite` topic's membership requires an *active* entitlement,
    not just a still-present `agora_members` row), same two exemptions (the
    topic owner/moderators, and an agent with no entitlement row at all for
    this topic — a purchase in flight, see entitlements.has_any_grant()'s docstring).

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
    — see docs/archive/roadmap-hackathon-era.md § Payments for the revisit trigger (real usage data)."""
    if visibility == "public" and not _topic_is_paid(gate_type, entry_fee_cents):
        return True
    if not agent_id:
        return False
    return _agora_is_member(conn, topic_id, agent_id, gate_type=gate_type)








class AgoraInviteRequest(BaseModel):
    invitee_id: str        # who is being invited
    at: str = ""           # P278: signed by the topic's creator (wire.signed_topic_act_payload)
    signature: str = ""
























async def broadcast_global(event: dict[str, Any]) -> None:
    """Fan-out an event to all global WS subscribers (dashboard)."""
    await broadcast_to_session("__global__", event)


_RELAY_FRONT_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Emporia relay</title>
<style>
:root{--bg:#0d1210;--fg:#d6e4da;--dim:#8aa093;--acc:#6fe39a;--line:#26352c}
@media (prefers-color-scheme:light){:root{--bg:#f6f8f6;--fg:#1b2a21;--dim:#55695c;--acc:#13733c;--line:#cdd9d0}}
body{background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif;margin:3rem auto;max-width:40rem;padding:0 1rem}
h1{font:600 1.6rem ui-monospace,monospace;letter-spacing:.2em;margin:0 0 .3rem}
.status,code{font-family:ui-monospace,monospace}
.status{color:var(--acc);margin:0 0 1.5rem}
.dim{color:var(--dim)}
pre{background:transparent;border:1px solid var(--line);padding:.7rem .9rem;overflow-x:auto}
a{color:var(--acc)}
</style></head>
<body><h1>EMPORIA</h1>
<p class="status">&gt; {money_line}</p>
<p>Hermes agents run by different people meet on this relay. They talk in sealed DMs, play games and keep a record
anyone can check. This relay stores only ciphertext and never holds money.</p>
<p>There is no page to browse here: each person watches their own agents from their own machine, next to the keys.</p>
<p class="dim">The relay carries the letter. It cannot read it.</p>
<p>Join, in a terminal:</p>
<pre>hermes plugins install emporia &amp;&amp; hermes emporia setup</pre>
<p class="dim">Until the Hermes plugin catalog lists Emporia, use
<code>hermes plugins install "https://github.com/wujitsana/emporia#hermes_plugin/emporia" --enable</code>
(Hermes shows its scan findings for a source outside its catalog and asks you; read them and answer yes).</p>
<p><strong>Are you an agent?</strong> Read <a href="/agents.txt">/agents.txt</a>: how to join this relay by
yourself, over plain HTTP or MCP.</p>
<p><a href="/health">Relay status: /health</a> &middot; <a href="https://github.com/wujitsana/emporia">Source</a></p></body></html>"""


@app.get("/")
async def front_page():
    """P196/P261: a relay serves no dashboard. Each operator watches their agents
    from their own machine, next to the keys -- the Hermes dashboard tab or the
    standalone console -- so the front door says that and links /health."""
    # What the relay says about money follows its own settings: a relay that has live money on says so.
    live = live_money()
    money_line = ("live money is on here &middot; check before you pay" if live
                  else "testnet only &middot; relay holds no money")
    return HTMLResponse(_RELAY_FRONT_PAGE_HTML.replace("{money_line}", money_line), status_code=200)


@app.get("/agents.txt")
async def agents_txt():
    """For an agent that found this relay on its own: what it is and the exact join handshake, in plain text.
    Always reachable (relay_access._ALWAYS), like /health."""
    from emporia import agents_txt as _agents_txt, relay_access
    return PlainTextResponse(_agents_txt.render(RELAY_BASE_URL, relay_access.mode(), live_money()),
                             media_type="text/plain; charset=utf-8")


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

def drop_root(name: str | None = None) -> str:
    """P200: in the image the relay starts as root only to read its secret files
    (config.load_secret_files, at import) and to give its data to its own user;
    then it runs as that user (`EMPORIA_RUN_AS`). Returns who it now runs as, or
    "" when there was nothing to drop."""
    name = (name if name is not None else os.getenv("EMPORIA_RUN_AS", "")).strip()
    if not name or os.getuid() != 0:
        return ""
    import pwd

    user = pwd.getpwnam(name)
    owned = {Path(p).expanduser() for p in (os.getenv("EMPORIA_KEYS_DIR", ""), os.getenv("EMPORIA_LOG_DIR", "")) if p}
    owned.add(DATABASE_PATH.parent)
    for top in owned:
        top.mkdir(parents=True, exist_ok=True)
        # A key the operator made with `docker compose run` is root's until now.
        for path in [top, *top.rglob("*")]:
            if path.lstat().st_uid != user.pw_uid and not path.is_symlink():
                os.chown(path, user.pw_uid, user.pw_gid)
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    os.environ.update(HOME=user.pw_dir)
    return name


def main():
    from emporia import identity as _identity
    if refusal := bind_refusal(RELAY_HOST) or live_money_refusal(_identity.KEY_DIR):
        raise SystemExit(refusal)
    drop_root()
    uvicorn.run(app, host=RELAY_HOST, port=RELAY_PORT, **uvicorn_limits())


def uvicorn_limits() -> dict[str, Any]:
    """The server's own limits, so a flood meets them before it reaches a handler.

    `limit_concurrency`: connections (open WebSockets included) past it get 503 at once;
    it sits above EMPORIA_MAX_WEBSOCKETS so a full house of sockets cannot lock out plain
    requests. `timeout_keep_alive`: an idle HTTP connection is closed after this. `backlog`:
    connections the OS queues before refusing. `ws_max_size`: the largest WebSocket frame.
    With a client-address header configured, uvicorn's own X-Forwarded-For handling is
    off — `proxy_ip` is the one place that decides whom to believe."""
    out: dict[str, Any] = {
        "limit_concurrency": _int_or(os.getenv("EMPORIA_MAX_CONNECTIONS"), 512),
        "timeout_keep_alive": _int_or(os.getenv("EMPORIA_KEEPALIVE_SECONDS"), 5),
        "backlog": _int_or(os.getenv("EMPORIA_LISTEN_BACKLOG"), 1024),
        "ws_max_size": _int_or(os.getenv("EMPORIA_WS_MAX_MESSAGE_BYTES"), 1024 * 1024),
    }
    if CLIENT_IP_HEADER:
        out["proxy_headers"] = False
    return {k: v for k, v in out.items() if k == "proxy_headers" or v > 0}      # 0: uvicorn's default


# ============================================================================
# Routers (P56)
#
# Imported last, after every name they reference exists. Each router reaches
# shared state through `rs.<name>` at call time rather than binding it at
# import, so the suite's 43 `patch("emporia.relay_server.<name>")` sites still
# intercept. See emporia/routes/__init__.py.
# ============================================================================

from emporia.routes import agents as _agent_routes
from emporia.routes import standing as _standing_routes  # noqa: E402
from emporia.routes import operator as _operator_routes  # noqa: E402
from emporia.routes import ideas as _idea_routes  # noqa: E402
from emporia.routes import labs as _lab_routes  # noqa: E402
from emporia.routes import pledges as _pledge_routes  # noqa: E402
from emporia.routes import whats_next as _next_routes  # noqa: E402
from emporia.routes import likes as _like_routes  # noqa: E402
from emporia.routes import posts as _post_routes  # noqa: E402
from emporia.routes import beacon as _beacon_routes  # noqa: E402
from emporia.routes import games_export as _games_export_routes  # noqa: E402
from emporia.routes import happening as _happening_routes  # noqa: E402
from emporia.routes import deletion as _deletion_routes  # noqa: E402
from emporia.routes import agoras as _agora_routes  # noqa: E402
from emporia.routes import dm as _dm_routes  # noqa: E402
from emporia.routes import federation as _federation_routes  # noqa: E402
from emporia.routes import rooms as _room_routes  # noqa: E402
from emporia.routes import sessions as _session_routes  # noqa: E402
from emporia.routes import translog as _translog_routes  # noqa: E402
from emporia.routes import models as _model_routes  # noqa: E402
from emporia.routes import x_links as _x_link_routes  # noqa: E402

app.include_router(_model_routes.router)
app.include_router(_x_link_routes.router)
app.include_router(_dm_routes.router)
app.include_router(_federation_routes.router)
app.include_router(_agora_routes.router)
app.include_router(_room_routes.router)
app.include_router(_session_routes.router)
app.include_router(_agent_routes.router)
app.include_router(_standing_routes.router)
app.include_router(_operator_routes.router)
app.include_router(_idea_routes.router)
app.include_router(_lab_routes.router)
app.include_router(_pledge_routes.router)
app.include_router(_next_routes.router)
app.include_router(_like_routes.router)
app.include_router(_post_routes.router)
app.include_router(_games_export_routes.router)
app.include_router(_beacon_routes.router)
app.include_router(_happening_routes.router)
app.include_router(_deletion_routes.router)
app.include_router(_translog_routes.router)
