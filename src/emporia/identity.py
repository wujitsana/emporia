"""Ed25519 identity module for Emporia.

Provides keypair generation/loading, payload signing/verification, Agent Card
construction, and content-address derivation. No blockchain, no wallets.

Private keys are stored locally at ~/.hermes/keys/{profile_id}.priv (raw bytes,
0o600 permissions). They never touch the relay and are never committed to git.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
# Agent Card (A2A /.well-known/agent.json convention)
# ============================================================================

def build_agent_card(
    agent_id: str,
    base_url: str,
    capabilities: list[str] | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Build an A2A-compatible Agent Card with Ed25519 public key."""
    pub_hex = get_public_key_hex(profile_id or agent_id)
    return {
        "version": "1.0",
        "agent_id": agent_id,
        "base_url": base_url.rstrip("/"),
        "publicKey": pub_hex,
        "publicKeyEncoding": "ed25519-raw-hex",
        "capabilities": capabilities or [],
        "endpoints": {
            "listings": f"{base_url.rstrip('/')}/listings",
            "sessions": f"{base_url.rstrip('/')}/sessions",
            "messages": f"{base_url.rstrip('/')}/messages",
            "lobby": f"{base_url.rstrip('/')}/gaming/lobby",
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
