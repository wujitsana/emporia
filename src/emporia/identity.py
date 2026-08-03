"""Ed25519 identity module for Emporia.

Provides keypair generation/loading, payload signing/verification, the relay
descriptor (not an A2A Agent Card — see build_relay_descriptor()), and
content-address derivation. No blockchain, no wallets.

Private keys are stored locally at ~/.hermes/keys/{profile_id}.priv (raw bytes,
0o600 permissions). They never touch the relay and are never committed to git.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

KEY_DIR = Path(os.getenv("EMPORIA_KEYS_DIR", "~/.hermes/keys")).expanduser()
_HANDLE_RE = re.compile(r"[^a-z0-9]+")


# ============================================================================
# Keypair lifecycle
# ============================================================================

def _key_path(profile_id: str) -> Path:
    safe = _HANDLE_RE.sub("_", profile_id.strip().lower()).strip("_") or "default"
    return KEY_DIR / f"{safe}.priv"


def _load_or_generate_at(path: Path) -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes()
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
    else:
        priv = ed25519.Ed25519PrivateKey.generate()
        raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.write_bytes(raw)
        path.chmod(0o600)
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes


def generate_or_load_keypair(profile_id: str) -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    """Return (private_key, public_key_bytes), generating and persisting if needed."""
    return _load_or_generate_at(_key_path(profile_id))


def get_public_key_hex(profile_id: str) -> str:
    """Return the hex public key for a profile, generating the keypair if needed."""
    _, pub_bytes = generate_or_load_keypair(profile_id)
    return pub_bytes.hex()


# Reserved path outside _key_path()'s sanitized profile_id → filename mapping, so no
# agent-chosen agent_id (unrestricted free-form string) can ever collide with it —
# _key_path() always strips leading/trailing underscores, so a leading-underscore
# filename is unreachable from any sanitized profile_id.
_RELAY_KEY_PATH_NAME = "_relay_identity.priv"


_RELAY_KEYPAIR_CACHE: tuple[ed25519.Ed25519PrivateKey, bytes] | None = None


def generate_or_load_relay_keypair() -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    """Return this relay's own persistent (private_key, public_key_bytes) — a single
    identity for the relay process itself, distinct from any agent's keypair. Used to sign
    outbound federation gossip batches so peers can verify authenticity/integrity
    independent of which agent happens to be the relay operator.

    Cached after first load — unlike per-agent keypairs (which vary by profile_id and are
    comparatively cold-path), this is called at least twice per federation-serve request
    (get_relay_public_key_hex() + sign_raw_relay()), so re-reading the key file from disk
    every call is pure overhead once the relay process has loaded it once.
    """
    global _RELAY_KEYPAIR_CACHE
    if _RELAY_KEYPAIR_CACHE is None:
        _RELAY_KEYPAIR_CACHE = _load_or_generate_at(KEY_DIR / _RELAY_KEY_PATH_NAME)
    return _RELAY_KEYPAIR_CACHE


def get_relay_public_key_hex() -> str:
    _, pub_bytes = generate_or_load_relay_keypair()
    return pub_bytes.hex()


def sign_raw_relay(data: bytes) -> str:
    """Sign raw bytes with this relay's own Ed25519 key. Returns lowercase hex signature."""
    priv, _ = generate_or_load_relay_keypair()
    return priv.sign(data).hex()


# ============================================================================
# Dashboard JWT HMAC secret (persisted, not per-process)
# ============================================================================
# Stored beside the relay's own key, same 0o600 model. Previously generated fresh via
# secrets.token_bytes(32) at relay_server.py import time — meaning every relay restart
# invalidated every outstanding dashboard/agent Bearer token relay-wide, and any
# multi-worker deployment had each worker mint tokens the others couldn't verify. See
# SECURITY.md. Lazily loaded and cached here (not at relay_server.py import time) for the
# same import-order reason generate_or_load_relay_keypair() and the federation pins are —
# tests patch identity.KEY_DIR via a fixture that runs after module import/collection.

_JWT_SECRET_FILE_NAME = "_relay_jwt_secret.bin"
_RELAY_JWT_SECRET_CACHE: bytes | None = None


def load_or_generate_relay_jwt_secret() -> bytes:
    """Return this relay's persistent JWT HMAC secret (32 raw bytes), generating and
    persisting it on first use. Cached after first load — called on every JWT
    encode/decode, so re-reading the file every request would be pure overhead."""
    global _RELAY_JWT_SECRET_CACHE
    if _RELAY_JWT_SECRET_CACHE is None:
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        path = KEY_DIR / _JWT_SECRET_FILE_NAME
        if path.exists():
            _RELAY_JWT_SECRET_CACHE = path.read_bytes()
        else:
            secret = secrets.token_bytes(32)
            path.write_bytes(secret)
            path.chmod(0o600)
            _RELAY_JWT_SECRET_CACHE = secret
    return _RELAY_JWT_SECRET_CACHE


# ============================================================================
# Federation peer key pinning (trust-on-first-use, persisted)
# ============================================================================
# Stored beside the relay's own key (same directory, same 0o600 model) so it survives
# container recreation — the Docker deployment mounts KEY_DIR (/data/keys) as a durable
# volume. Kept in this module rather than relay_server.py so the file path always tracks
# KEY_DIR's live value (including test patches via patch.object(identity, "KEY_DIR", ...)),
# the same reason generate_or_load_relay_keypair() lives here rather than in relay_server.py.

