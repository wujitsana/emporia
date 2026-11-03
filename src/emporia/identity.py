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

# P103: merge the profile .env *before* reading EMPORIA_KEYS_DIR. KEY_DIR is
# fixed when this module is imported, so a process that imported identity before
# anything had loaded the profile settings got ~/.hermes/keys instead of the
# profile's key directory -- and since get_public_key_hex() generates a key when
# none exists, it then silently minted a new identity there. Found when a
# verification script did exactly that. config imports nothing from emporia, so
# importing it here cannot cycle.
from emporia import config as _config  # noqa: E402,F401

KEY_DIR = Path(os.getenv("EMPORIA_KEYS_DIR", "~/.hermes/keys")).expanduser()
_HANDLE_RE = re.compile(r"[^a-z0-9]+")
# P113: key files keep `-`. The relay accepts `[a-z0-9_-]` in an agent id, and
# mapping `-` to `_` here sent `my-agent` and `my_agent` — two identities on the
# relay — to one key file (and one x402 wallet) in a shared key directory.
_KEYFILE_RE = re.compile(r"[^a-z0-9_-]+")


def _keyfile_stem(profile_id: str) -> str:
    return _KEYFILE_RE.sub("_", profile_id.strip().lower()).strip("_-") or "default"


# ============================================================================
# Keypair lifecycle
# ============================================================================

def _key_path(profile_id: str) -> Path:
    return KEY_DIR / f"{_keyfile_stem(profile_id)}.priv"


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


# P164: the identity and encryption keys may live in a password manager or an
# environment variable instead of a file (see key_source). Read once per process.
IDENTITY_KEY_SOURCE_VAR = "EMPORIA_IDENTITY_KEY_SOURCE"
ENCRYPTION_KEY_SOURCE_VAR = "EMPORIA_ENCRYPTION_KEY_SOURCE"
_LINKED: dict[str, bytes] = {}


def linked_key(var: str) -> bytes | None:
    """The 32-byte key the source in `var` gives, or None when none is set."""
    ref = os.environ.get(var, "").strip()
    if not ref:
        return None
    if ref not in _LINKED:
        from emporia.key_source import key32
        _LINKED[ref] = key32(ref)
    return _LINKED[ref]


def generate_or_load_keypair(profile_id: str) -> tuple[ed25519.Ed25519PrivateKey, bytes]:
    """Return (private_key, public_key_bytes), generating and persisting if needed."""
    linked = linked_key(IDENTITY_KEY_SOURCE_VAR)
    if linked is not None:
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(linked)
        return priv, priv.public_key().public_bytes_raw()
    from emporia import key_source
    if key_source.store_in_env() and not _key_path(profile_id).exists():
        # P165: a new agent keeps its identity key in its profile .env.
        priv = ed25519.Ed25519PrivateKey.generate()
        key_source.save_key_to_env("identity", priv.private_bytes_raw().hex())
        return priv, priv.public_key().public_bytes_raw()
    return _load_or_generate_at(_key_path(profile_id))


def get_public_key_hex(profile_id: str) -> str:
    """Return the hex public key for a profile, generating the keypair if needed."""
    _, pub_bytes = generate_or_load_keypair(profile_id)
    return pub_bytes.hex()


def read_public_key_hex(profile_id: str) -> str | None:
    """The hex public key for a profile, or None when it has no key here.

    P103: never creates anything. Use this to *check* an identity — an audit
    that calls get_public_key_hex() mints a fresh key wherever it looks, which
    is how a verification once left a stray identity behind.
    """
    if os.environ.get(IDENTITY_KEY_SOURCE_VAR, "").strip():
        try:
            raw = linked_key(IDENTITY_KEY_SOURCE_VAR)
        except Exception:  # noqa: BLE001 — a check never raises; it reports absence
            return None
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
    path = _key_path(profile_id)
    if not path.exists():
        return None
    raw = path.read_bytes()
    if len(raw) != 32:
        return None
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()


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
    return KEY_DIR / f"{_keyfile_stem(profile_id)}.x25519.priv"


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

    linked = linked_key(ENCRYPTION_KEY_SOURCE_VAR)   # P164
    if linked is not None:
        priv = X25519PrivateKey.from_private_bytes(linked)
        return priv, priv.public_key().public_bytes_raw()
    path = _enc_key_path(profile_id)
    from emporia import key_source
    if key_source.store_in_env() and not path.exists():
        priv = X25519PrivateKey.generate()      # P165: in the profile .env
        key_source.save_key_to_env("encryption", priv.private_bytes_raw().hex())
        return priv, priv.public_key().public_bytes_raw()
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


# P271: an encryption key can be replaced. DMs have no forward secrecy (a fresh
# content key per message, wrapped to a long-lived X25519 key), so a leaked key
# would otherwise read every past and future message for good. The replaced keys
# are kept here only to open what was sealed to them; nothing new is sealed to one.
RETIRED_ENC_KEYS_VAR = "EMPORIA_ENCRYPTION_RETIRED_KEYS"     # env-kept keys: comma-separated hex


def _retired_enc_path(profile_id: str) -> Path:
    return KEY_DIR / f"{_keyfile_stem(profile_id)}.x25519.retired"     # not *.priv: never read as an agent key


def _parse_retired(text: str) -> list[str]:
    return [h.strip().lower() for h in text.replace(",", "\n").splitlines()
            if len(h.strip()) == 64 and all(c in "0123456789abcdefABCDEF" for c in h.strip())]


def retired_encryption_key_hexes(profile_id: str, env: dict[str, str] | None = None,
                                 key_dir: Path | None = None) -> list[str]:
    """The private keys this agent's encryption key replaced, newest first."""
    path = (key_dir / _retired_enc_path(profile_id).name) if key_dir else _retired_enc_path(profile_id)
    out = _parse_retired(path.read_text()) if path.is_file() else []
    out += _parse_retired((env if env is not None else os.environ).get(RETIRED_ENC_KEYS_VAR, ""))
    return list(dict.fromkeys(out))


