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
import time
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


def _write_secret_atomically(path: Path, data: bytes) -> None:
    """Create `path` containing `data`, 0600 from the instant it exists, atomically.

    Replaces the previous write_bytes()-then-chmod() pattern, which lacked two
    properties every caller here needs: the file is never briefly world-readable
    (O_CREAT honours the 0o600 mode; umask can only clear bits, never set them),
    and a crash mid-write truncates the *temp* file, not the real one — every
    caller here branches on path.exists(), so a truncated real file would never
    self-heal.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# P12: _load_or_generate_at() serves two callers whose corrupt-key recovery paths are
# not the same action. An agent key's fix is local (re-register, or restore from
# backup). The relay's own federation-signing key is pinned by every peer that has
# ever synced with it (TOFU, see save_federation_peer_pubkeys()) — deleting it doesn't
# register a "new identity" locally, it makes every peer reject the replacement outright
# as a `peer key mismatch`, a fix each peer operator then has to apply on their own box.
_AGENT_KEY_RECOVERY_HINT = (
    "Restore {path} from backup, or delete it to register as a new identity."
)
_RELAY_KEY_RECOVERY_HINT = (
    "this is the relay's federation signing identity, and a new public key is "
    "rejected outright by every peer that has pinned the old one (`peer key "
    "mismatch`), which each peer operator must then clear on their own box. "
    "Restore {path} from backup; deleting it is not a local recovery."
)


def _load_or_generate_at(
    path: Path, recovery_hint: str = _AGENT_KEY_RECOVERY_HINT,
) -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    # mode= only applies when this call actually creates the directory — an
    # already-existing KEY_DIR (e.g. an existing deployment) keeps its current mode.
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes()
        try:
            priv = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
        except ValueError as e:
            # A truncated write (crash, disk-full) is the only way this file gets
            # here at the wrong length — _write_secret_atomically() means a
            # completed write is always exactly 32 bytes. Deliberately not
            # regenerating: a new key would change this identity's public key,
            # so recovery has to be a human decision, not something this function
            # decides on its own — recovery_hint says what that decision is for
            # this specific caller (see _AGENT_KEY_RECOVERY_HINT /
            # _RELAY_KEY_RECOVERY_HINT above).
            raise ValueError(
                f"private key file is corrupt (unrecoverable): {path} is "
                f"{len(raw)} bytes, expected 32 — refusing to silently "
                f"regenerate, since that would mint a new public key. "
                + recovery_hint.format(path=path)
            ) from e
    else:
        priv = ed25519.Ed25519PrivateKey.generate()
        raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _write_secret_atomically(path, raw)
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes


def generate_or_load_keypair(profile_id: str) -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    """Return (private_key, public_key_bytes), generating and persisting if needed."""
    return _load_or_generate_at(_key_path(profile_id))


def get_public_key_hex(profile_id: str) -> str:
    """Return the hex public key for a profile, generating the keypair if needed."""
    _, pub_bytes = generate_or_load_keypair(profile_id)
    return pub_bytes.hex()


# ============================================================================
# X25519 encryption keys (P16) — key agreement for end-to-end encrypted DMs
# ============================================================================
# A *separate* key from the Ed25519 identity above, deliberately. Ed25519 and
# X25519 keys are birationally related and libsodium exposes a conversion, but
# `cryptography` does not, and reusing one key across signing and key agreement is
# a sharp edge with no upside here. The cost is one more file per agent; the
# benefit is that a compromise or rotation of one never implies the other.
#
# Same storage contract as the signing key: raw 32 bytes under KEY_DIR, 0600 from
# creation via _write_secret_atomically(), never transmitted, and never silently
# regenerated when corrupt.

_ENC_KEY_RECOVERY_HINT = (
    "Restore {path} from backup. Deleting it mints a new encryption key, which "
    "every peer that has pinned the old one will then reject — and it makes every "
    "message already sealed to the old key permanently unreadable."
)


def _enc_key_path(profile_id: str) -> Path:
    # Same sanitization as _key_path(), different suffix — so the two keys for one
    # profile sit side by side and neither can ever land on the other's filename.
    safe = _HANDLE_RE.sub("_", profile_id.strip().lower()).strip("_") or "default"
    return KEY_DIR / f"{safe}.x25519.priv"


def generate_or_load_encryption_keypair(profile_id: str) -> tuple[Any, bytes]:
    """Return (X25519 private key, public key bytes), generating and persisting
    on first use.

    Not routed through `_load_or_generate_at()`: that function is typed to
    Ed25519 all the way through (`ed25519.Ed25519PrivateKey.from_private_bytes`,
    and a recovery hint written for a signing identity). The failure *policy* is
    copied deliberately — a corrupt file raises rather than regenerating, because
    a new public key here is worse than for the signing key: peers reject it on
    their pin, and every message already sealed to the old key becomes
    permanently unreadable.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    path = _enc_key_path(profile_id)
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes()
        try:
            priv = X25519PrivateKey.from_private_bytes(raw)
        except ValueError as e:
            raise ValueError(
                f"encryption key file is corrupt (unrecoverable): {path} is "
                f"{len(raw)} bytes, expected 32 — refusing to silently regenerate. "
                + _ENC_KEY_RECOVERY_HINT.format(path=path)
            ) from e
    else:
        priv = X25519PrivateKey.generate()
        _write_secret_atomically(path, priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    return priv, priv.public_key().public_bytes_raw()


def get_encryption_public_key_hex(profile_id: str) -> str:
    """The hex X25519 public key to publish to the relay, generating on first use."""
    _, pub_bytes = generate_or_load_encryption_keypair(profile_id)
    return pub_bytes.hex()


def encryption_key_fingerprint(public_key_hex: str) -> str:
    """A short, human-comparable digest of an encryption public key.

    First contact is trust-on-first-use, and TOFU is only fully closed by two
    people comparing keys over a channel the relay does not control. A 64-char
    hex string is not something anyone compares correctly by eye; this is.
    """
    digest = hashlib.sha256(public_key_hex.strip().lower().encode("ascii")).hexdigest()
    return ":".join(digest[i:i + 4] for i in range(0, 16, 4))


# ============================================================================
# Peer encryption-key pinning (trust-on-first-use, persisted)
# ============================================================================
# The relay serves peer encryption keys, so an *active* hostile relay's one
# remaining move against E2E DMs is handing out a key it holds. Pinning turns
# that from "undetectable, works forever" into "works once, against a peer you
# have never messaged, and is permanent evidence afterwards".
#
# Stored beside the keys themselves, exactly like federation_peer_pubkeys.json,
# and for the same reason: it must survive container recreation, and keeping the
# path in this module means it tracks KEY_DIR's live value (including test
# patches) rather than a value captured at import time.

_PEER_ENC_KEYS_FILE_NAME = "peer_encryption_keys.json"


class PeerKeyMismatch(Exception):
    """A peer's encryption key changed after it was pinned. Raised rather than
    warned: continuing would seal the message to whatever key was just served,
    which is precisely the attack pinning exists to stop."""


def _valid_x25519_pubkey_hex(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def load_peer_encryption_keys() -> dict[str, str]:
    """Return the persisted {agent_id: x25519_pubkey_hex} pin map, or {} if none.

    Missing file = ordinary first run, returns {} silently. Corrupt file follows
    `load_federation_peer_pubkeys()`'s policy exactly: log loudly, rename the
    damaged file aside so the next save can't destroy the evidence, and return {}
    — never self-heal, because silently regenerating an empty map *is* the
    invisible-TOFU-reset bug rather than a fix for it.
    """
    path = KEY_DIR / _PEER_ENC_KEYS_FILE_NAME
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        import logging
        logging.getLogger("emporia.identity").warning(
            "peer encryption-key pin map at %s is corrupt/unreadable (%s) — every "
            "previously-pinned peer is being reset to trust-on-first-use; preserving "
            "the damaged file for inspection", path, e,
        )
        try:
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return {}


def save_peer_encryption_keys(pins: dict[str, str]) -> None:
    """Persist the full pin map.

    Unlike `save_federation_peer_pubkeys()`, a write failure **raises**. That
    sibling is best-effort because federation pinning is hardening the relay can
    run without; here a pin that fails to persist means the next send silently
    re-TOFUs and would accept a substituted key — the exact failure the caller
    asked to be protected from. Nor does this newly restrict anyone: the X25519
    private key already requires this same directory to be writable, so a
    deployment that cannot write here cannot do encrypted DMs at all.
    """
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_secret_atomically(
        KEY_DIR / _PEER_ENC_KEYS_FILE_NAME,
        json.dumps(pins, sort_keys=True, indent=2).encode(),
    )


def pin_peer_encryption_key(agent_id: str, public_key_hex: str) -> str:
    """Trust-on-first-use: pin `public_key_hex` for `agent_id` and return it, or
    raise `PeerKeyMismatch` if a *different* key was already pinned.

    Call this on every fetched peer key, not just the first — the check is the
    point. A malformed or empty key is rejected outright (`ValueError`) rather
    than pinned: an agent who never published one comes back as "", and pinning
    that would make the next substituted key "match" nothing at all.
    """
    key = (public_key_hex or "").strip().lower()
    if not _valid_x25519_pubkey_hex(key):
        raise ValueError(
            f"'{agent_id}' has no usable encryption key published "
            f"(got {public_key_hex!r}) — they must publish one before you can "
            f"send them encrypted messages"
        )
    pins = load_peer_encryption_keys()
    existing = pins.get(agent_id)
    if existing and existing != key:
        raise PeerKeyMismatch(
            f"the encryption key served for '{agent_id}' does not match the one "
            f"pinned on first contact (pinned {encryption_key_fingerprint(existing)}, "
            f"served {encryption_key_fingerprint(key)}). Refusing to encrypt. If "
            f"they legitimately rotated, confirm the new fingerprint out of band "
            f"and call trust_new_encryption_key('{agent_id}', ...)."
        )
    if not existing:
        pins[agent_id] = key
        save_peer_encryption_keys(pins)
    return key


def trust_new_encryption_key(agent_id: str, public_key_hex: str) -> None:
    """Replace a pin after an out-of-band fingerprint check — the explicit escape
    hatch for legitimate rotation. Separate from `pin_peer_encryption_key()` on
    purpose: overwriting a pin must be something a human chose, never something a
    send path does on its own."""
    key = (public_key_hex or "").strip().lower()
    if not _valid_x25519_pubkey_hex(key):
        raise ValueError(f"not a valid X25519 public key: {public_key_hex!r}")
    pins = load_peer_encryption_keys()
    pins[agent_id] = key
    save_peer_encryption_keys(pins)


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
        _RELAY_KEYPAIR_CACHE = _load_or_generate_at(
            KEY_DIR / _RELAY_KEY_PATH_NAME, recovery_hint=_RELAY_KEY_RECOVERY_HINT,
        )
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
        KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = KEY_DIR / _JWT_SECRET_FILE_NAME
        existing = path.read_bytes() if path.exists() else b""
        if len(existing) == 32:
            _RELAY_JWT_SECRET_CACHE = existing
        else:
            # A truncated write (crash, disk-full) is the only way a persisted
            # secret ends up here at the wrong length — _write_secret_atomically()
            # means a completed write is always exactly 32 bytes. Unlike a corrupt
            # private key, regenerating here is safe: the blast radius is only
            # invalidating outstanding bearer tokens (an availability blip), versus
            # silently continuing to HMAC-sign every dashboard/agent token with a
            # shorter, weaker key.
            secret = secrets.token_bytes(32)
            _write_secret_atomically(path, secret)
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
    """Return the persisted {peer_url: pubkey_hex} pin map, or {} if none exists yet.

    A missing file is the ordinary first-run case and returns {} silently. A file that
    exists but is corrupt/unreadable is different: silently returning {} there resets
    every previously-pinned peer back to trust-on-first-use, invisibly, and the next
    save_federation_peer_pubkeys() call would overwrite the corrupt file with a fresh
    one, destroying the evidence anything was wrong. That case is logged loudly and the
    corrupt file is preserved (renamed aside) before still returning {} — deliberately
    NOT raised: none of this function's three call sites (relay_server.py) wrap it in a
    try/except, and the federation-sync loop that calls two of them has no per-peer
    exception boundary — raising here would abort sync for every configured peer, and
    500 the dashboard's own peer-status diagnostic view, on the strength of one damaged
    file. Self-healing (this repo's policy for the JWT secret) is wrong here for the
    opposite reason: silently regenerating an empty map IS the invisible-TOFU-reset bug,
    not a fix for it.
    """
    path = KEY_DIR / _PEER_PUBKEYS_FILE_NAME
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        import logging
        logging.getLogger("emporia.relay").warning(
            "federation peer pin map at %s is corrupt/unreadable (%s) — every "
            "previously-pinned peer is being reset to trust-on-first-use; "
            "preserving the damaged file for inspection", path, e,
        )
        try:
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass  # best-effort, same as save_federation_peer_pubkeys()'s own contract
        return {}


def save_federation_peer_pubkeys(pubkeys: dict[str, str]) -> None:
    """Persist the full {peer_url: pubkey_hex} pin map. Best-effort: a write failure
    (read-only filesystem, permissions) degrades to in-memory-only pinning for this
    process's lifetime rather than raising — pinning is a hardening measure, not a
    correctness dependency the relay should crash over.
    """
    path = KEY_DIR / _PEER_PUBKEYS_FILE_NAME
    try:
        # P12: KEY_DIR.mkdir() moved inside this try — it used to sit before it, so a
        # read-only KEY_DIR parent (the read-only-container scenario this docstring's
        # own "best-effort" contract exists for) raised OSError straight through it,
        # out of the mid-federation-pull caller in relay_server.py.
        KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_secret_atomically(path, json.dumps(pubkeys, sort_keys=True, indent=2).encode())
    except OSError:
        pass


def normalize_peer_url(url: str) -> str:
    """Canonical form for a peer relay URL used as a dict key or stored value in
    federation state (N13-2, R2).

    Both `_pull_federated_listings`/`_pull_federated_lobby` (relay_server.py) and
    `sync_lobby_from_peer` (mcp_server.py) build their outbound request URL with
    `.rstrip("/")`, but every *stored* value derived from their `peer_url` parameter —
    the pin-store key, `trusted_origin` passed to `import_challenge()`, the
    `_FEDERATION_*_LAST_SYNC` dict key — used the raw, un-normalized parameter. A peer
    configured or called as `"https://p/"` and the same peer called as `"https://p"`
    landed as two different keys for the same relay, which meant a peer's own later,
    legitimate re-sync could be rejected by N12-3's ownership check as belonging to a
    "different origin relay."

    Strips only surrounding whitespace and a trailing slash — that's the demonstrated
    failure mode. Does not lower-case scheme/host: no live case-sensitivity bug was
    found, and normalizing case risks new drift for a URL with a case-sensitive path
    segment, for no demonstrated benefit.
    """
    return url.strip().rstrip("/")


def normalize_peer_pubkey_keys(pins: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Re-key an already-loaded {peer_url: pubkey_hex} pin map to normalize_peer_url()'s
    form (N13-2). Returns (migrated_map, changed) so a caller only re-persists when the
    key shape actually moved — corrects a pin file written before this fix, where a
    peer could have been pinned under an un-normalized key (e.g. a trailing slash).

    If two raw keys normalize to the same value (only reachable via the pre-fix bug
    this migrates away from — self-healing after this fix ships), the entry that
    sorts later wins, matching this file's own sort_keys=True on-disk order; this is
    not treated as an ambiguous/tampered state requiring a human decision, since both
    raw keys came from this relay's own prior pinning of what was, in reality, the
    same peer.
    """
    migrated: dict[str, str] = {}
    changed = False
    for raw_key in sorted(pins):
        key = normalize_peer_url(raw_key)
        if key != raw_key:
            changed = True
        migrated[key] = pins[raw_key]
    return migrated, changed


# ============================================================================
# Federation batch canonicalization (shared by relay_server.py and mcp_server.py)
# ============================================================================

def federation_signable_bytes(origin_relay: str, key: str, items: list[dict]) -> bytes:
    """Canonical bytes signed over a federation batch (listings, lobby challenges, ...).

    Must produce the exact same output whether called on the freshly-built payload
    (serving side) or the same payload after a JSON round-trip over HTTP (pulling
    side) — sort_keys + compact separators makes this deterministic regardless of dict
    insertion order. `key` names the payload's item list field (e.g. "listings",
    "challenges") so every federation feed shares this one canonicalizer rather than
    each growing its own, subtly-different copy (N11-5, R5).
    """
    payload = {"origin_relay": origin_relay, key: items}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