_PEER_PUBKEYS_FILE_NAME = "federation_peer_pubkeys.json"


def load_federation_peer_pubkeys() -> dict[str, str]:
    """Return the persisted {peer_url: pubkey_hex} pin map, or {} if none exists yet."""
    path = KEY_DIR / _PEER_PUBKEYS_FILE_NAME
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_federation_peer_pubkeys(pubkeys: dict[str, str]) -> None:
    """Persist the full {peer_url: pubkey_hex} pin map. Best-effort: a write failure
    (read-only filesystem, permissions) degrades to in-memory-only pinning for this
    process's lifetime rather than raising — pinning is a hardening measure, not a
    correctness dependency the relay should crash over.
    """
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    path = KEY_DIR / _PEER_PUBKEYS_FILE_NAME
    try:
        path.write_text(json.dumps(pubkeys, sort_keys=True, indent=2))
        path.chmod(0o600)
    except OSError:
        pass


# ============================================================================
# Signing / verification
# ============================================================================

def sign(payload: dict[str, Any], profile_id: str) -> str:
    """Sign payload (deterministic sort_keys JSON) with profile's Ed25519 key.
    Returns base64url-encoded signature (no padding).
    """
    priv, _ = generate_or_load_keypair(profile_id)
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig_bytes = priv.sign(serialized)
    return base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode("ascii")


def sign_raw(data: bytes, profile_id: str) -> str:
    """Sign raw bytes with profile's Ed25519 key. Returns lowercase hex signature."""
    priv, _ = generate_or_load_keypair(profile_id)
    return priv.sign(data).hex()


def verify(payload: dict[str, Any], sig_b64: str, pubkey_hex: str) -> bool:
    """Verify Ed25519 signature over payload. Returns True if valid."""
    try:
        pub_bytes = bytes.fromhex(pubkey_hex)
        # Accept both padded and unpadded base64url
        padding = "=" * (-len(sig_b64) % 4)
        sig_bytes = base64.urlsafe_b64decode(sig_b64 + padding)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        pub_key.verify(sig_bytes, serialized)
        return True
    except Exception:
        return False


def verify_raw(data: bytes, sig_hex: str, pubkey_hex: str) -> bool:
    """Verify a hex Ed25519 signature over raw bytes. Counterpart to sign_raw()."""
    try:
        pub_bytes = bytes.fromhex(pubkey_hex)
        sig_bytes = bytes.fromhex(sig_hex)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig_bytes, data)
        return True
    except Exception:
        return False


def verify_handshake_proof(challenge_frame: dict[str, Any], signature_hex: str) -> bool:
    """Verify a challenge-response handshake proof (hex signature over sorted JSON challenge)."""
    try:
        pub_bytes = bytes.fromhex(challenge_frame["public_key"])
        sig_bytes = bytes.fromhex(signature_hex)
        pub_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
        serialized = json.dumps(challenge_frame, sort_keys=True).encode("utf-8")
        pub_key.verify(sig_bytes, serialized)
        return True
    except Exception:
        return False


# ============================================================================
# Relay descriptor — Emporia's own document, NOT an A2A Agent Card. It does not
# live at A2A's reserved discovery path (/.well-known/agent[-card].json — see
# relay_server.py's a2a_well_known_not_supported()) and does not validate
# against the A2A AgentCard schema (no `skills`, no `supportedInterfaces`, no
# JSON-RPC endpoint behind it). Served at GET /relay-card.
# ============================================================================

def build_relay_descriptor(
    agent_id: str,
    base_url: str,
    public_key_hex: str,
    display_name: str | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Build Emporia's relay descriptor: the owner agent's identity, capability
    list, and endpoint map, as served at GET /relay-card."""
    base = base_url.rstrip("/")
    return {
        "version": "1.0",
        "agent_id": agent_id,
        "display_name": display_name or agent_id,
        "base_url": base,
        "publicKey": public_key_hex,
        "publicKeyEncoding": "ed25519-raw-hex",
        "capabilities": capabilities or [],
        "endpoints": {
            "listings": f"{base}/listings",
            "sessions": f"{base}/sessions",
            "messages": f"{base}/messages",
            "dm": f"{base}/dm",
            "lobby": f"{base}/gaming/lobby",
            "rooms": f"{base}/rooms",
            "agoras": f"{base}/agoras/topics",
        },
    }


# ============================================================================
# Content-addressed ID derivation
# ============================================================================

def content_address_for(name: str, nous_user_id: str | None = None) -> str:
    """Derive a deterministic 20-byte content address (no 0x prefix).

    Uses nous_user_id as identity material when available, enabling the same
    Nous user to resolve to the same player_id across machines.
    """
    normalized = _HANDLE_RE.sub("_", name.strip().lower()).strip("_") or "player"
    material = nous_user_id.strip() if nous_user_id else normalized
    digest = hashlib.sha3_256(f"emporia:v1:{normalized}:{material}".encode()).hexdigest()
    return digest[-40:]  # 20 bytes, no 0x prefix