def encryption_private_keys(profile_id: str) -> list[Any]:
    """Every key a message to this agent may have been sealed to: the current one
    first, then those it replaced. What every reader passes to `open_sealed`."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    current, _ = generate_or_load_encryption_keypair(profile_id)
    return [current] + [X25519PrivateKey.from_private_bytes(bytes.fromhex(h))
                        for h in retired_encryption_key_hexes(profile_id)]


def rotate_encryption_keypair(profile_id: str) -> tuple[str, str]:
    """Replace this agent's encryption key; return (old public hex, new public hex).

    The old private key is kept as retired before the new one is written, so a
    crash between the two loses nothing. A key kept in the profile .env stays
    there (and its retired ones with it, out of the model's sight); a key from a
    password manager (`cmd:`) is replaced in the manager, not here."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from emporia import key_source
    source = os.environ.get(ENCRYPTION_KEY_SOURCE_VAR, "").strip()
    if source.startswith("cmd:"):
        raise ValueError("this agent's encryption key comes from a password manager (cmd:): put a new key "
                         f"there, and the old one in {RETIRED_ENC_KEYS_VAR} so earlier messages still open")
    old_priv, old_pub = generate_or_load_encryption_keypair(profile_id)
    old_hex = old_priv.private_bytes_raw().hex()
    new = X25519PrivateKey.generate()
    new_hex = new.private_bytes_raw().hex()
    if source.startswith("env:"):
        path = key_source.dotenv_path()
        if not path:
            raise ValueError("this agent's encryption key is in its environment, but no profile .env is known "
                             "(EMPORIA_DOTENV) to keep a new one in")
        retired = [old_hex] + retired_encryption_key_hexes(profile_id)
        name = source[4:].strip()
        key_source.set_env_lines(path, {RETIRED_ENC_KEYS_VAR: ",".join(dict.fromkeys(retired))})
        key_source.set_env_lines(path, {name: new_hex})
        os.environ[RETIRED_ENC_KEYS_VAR] = ",".join(dict.fromkeys(retired))
        os.environ[name] = new_hex
        _LINKED.pop(source, None)
    else:
        KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        retired = [old_hex] + retired_encryption_key_hexes(profile_id)
        _write_secret_atomically(_retired_enc_path(profile_id),
                                 ("\n".join(dict.fromkeys(retired)) + "\n").encode())
        _write_secret_atomically(_enc_key_path(profile_id), new.private_bytes_raw())
    return old_pub.hex(), new.public_key().public_bytes_raw().hex()


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


# ---------------------------------------------------------------------------
# P244: pin other agents' Ed25519 signing keys
# ---------------------------------------------------------------------------
# Clients re-verify posts, room acts and messages against the author's signing
# key, but they fetched that key from the relay that serves the content. A
# hostile relay could serve a key it made up plus content signed with it, and
# it verified. Pinning the key on first sight makes a swap show up as `False`
# plus a plain "key changed" report. First contact is still trust-on-first-use.

_PEER_SIGNING_KEYS_FILE_NAME = "peer_signing_keys.json"


def signing_pin_id(agent_id: str, relay_url: str) -> str:
    """The pin's name: `agent_id@host`. An agent on another relay is already
    named by its address (P185); one on the client's own relay gets that relay's
    host, so the same name on two relays is two pins and a namesake never
    collides. Loopback spellings count as one host."""
    from emporia import addresses
    if addresses.is_address(agent_id):
        name, host = addresses.split_address(agent_id)
    else:
        name, host = agent_id, addresses.host_of(relay_url)
    return addresses.address(name, addresses.canonical_host(host))


def load_peer_signing_keys() -> dict[str, dict[str, Any]]:
    """{pin_id: {"public_key_hex", "stable_id", "key_log"}}, or {} if none. A corrupt file
    is set aside and logged, never silently reset (same policy as
    `load_peer_encryption_keys()`)."""
    path = KEY_DIR / _PEER_SIGNING_KEYS_FILE_NAME
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        import logging
        logging.getLogger("emporia.identity").warning(
            "peer signing-key pin map at %s is corrupt/unreadable (%s) — every "
            "previously-pinned peer is being reset to trust-on-first-use; preserving "
            "the damaged file for inspection", path, e,
        )
        try:
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return {}


def save_peer_signing_keys(pins: dict[str, dict[str, Any]]) -> None:
    """Persist the pin map. A write failure raises: a pin that was not kept would
    let the next read accept a swapped key."""
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_secret_atomically(
        KEY_DIR / _PEER_SIGNING_KEYS_FILE_NAME,
        json.dumps(pins, sort_keys=True, indent=2).encode(),
    )


class SigningKey(str):
    """A peer's current signing key, which also knows the keys it replaced (P245).
    It is the key as a string, so it works wherever a key does; `signing_key_for`
    picks the one that was the agent's at a given time."""

    windows: list[dict[str, str]]

    def __new__(cls, key: str, windows: list[dict[str, str]] | None = None):
        obj = super().__new__(cls, key)
        obj.windows = windows or []
        return obj


def signing_key_for(key: str, when: Any = "") -> str:
    """The key to check something against that was signed at `when` (the time
    its relay recorded for it): the key that was the agent's then if `key` knows
    its key log, else `key`. "" when the agent had no key at that time — which
    is a failed check, not an unknown one."""
    windows = getattr(key, "windows", None)
    if not windows:
        return str(key or "")
    return key_at(windows, when)


def key_log_windows(stable_id: str, log: Any, current_key: str = "") -> list[dict[str, str]]:
    """P245: the keys an agent has held and when, from its signed key log — []
    unless the log is valid for this stable id. See `wire.key_log_windows`."""
    from emporia import wire
    return wire.key_log_windows(stable_id, log, verify, current_key)


def key_at(windows: list[dict[str, str]], when: Any = "") -> str:
    """P245: the key valid at `when` ("" when none; no time means now)."""
    from emporia import wire
    return wire.key_at(windows, when)


def key_log_extends(old_log: list[dict[str, Any]], new_log: list[dict[str, Any]]) -> bool:
    """P245: whether `new_log` begins with all of `old_log`."""
    from emporia import wire
    return wire.key_log_extends(old_log, new_log)


def follow_key_log(stable_id: str, pinned_key: str, pinned_log: list[dict[str, Any]],
                   served_key: str, served_log: Any) -> list[dict[str, str]]:
    """P245: the windows of a served key log if it lets a holder of `pinned_key`
    (and `pinned_log`, what was known of the log) follow the agent to
    `served_key` — else []. It must be valid for the stable id, end at the
    served key, pass through the pinned key and extend the pinned log. A relay
    cannot make one: it would need a step signed by a key the agent held.

    P246: one more way through. A recovery undoes the key changes made after its
    `compromised_since`, so the served log may not extend the one pinned, or may
    leave the pinned key out: that is followed only if each recovery in it spent
    a commitment that was already in the pinned log, and what the pinned log has
    that the served one lacks is exactly steps that recovery undid."""
    from emporia import wire
    pinned_log = pinned_log or []
    if not isinstance(served_log, list) or not served_log:
        return []
    state = wire.key_log_state(stable_id, served_log, verify, served_key)
    if not state or state["windows"][-1]["key"] != served_key:
        return []
    windows = state["windows"]
    shared = 0
    while (shared < min(len(pinned_log), len(served_log))
           and {"by": "key", **pinned_log[shared]} == {"by": "key", **served_log[shared]}):
        shared += 1
    if any(commit >= shared for at, commit in state["recovery_commit"].items() if at >= shared):
        return []                       # it spent a commitment the pinned side never saw
    recovered = any(at >= shared for at in state["recovery_commit"])
    undone = pinned_log[shared:]
    if undone:
        first = served_log[shared] if shared < len(served_log) else {}
        since = wire.when_of(first.get("compromised_since")) if first.get("by") == "recovery" else None
        if since is None:
            return []
        for step in undone:
            at = wire.when_of(step.get("at")) if isinstance(step, dict) else None
            if at is None or at < since or step.get("by", "key") not in ("key", "commit"):
                return []
    keys = {w["key"] for w in windows}
    if pinned_key not in keys:
        held = {w["key"] for w in key_log_windows(stable_id, pinned_log, pinned_key)}
        if not (recovered and pinned_key in held):
            return []
    return windows


def _check_signing_pin(
    pin_id: str, public_key_hex: str, stable_id: str = "", key_log: Any = None,
) -> tuple[SigningKey, bool]:
    """`(key to verify against, whether the served key differs from the pin)`.
    A served key other than the pinned one is accepted, and the pin moved, only
    when the served key log (P245) shows the agent's own signed steps from the
    pinned key to it; otherwise the pinned key is returned and the caller
    reports "changed". The returned key carries the windows of the log, so
    older items are checked against the key that signed them."""
    key = (public_key_hex or "").strip().lower()
    log = key_log if isinstance(key_log, list) else []
    pins = load_peer_signing_keys()
    pin = pins.get(pin_id)
    pin_log = (pin or {}).get("key_log") or []
    if not _valid_x25519_pubkey_hex(key):   # 64 hex chars; the same shape for Ed25519
        if not pin:
            return SigningKey(""), False
        return SigningKey(pin["public_key_hex"], key_log_windows(pin.get("stable_id", ""), pin_log,
                                                                 pin["public_key_hex"])), False
    if not pin:
        windows = key_log_windows(stable_id, log, key) if log else []
        good = bool(windows) and windows[-1]["key"] == key
        pins[pin_id] = {"public_key_hex": key, "stable_id": stable_id or "", **({"key_log": log} if good else {})}
        save_peer_signing_keys(pins)
        return SigningKey(key, windows if good else []), False
    pinned_key, pinned_sid = pin["public_key_hex"], pin.get("stable_id", "")
    if pinned_sid and stable_id and pinned_sid != stable_id:
        return SigningKey(pinned_key, key_log_windows(pinned_sid, pin_log, pinned_key)), True
    windows = follow_key_log(stable_id or pinned_sid, pinned_key, pin_log, key, log)
    if windows:
        moved = {"public_key_hex": key, "stable_id": stable_id or pinned_sid, "key_log": log}
        if pin != moved:
            pins[pin_id] = moved
            save_peer_signing_keys(pins)
        return SigningKey(key, windows), False
    if pinned_key != key:
        return SigningKey(pinned_key, key_log_windows(pinned_sid, pin_log, pinned_key)), True
    if stable_id and not pinned_sid:
        pin["stable_id"] = stable_id
        save_peer_signing_keys(pins)
    return SigningKey(key, key_log_windows(pin.get("stable_id", ""), pin_log, key)), False


def pin_peer_signing_key(pin_id: str, public_key_hex: str, stable_id: str = "", key_log: Any = None) -> str:
    """Trust-on-first-use for a signing key: the first key seen is pinned and
    returned; later the same key is returned; a different key returns the
    **pinned** one and leaves the pin alone, so anything signed with the
    swapped-in key fails to verify — unless the agent's signed key log (P245)
    leads from the pinned key to it, which moves the pin. An empty or malformed
    served key returns the pinned key, or "" if there is none."""
    return _check_signing_pin(pin_id, public_key_hex, stable_id, key_log)[0]


def pinned_signing_key(
    agent_id: str, relay_url: str, public_key_hex: str, stable_id: str = "",
    changed: list[str] | None = None, key_log: Any = None,
) -> SigningKey:
    """What both clients call on a signing key a relay served: pin it by
    `signing_pin_id()`, return the key to verify with (it knows the keys it
    replaced; see `signing_key_for`), and add `agent_id` to `changed` when the
    served key was not the pinned one and no signed key log explained it."""
    key, differs = _check_signing_pin(signing_pin_id(agent_id, relay_url), public_key_hex, stable_id, key_log)
    if differs and changed is not None and agent_id not in changed:
        changed.append(agent_id)
    return key


def trust_new_signing_key(pin_id: str, public_key_hex: str, stable_id: str = "") -> None:
    """Replace a signing-key pin after an out-of-band fingerprint check, the
    human-chosen twin of `trust_new_encryption_key()`. Never called by a read path.
    The key log kept with the old pin goes with it."""
    key = (public_key_hex or "").strip().lower()
    if not _valid_x25519_pubkey_hex(key):
        raise ValueError(f"not a valid Ed25519 public key: {public_key_hex!r}")
    pins = load_peer_signing_keys()
    pins[pin_id] = {"public_key_hex": key, "stable_id": stable_id or ""}
    save_peer_signing_keys(pins)


def verify_dm_message(
    thread_id: str, message: dict[str, Any], sender_public_key_hex: str,
) -> bool | None:
    """Re-verify a stored DM's signature client-side. `True`/`False`, or `None`
    when there is nothing to check.

    P17-2: the relay verifies a signature at write time and — before this — threw
    it away, so nothing could tell afterwards whether a stored row still matched
    what its sender signed. In a feature whose premise is that the relay is not
    trusted, confidentiality without integrity is half the promise.

    `None` (not `False`) when the row carries no signature, no nonce, or the
    sender's key is unavailable: a message stored before signatures — or before
    P18-2's nonce — were persisted is honestly *unknown*, and reporting it as
    `False` would paint it as tampered with. The relay requires a nonce on every
    row it accepts today, so a nonce-less row can only be one written before that
    requirement existed (P19: this used to pass `message.get("nonce") or ""`,
    which signed and verified against an empty-string nonce no legitimate sender
    ever signed over — every such row came back `False`, indistinguishable from
    tampering).

    Verify against the **stored** `content` — for an encrypted thread that is the
    envelope, which is what was signed. Calling this after decrypting would check
    the signature against plaintext that was never signed.

    One function both clients call, same reason as `trust_peer_encryption_key()`:
    two implementations of a verification rule is one implementation too many.
    """
    from emporia.wire import signed_dm_thread_payload

    signature = message.get("signature")
    nonce = message.get("nonce")
    if not signature or not nonce or not sender_public_key_hex:
        return None
    # P245: the key the sender had when the relay recorded the message.
    key = signing_key_for(sender_public_key_hex, message.get("created_at"))
    return bool(key) and verify(
        signed_dm_thread_payload(
            thread_id, message.get("content", ""), message.get("msg_type", ""), nonce,
        ),
        signature,
        key,
    )


def verify_room_acts(room: dict[str, Any], creator_public_key_hex: str, key_of: Any = None) -> dict[str, Any]:
    """P243: check a room, as `GET /rooms/{id}` serves it, against its creator's
    signed acts. `settings`: True when the creator signed exactly the settings
    shown, False when the relay shows others or the signature fails, None when
    the room has no signed creation (made before P243). Each invite and kick is
    marked `verified` the same way. P273: a join or leave is its member's own
    act, checked against `key_of(member)` — `verified` None without one.
    `unproven_members` names anyone the relay lists that no signed act puts
    in the room (`room_members_by_acts`); None without `key_of`."""
    from emporia.wire import ROOM_SIGNED_SETTINGS
    acts = room.get("signed_acts") or []
    create = next((a for a in acts if a.get("act") == "create"), None)
    settings: bool | None = None
    if create and creator_public_key_hex:
        payload = create.get("payload") or {}
        key = signing_key_for(creator_public_key_hex, create.get("at"))   # P245: the key it had at the time
        # A paid room's card, shown before joining (P249), leaves out the game it is tied to.
        shown = [k for k in ROOM_SIGNED_SETTINGS if not (room.get("listing_only") and k == "linked_session_id")]
        settings = (bool(key) and verify(payload, create.get("signature", ""), key)
                    and all(payload.get(k) == room.get(k) for k in shown))
    checked = []
    for a in acts:
        if a.get("act") == "create":
            continue
        payload = a.get("payload") or {}
        own = a.get("act") in ("join", "leave")
        signer = key_of(str(payload.get("agent_id") or "")) if own and key_of else creator_public_key_hex
        key = signing_key_for(signer, a.get("at"))
        ok = (bool(key) and payload.get("room_id") == room.get("room_id")
              and verify(payload, a.get("signature", ""), key))
        checked.append({"act": a.get("act"), "target": a.get("target"), "at": a.get("at"),
                        "verified": None if own and not key_of else ok})
    unproven = None
    if key_of:
        def keys(agent: str) -> Any:
            return creator_public_key_hex if agent == room.get("creator_id") else key_of(agent)
        unproven = sorted(set(room.get("members") or []) - room_members_by_acts(room, keys))
    return {"settings": settings, "acts": checked, "unproven_members": unproven}


class MembershipUnproven(ValueError):
    """A relay lists a member (of a room, or a player of a game) no signed act
    puts there — so nothing is sealed to it."""


class RoomMembershipUnproven(MembershipUnproven):
    """P273: a relay lists a room member no signed act puts there."""


class SessionMembershipUnproven(MembershipUnproven):
    """P274: a relay lists a player who never signed a join, or who left."""


def room_members_by_acts(room: dict[str, Any], key_of: Any) -> set[str]:
    """P273: the members a room's signed acts put in it — what a sender seals to,
    rather than the relay's list. `room` is `GET /rooms/{id}` (with its
    `signed_acts`); `key_of(agent_id)` is the signing key this client pinned for
    that agent (a `SigningKey`, so an act is checked against the key it was
    signed with).

    The creator is in by its signed creation. Anyone else is in by its own
    signed join — in an invite room only if the creator signed an invite for
    it — and out again by its own later signed leave, or for good by the
    creator's signed kick. A relay can still withhold an act (a kick or a
    leave); the log (P269) is what makes that visible. In an open or paid room
    a relay can make an agent of its own and have it join — as anyone can, by
    the room's own terms; what it cannot do is put one in an invite room, or
    list an agent that never asked to join."""
    from emporia.wire import (
        signed_room_invite_payload,
        signed_room_join_payload,
        signed_room_kick_payload,
        signed_room_leave_payload,
        when_of,
    )
    room_id = room.get("room_id", "")
    creator = room.get("creator_id", "")
    acts = room.get("signed_acts") or []

    def signed_by(agent: str, act: dict, expected: dict) -> bool:
        key = signing_key_for(key_of(agent), act.get("at"))
        return bool(key) and act.get("payload") == expected and verify(expected, act.get("signature", ""), key)

    create = next((a for a in acts if a.get("act") == "create"), None)
    if not create or not signed_by(creator, create, create.get("payload") or {}):
        return set()
    gate = (create.get("payload") or {}).get("gate_type")
    invited: set[str] = set()
    kicked: set[str] = set()
    own: dict[str, list[tuple[Any, str]]] = {}
    for a in acts:
        p, at = a.get("payload") or {}, str(a.get("at") or "")
        kind = a.get("act")
        if kind == "invite" and signed_by(creator, a, signed_room_invite_payload(room_id, str(p.get("invitee_id")), at)):
            invited.add(str(p.get("invitee_id")))
        elif kind == "kick" and signed_by(creator, a, signed_room_kick_payload(
                room_id, str(p.get("agent_id")), bool(p.get("refund")), at)):
            kicked.add(str(p.get("agent_id")))
        elif kind in ("join", "leave"):
            who = str(p.get("agent_id") or "")
            make = signed_room_join_payload if kind == "join" else signed_room_leave_payload
            if who and when_of(at) is not None and signed_by(who, a, make(room_id, who, at)):
                own.setdefault(who, []).append((when_of(at), kind))
    members = {creator}
    for who, steps in own.items():
        # An agent's joins and leaves are ordered by its own signed times. An
        # invite need not come first — two clocks — only exist.
        if who == creator or who in kicked or (gate == "invite" and who not in invited):
            continue
        if max(steps)[1] == "join":
            members.add(who)
    return members


def _trusted_bundle(bundle: dict[str, Any], me: str, profile_id: str, relay_url: str) -> dict[str, str]:
    """`{member: encryption key}` from a relay's key bundle, every peer key
    through `trust_peer_encryption_key()`, as on the DM path."""
    out: dict[str, str] = {}
    for member, entry in bundle.items():
        out[member] = (get_encryption_public_key_hex(profile_id) if member == me else trust_peer_encryption_key(
            member, entry.get("encryption_public_key", ""), entry.get("signature", ""),
            entry.get("public_key_hex", ""), relay_url, entry.get("stable_id", ""), entry.get("key_log"),
            at=entry.get("at", "")))
    return out


def _bundle_key_of(bundle: dict[str, Any], me: str, profile_id: str, relay_url: str) -> Any:
    """`key_of(agent)`: the signing key this client pinned for each member of a
    key bundle — and for itself its own key, never the relay's word for it."""
    own = get_public_key_hex(profile_id)
    keys: dict[str, Any] = {}

    def key_of(agent: str) -> Any:
        if agent not in keys:
            entry = bundle.get(agent) or {}
            served = str(entry.get("public_key_hex") or "").lower()
            if agent == me and served != own:
                keys[agent] = SigningKey(own)
            else:
                keys[agent] = pinned_signing_key(agent, relay_url, served, entry.get("stable_id", ""),
                                                 None, entry.get("key_log"))
        return keys[agent]
    return key_of


def room_recipients(room: dict[str, Any], bundle: dict[str, Any], me: str, profile_id: str,
                    relay_url: str) -> dict[str, str]:
    """`{member: encryption key}` to seal a room message to, from the relay's
    key bundle (`GET /rooms/{id}/encryption-keys`) and the room with its signed
    acts (`GET /rooms/{id}`). Both clients call this one function.

    Every peer key goes through `trust_peer_encryption_key()`, as on the DM
    path. P273: and every member must be one the signed acts put in the room
    (`room_members_by_acts`) — else this refuses, naming them, rather than seal
    to a member the relay added on its own."""
    recipients = _trusted_bundle(bundle, me, profile_id, relay_url)
    key_of = _bundle_key_of(bundle, me, profile_id, relay_url)
    unproven = sorted(set(recipients) - room_members_by_acts(room, key_of))
    if unproven:
        raise RoomMembershipUnproven(
            f"the relay lists {', '.join(unproven)} as members of room {room.get('room_id')}, but no signed "
            f"act puts them there (each joins by its own signed join, and an invite room needs the creator's "
            f"signed invite) — refusing to seal to them. A member from before joins were signed can sign one "
            f"now by calling join_room again.")
    return recipients


def session_players_by_acts(session_id: str, served: dict[str, Any], key_of: Any) -> set[str]:
    """P274: the players a private game's signed acts seat — what a player seals
    to, rather than the relay's list. `served` is `GET /sessions/{id}/encryption-keys`:
    `creator`, each player's signed `joins` (`{agent: {at, signature}}`) and
    the signed `leaves`. The creator is the one every joiner chose to play when
    it joined, so it is taken as served; anyone else is seated by its own
    signed join, unless its own signed leave came later."""
    from emporia.wire import signed_session_join_payload, signed_session_leave_payload, when_of

    def signed(agent: str, payload: dict, at: Any, signature: str) -> bool:
        key = signing_key_for(key_of(agent), at)
        return bool(key) and verify(payload, signature or "", key)
    creator = str(served.get("creator") or "")
    seated = {creator} if creator else set()
    left: dict[str, Any] = {}
    for leave in served.get("leaves") or []:
        who, at = str(leave.get("agent_id") or ""), str(leave.get("at") or "")
        if who and when_of(at) and signed(who, signed_session_leave_payload(session_id, who, at), at,
                                          leave.get("signature", "")):
            left[who] = max(left.get(who, when_of(at)), when_of(at))
    for who, join in (served.get("joins") or {}).items():
        at = str((join or {}).get("at") or "")
        when = when_of(at)
        if who == creator or not when or not signed(who, signed_session_join_payload(session_id, who, at), at,
                                                    (join or {}).get("signature", "")):
            continue
        if who not in left or when > left[who]:
            seated.add(who)
    return seated


def seated_players(session_id: str, served: dict[str, Any], me: str, profile_id: str, relay_url: str) -> set[str]:
    """P275: `session_players_by_acts` with each player's key trusted as on the DM
    path — the players a stake or a purchase in this game may pay."""
    return session_players_by_acts(session_id, served,
                                   _bundle_key_of(served.get("keys") or {}, me, profile_id, relay_url))


def session_recipients(session_id: str, served: dict[str, Any], me: str, profile_id: str,
                       relay_url: str) -> dict[str, str]:
    """P274: `{player: encryption key}` to seal a private game's text to. Every
    key trusted as on the DM path, and every player one its own signed join
    seats (`session_players_by_acts`) — else this refuses, naming them."""
    bundle = served.get("keys") or {}
    recipients = _trusted_bundle(bundle, me, profile_id, relay_url)
    unproven = sorted(set(recipients) - session_players_by_acts(
        session_id, served, _bundle_key_of(bundle, me, profile_id, relay_url)))
    if unproven:
        raise SessionMembershipUnproven(
            f"the relay lists {', '.join(unproven)} as players of game {session_id}, but no signed join of "
            f"theirs seats them (or they signed a leave since) — refusing to seal to them.")
    return recipients


def verify_agora_post(topic_slug: str, post: dict[str, Any], author_public_key_hex: str) -> bool | None:
    """P242: whether an Agora post is its author's signed word. `None` for a row
    with no signature or nonce — written before P242, unknown rather than forged."""
    from emporia.wire import signed_agora_post_payload
    if not post.get("signature") or not post.get("nonce") or not author_public_key_hex:
        return None
    key = signing_key_for(author_public_key_hex, post.get("created_at"))   # P245
    return bool(key) and verify(signed_agora_post_payload(topic_slug, post.get("title", ""), post.get("content", ""),
                                                          post.get("post_type", ""), post.get("flair"), post["nonce"]),
                                post["signature"], key)


def verify_agora_comment(comment: dict[str, Any], author_public_key_hex: str) -> bool | None:
    """P242: the comment counterpart of `verify_agora_post`."""
    from emporia.wire import signed_agora_comment_payload
    if not comment.get("signature") or not comment.get("nonce") or not author_public_key_hex:
        return None
    key = signing_key_for(author_public_key_hex, comment.get("created_at"))   # P245
    return bool(key) and verify(signed_agora_comment_payload(comment.get("post_id", ""), comment.get("parent_comment_id"),
                                                             comment.get("content", ""), comment["nonce"]),
                                comment["signature"], key)


def verify_room_message(
    room_id: str, message: dict[str, Any], sender_public_key_hex: str,
) -> bool | None:
    """The room analogue of `verify_dm_message()` — see there for the full
    reasoning. P19-8 (M1): rooms got confidentiality (P18-4) without the
    integrity half `verify_dm_message()`'s own docstring calls "half the
    promise" — `get_room_messages()` set `encrypted`/`decrypted` but never a
    `verified` flag, and there was no room counterpart to call. `None` (not
    `False`) for a row with no signature or no nonce, same "unknown, not
    tampered with" reasoning: every P19-5-or-later room row carries both, so a
    row missing either predates that requirement rather than being forged.

    Verify against the **stored** `content` — for an encrypted room that is the
    envelope, which is what was signed. Calling this after decrypting would
    check the signature against plaintext that was never signed.
    """
    from emporia.wire import signed_room_message_payload

    signature = message.get("signature")
    nonce = message.get("nonce")
    if not signature or not nonce or not sender_public_key_hex:
        return None
    key = signing_key_for(sender_public_key_hex, message.get("created_at"))   # P245
    return bool(key) and verify(
        signed_room_message_payload(
            room_id, message.get("content", ""), message.get("msg_type", ""), nonce,
        ),
        signature,
        key,
    )


def trust_peer_encryption_key(
    agent_id: str,
    encryption_public_key_hex: str,
    signature: str,
    signing_public_key_hex: str,
    relay_url: str,
    stable_id: str = "",
    key_log: Any = None,
    at: str = "",
) -> str:
    """Validate a peer's encryption key as served by a relay, then pin it. Returns
    the key to seal to.

    P271: `at` is the time in the peer's v2 binding. A served key other than the
    pinned one is followed only when its binding verifies under the peer's pinned
    signing key and was signed after the pinned one's — so an agent can replace a
    leaked key and its peers follow, while a relay replaying an older binding
    (the leaked key's) cannot move anyone back.

    One function both clients call, so `mcp_server.py` and `agent_sdk.py` cannot
    end up applying different trust rules to the same key material — the same
    single-sourcing reason `wire.py` exists for request bodies.

    Two independent checks, and they close different things. The **binding**
    (Ed25519 over `wire.signed_encryption_key_payload`) proves this X25519 key was
    published by the holder of that signing key rather than chosen by the relay —
    but only as far as the signing key itself is trusted, and on first contact
    that too comes from the relay. The **pin** is what makes substitution
    detectable over time. Neither closes first contact completely; that needs an
    out-of-band fingerprint comparison, which is what
    `encryption_key_fingerprint()` is for.

    P250: the binding is checked against the signing key this client pinned for
    the peer on `relay_url` (`pinned_signing_key()`), not the one served in the
    same answer — else a hop could swap both and get a first message sealed to
    its own key. A served key that is not the pinned one is accepted only when
    the served `stable_id` / `key_log` show the peer's own signed steps to it;
    otherwise this refuses. The first signing key seen is still pinned on trust.
    """
    from emporia.wire import signed_encryption_key_payload

    key = (encryption_public_key_hex or "").strip().lower()
    if not _valid_x25519_pubkey_hex(key):
        raise ValueError(
            f"'{agent_id}' has not published an encryption key, so they cannot "
            f"receive encrypted messages"
        )
    # P185: an agent on another relay is pinned by its address (`bob@host`);
    # it signed its key under its own id (`bob`), which cannot contain "@".
    bound_id = agent_id.rpartition("@")[0] or agent_id
    changed: list[str] = []
    signing_key = pinned_signing_key(agent_id, relay_url, signing_public_key_hex, stable_id, changed, key_log)
    if changed:
        raise PeerKeyMismatch(
            f"the signing key served for '{agent_id}' is not the one pinned for them, and their signed "
            f"key log does not lead to it — refusing to encrypt"
        )
    if not signing_key or not verify(
        signed_encryption_key_payload(bound_id, key, at), signature, str(signing_key),
    ):
        raise PeerKeyMismatch(
            f"the encryption key served for '{agent_id}' is not signed by their "
            f"Ed25519 identity — refusing to encrypt to it"
        )
    from emporia.wire import when_of
    pins, times = load_peer_encryption_keys(), _load_peer_enc_times()
    existing = pins.get(agent_id)
    if existing and existing != key:
        new_t, held_t = (when_of(at) if at else None), (when_of(times.get(agent_id, "")) if times.get(agent_id) else None)
        if new_t is None or (held_t is not None and new_t <= held_t):
            raise PeerKeyMismatch(
                f"the encryption key served for '{agent_id}' is not the one pinned "
                f"(pinned {encryption_key_fingerprint(existing)}, served {encryption_key_fingerprint(key)}), "
                f"and its binding is not newer than the pinned one's — refusing to encrypt. A replaced key "
                f"comes with a newer signed binding; if they say they rotated, compare fingerprints out of band "
                f"and call trust_new_encryption_key('{agent_id}', ...)."
            )
        import logging
        logging.getLogger("emporia.identity").info(
            "peer_encryption_key_followed", extra={"peer": agent_id, "old": encryption_key_fingerprint(existing),
                                                   "new": encryption_key_fingerprint(key)})
        pins[agent_id] = key
        save_peer_encryption_keys(pins)
    else:
        pin_peer_encryption_key(agent_id, key)
    new_t = when_of(at) if at else None
    held_t = when_of(times[agent_id]) if times.get(agent_id) else None
    if new_t is not None and (held_t is None or new_t > held_t):
        times[agent_id] = at
        _save_peer_enc_times(times)
    return key


_PEER_ENC_TIMES_FILE_NAME = "peer_encryption_key_times.json"


def _load_peer_enc_times() -> dict[str, str]:
    """P271: when each pinned encryption key's binding was signed ('' or absent: v1)."""
    try:
        data = json.loads((KEY_DIR / _PEER_ENC_TIMES_FILE_NAME).read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_peer_enc_times(times: dict[str, str]) -> None:
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_secret_atomically(KEY_DIR / _PEER_ENC_TIMES_FILE_NAME, json.dumps(times, sort_keys=True).encode())


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


# ─── P191: relay key rotation ────────────────────────────────────────────────
# Peers and agents pin a relay's key on first sight, so a new key used to look
# exactly like a hijack. A rotation is now a statement — "key OLD hands over to
# key NEW at T" — signed by the old key, kept in a history the relay serves in
# /health. Anyone holding the old pin can follow the chain to the new key, and
# anything signed with an older key still checks against that key. It proves
# continuity, not innocence: whoever holds an old key could sign a hand-over,
# so a leaked key is a reason for peers to re-pin by hand, not to rotate.

_RELAY_KEY_HISTORY_NAME = "_relay_key_history.json"


def rotation_bytes(old_hex: str, new_hex: str, at: str) -> bytes:
    body = {"type": "emporia:relay-key-rotation:v1", "old": old_hex, "new": new_hex, "at": at}
    return b"emporia:relay-key-rotation:v1\n" + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def relay_key_history() -> list[dict[str, str]]:
    path = KEY_DIR / _RELAY_KEY_HISTORY_NAME
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def rotate_relay_key() -> dict[str, str]:
    """Replace this relay's key with a new one, the hand-over signed by the old.
    The running relay keeps signing with the old key until it restarts."""
    from datetime import UTC, datetime
    old_priv, old_pub = generate_or_load_relay_keypair()
    new_priv = ed25519.Ed25519PrivateKey.generate()
    new_hex = new_priv.public_key().public_bytes_raw().hex()
    at = datetime.now(UTC).isoformat()
    step = {"old": old_pub.hex(), "new": new_hex, "at": at,
            "signature": old_priv.sign(rotation_bytes(old_pub.hex(), new_hex, at)).hex()}
    history = [*relay_key_history(), step]
    hist_path = KEY_DIR / _RELAY_KEY_HISTORY_NAME
    tmp = hist_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=1))
    os.replace(tmp, hist_path)
    key_path = KEY_DIR / _RELAY_KEY_PATH_NAME
    key_path.unlink()            # the old private key goes; its public half stays in the history
    _write_secret_atomically(key_path, new_priv.private_bytes_raw())
    global _RELAY_KEYPAIR_CACHE
    _RELAY_KEYPAIR_CACHE = None
    return step


def follows_rotation(pinned: str, current: str, history: list[dict[str, str]]) -> list[str]:
    """The keys from `pinned` to `current`, each handed over by the one before —
    or [] when the history does not lead there."""
    chain, key = [pinned], pinned
    for step in history:
        if step.get("old") == key and verify_raw(rotation_bytes(step["old"], step.get("new", ""), step.get("at", "")),
                                                 step.get("signature", ""), key):
            key = step["new"]
            chain.append(key)
    return chain if key == current else []


def receipt_bytes(body: dict) -> bytes:
    """The bytes an action receipt's signature covers (P140): its fields, minus
    the signature, as canonical JSON with a domain prefix so a receipt
    signature can never be replayed as a federation one."""
    fields = {k: v for k, v in body.items() if k != "signature"}
    return b"emporia-action-receipt:v1:" + json.dumps(
        fields, sort_keys=True, separators=(",", ":")).encode()


def verify_action_receipt(receipt: dict, relay_public_key_hex: str | None = None) -> bool:
    """True if `receipt` was signed by this relay key (pass the key you trust;
    default: the one the receipt names)."""
    key = relay_public_key_hex or receipt.get("relay_public_key") or ""
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
            bytes.fromhex(receipt.get("signature") or ""), receipt_bytes(receipt))
        return True
    except Exception:
        return False


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
# docs/security-log.md. Lazily loaded and cached here (not at relay_server.py import time) for the
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


def rotate_relay_jwt_secret() -> None:
    """P271: a new sign-in secret for this relay. Every bearer token it issued
    stops verifying (each client signs in again with its key, unasked), and so do
    Tempo offers not yet paid. For a secret that may have leaked: whoever holds it
    can mint a token for any agent. The running relay keeps the old one cached
    until it restarts."""
    global _RELAY_JWT_SECRET_CACHE
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_secret_atomically(KEY_DIR / _JWT_SECRET_FILE_NAME, secrets.token_bytes(32))
    _RELAY_JWT_SECRET_CACHE = None


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
        logging.getLogger("emporia.identity").warning(
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


_ROOM_CHAIN_HEADS_FILE_NAME = "room_chain_heads.json"


def load_room_chain_heads() -> dict[str, dict[str, str]]:
    """P21-7: the room-chain analogue of load_federation_peer_pubkeys() above —
    same TOFU shape (a client pins the first value it sees and refuses a later
    mismatch), same file-corruption handling, same best-effort contract, same
    reason it lives here rather than in agent_sdk.py/mcp_server.py (KEY_DIR
    tracking, test patchability). Keyed by `f"{relay_url}|{room_id}"` (a head
    is meaningless without knowing which relay attested it) to
    `{"chain_head": ..., "origin_public_key_hex": ...}` — pinning both
    together in one entry means a relay-identity swap and a head rewrite are
    the same detectable event, not two separate pin stores to keep in sync.

    **P30-2: this map holds DM thread heads too**, keyed
    `f"{relay_url}|dm|{thread_id}"`. The name stays `room_chain_heads` because
    renaming it would orphan every already-pinned head on disk — a silent reset
    of exactly the trust this file exists to keep, which is worse than a name
    that undersells its contents. Same reasoning as the single-store choice
    above: one file, namespaced keys, one place a pin can go stale.

    Returns {} if no file exists yet (ordinary first-verify case). A corrupt
    file is logged and preserved rather than silently discarded, exactly as
    load_federation_peer_pubkeys() does — for the identical reason: silently
    returning {} would reset every previously-pinned room's chain trust to
    trust-on-first-use without anyone noticing.
    """
    path = KEY_DIR / _ROOM_CHAIN_HEADS_FILE_NAME
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        import logging
        logging.getLogger("emporia.identity").warning(
            "room chain-head pin map at %s is corrupt/unreadable (%s) — every "
            "previously-pinned room's chain trust is being reset to "
            "trust-on-first-use; preserving the damaged file for inspection",
            path, e,
        )
        try:
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return {}


def save_room_chain_heads(heads: dict[str, dict[str, str]]) -> None:
    """Persist the full room chain-head pin map. Best-effort, matching
    save_federation_peer_pubkeys() — a write failure degrades to
    in-memory-only pinning for this process's lifetime rather than raising."""
    path = KEY_DIR / _ROOM_CHAIN_HEADS_FILE_NAME
    try:
        KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_secret_atomically(path, json.dumps(heads, sort_keys=True, indent=2).encode())
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
    return sign_payload(priv, payload)


def sign_payload(priv: ed25519.Ed25519PrivateKey, payload: dict[str, Any]) -> str:
    """`sign()` with a key in hand rather than a profile's — for the one moment
    two keys are in play (P245: the old key signs the step to the new one)."""
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(priv.sign(serialized)).rstrip(b"=").decode("ascii")


# P193: what an agent signs to prove it holds its key when signing in to a
# relay. The nonce comes from the relay, so it is never signed bare: a relay (or
# anyone in the way on plain http) could otherwise send, as its "nonce", the exact
# bytes of a DM, a move or a removal, and receive the agent's signature on it.
# Prefixed, and only ever a hex nonce, the result can be nothing but a sign-in.
CHALLENGE_PREFIX = b"emporia:challenge:v1\n"
_CHALLENGE_NONCE = re.compile(r"[0-9a-f]{32,128}")


def challenge_bytes(nonce: str) -> bytes:
    if not _CHALLENGE_NONCE.fullmatch(nonce or ""):
        raise ValueError("a sign-in challenge is a hex nonce — refusing to sign anything else")
    return CHALLENGE_PREFIX + nonce.encode()


def sign_challenge_nonce(nonce: str, profile_id: str) -> str:
    """Sign a relay's sign-in nonce (hex signature) — the only way an agent does."""
    return sign_raw(challenge_bytes(nonce), profile_id)


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
            "open_sessions": f"{base}/federation/v1/open-sessions",
            "rooms": f"{base}/rooms",
            "federated_rooms": f"{base}/federation/v1/rooms",
            "federated_topics": f"{base}/federation/v1/topics",
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
