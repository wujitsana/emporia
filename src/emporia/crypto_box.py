"""End-to-end message encryption for Emporia — the layer the relay cannot see through.

Emporia's `encrypted` rooms flag has, until now, meant only a relay-side posture:
the relay agrees to store an opaque blob and skip the guardrails scan
(`relay_server.py`'s room route), while `mcp_server.py` told callers to "encrypt
content before calling this tool" and provided nothing to do it with. This module
is that missing half — the actual crypto — written for encrypted DM threads and
deliberately shaped so rooms can reuse it later (wrap to N members instead of 2).

**The guarantee.** The relay process, its operator, and anyone holding the SQLite
file see only what `seal()` produces. No key the relay holds recovers the
plaintext, because the relay holds no key at all: X25519 private keys live beside
Ed25519 ones under `identity.KEY_DIR`, 0600, and are never transmitted.

**Shape.** A fresh random 32-byte content key (CEK) encrypts the message once
under AES-256-GCM; that CEK is then wrapped *once per participant* via
ephemeral-static X25519 ECDH → HKDF-SHA256 → AES-256-GCM. Wrapping to the sender
as well as the recipient is not redundancy — it is what lets the sender read their
own thread back. A plain sealed-box would make `GET /dm/{id}/messages` return the
caller's own history as garbage, which is not a shippable DM product.

**AAD.** Every ciphertext is bound to `{v, thread_id, sender_id, msg_type}` via
`message_aad()`. Without that binding a hostile relay could relocate a ciphertext
into a different thread, or relabel a "chat" as a negotiation "accept", and it
would still decrypt — turning opaque storage into a forgery primitive requiring no
key. With it, any such move fails authentication at the recipient.

Pure by design, same posture as `wire.py`: no I/O, no `httpx`, no `identity`
import, no relay import. Keys come in as arguments. That keeps every guarantee
above testable without HTTP, a DB, or a key file.

**What this does not do** (recorded in SECURITY.md, not implied away): it does not
hide metadata (who/when/how often/size/`msg_type` stay visible to the relay), and
it has no forward secrecy against a stolen long-term key — the CEK is fresh per
message, but a stolen X25519 private key retroactively unwraps everything wrapped
to it. Ratcheting is a much larger design and is out of scope.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENVELOPE_VERSION = 1
ENVELOPE_ALG = "x25519-hkdf-sha256/aes-256-gcm"

# Distinct HKDF `info` strings for the two derivations, so a key derived for one
# purpose can never be valid for the other even if a future version reuses inputs.
_WRAP_INFO = b"emporia-dm-v1-wrap"

_NONCE_BYTES = 12   # AES-GCM standard nonce length
_CEK_BYTES = 32     # AES-256


class DecryptionError(Exception):
    """Raised for every failure to recover plaintext — a bad key, a tampered
    field, an AAD that doesn't match, an unknown version, a malformed envelope.

    Deliberately one exception type with a non-specific message: distinguishing
    "wrong key" from "tampered ciphertext" from "not a recipient" tells an
    attacker which of those they achieved, and none of the callers branch on it.
    """


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: Any) -> bytes:
    if not isinstance(s, str):
        raise DecryptionError("malformed envelope")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise DecryptionError("malformed envelope") from e


def message_aad(thread_id: str, sender_id: str, msg_type: str) -> bytes:
    """The additional authenticated data every DM ciphertext is bound to.

    Canonicalized the same way `identity.sign()` canonicalizes a signed payload
    (`sort_keys=True`), for the same reason: sender and recipient must produce
    byte-identical AAD from the same facts or decryption fails with an error that
    points nowhere near the cause. `separators` is pinned too — the default
    `json.dumps` separators include a space after ": ", and a future formatting
    change would silently break every existing message.

    `v` is included so a version bump changes the AAD, making cross-version
    confusion a decryption failure rather than a subtle mismatch.
    """
    return json.dumps(
        {
            "v": ENVELOPE_VERSION,
            "thread_id": thread_id,
            "sender_id": sender_id,
            "msg_type": msg_type,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _wrap_key_for(
    eph_priv: X25519PrivateKey, eph_pub: bytes, recipient_pub_hex: str, cek: bytes, aad: bytes,
) -> dict[str, str]:
    try:
        recipient_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(recipient_pub_hex))
    except ValueError as e:
        raise ValueError(f"not a valid X25519 public key: {recipient_pub_hex!r}") from e
    shared = eph_priv.exchange(recipient_pub)
    # Salt binds the derived key to *this* ephemeral key and *this* recipient, so
    # the same ECDH output can never produce the same wrapping key for two
    # different (epk, recipient) pairs.
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=eph_pub + recipient_pub.public_bytes_raw(),
        info=_WRAP_INFO,
    ).derive(shared)
    nonce = os.urandom(_NONCE_BYTES)
    return {"n": _b64e(nonce), "ct": _b64e(AESGCM(wrap_key).encrypt(nonce, cek, aad))}


def seal(plaintext: str, recipients: dict[str, str], aad: bytes) -> dict[str, Any]:
    """Encrypt `plaintext` so that exactly the agents in `recipients`
    (`{agent_id: x25519_public_key_hex}`) can recover it.

    Include the *sender* in `recipients` — omitting them produces a message they
    can never read back. The relay enforces this for DM threads by requiring a
    wrapped key for both participants (`validate_envelope_for()`).
    """
    if not recipients:
        raise ValueError("seal() requires at least one recipient")

    eph_priv = X25519PrivateKey.generate()
    eph_pub = eph_priv.public_key().public_bytes_raw()
    cek = os.urandom(_CEK_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    return {
        "v": ENVELOPE_VERSION,
        "alg": ENVELOPE_ALG,
        "epk": eph_pub.hex(),
        "n": _b64e(nonce),
        "ct": _b64e(AESGCM(cek).encrypt(nonce, plaintext.encode("utf-8"), aad)),
        # sorted() only so the serialized envelope is deterministic given its
        # inputs — it is not a security property, just easier to diff and test.
        "keys": {
            agent_id: _wrap_key_for(eph_priv, eph_pub, pub_hex, cek, aad)
            for agent_id, pub_hex in sorted(recipients.items())
        },
    }


def open_sealed(
    envelope: dict[str, Any], agent_id: str, private_key: X25519PrivateKey, aad: bytes,
) -> str:
    """Recover the plaintext `agent_id` was sealed to, or raise `DecryptionError`.

    Takes the private key as an object rather than loading it, so this module
    stays free of key-file I/O (`identity.generate_or_load_encryption_keypair()`
    is the loader).
    """
    if not isinstance(envelope, dict):
        raise DecryptionError("malformed envelope")
    if envelope.get("v") != ENVELOPE_VERSION or envelope.get("alg") != ENVELOPE_ALG:
        raise DecryptionError("unsupported envelope version or algorithm")

    keys = envelope.get("keys")
    if not isinstance(keys, dict):
        raise DecryptionError("malformed envelope")
    wrapped = keys.get(agent_id)
    if not isinstance(wrapped, dict):
        raise DecryptionError("not a recipient of this message")

    epk_hex = envelope.get("epk")
    if not isinstance(epk_hex, str):
        raise DecryptionError("malformed envelope")
    try:
        eph_pub_bytes = bytes.fromhex(epk_hex)
        eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
    except ValueError as e:
        raise DecryptionError("malformed envelope") from e

    try:
        shared = private_key.exchange(eph_pub)
    except ValueError as e:
        # P17-5: X25519 exchange() raises ValueError("Error computing shared key.")
        # for a low-order/all-zeros peer point — a *different* exception from the
        # InvalidTag below, and `epk` is attacker-controllable. Unguarded, it
        # escaped this function entirely and took the caller's whole thread read
        # with it, breaking the "one bad message must not make a thread
        # unreadable" contract both clients rely on. Found by a live tamper drill,
        # not by the suite: flipping a byte of a valid epk almost always yields
        # another valid point and fails as InvalidTag, so only a *structured* bad
        # value reaches this path.
        raise DecryptionError("could not decrypt this message") from e
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=eph_pub_bytes + private_key.public_key().public_bytes_raw(),
        info=_WRAP_INFO,
    ).derive(shared)
    try:
        cek = AESGCM(wrap_key).decrypt(_b64d(wrapped.get("n")), _b64d(wrapped.get("ct")), aad)
        plaintext = AESGCM(cek).decrypt(_b64d(envelope.get("n")), _b64d(envelope.get("ct")), aad)
    except InvalidTag as e:
        raise DecryptionError("could not decrypt this message") from e
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DecryptionError("could not decrypt this message") from e


# ============================================================================
# Relay-side helpers — shape validation only. No key, no plaintext, no decryption.
# ============================================================================

def parse_envelope(content: str) -> dict[str, Any]:
    """Parse a stored/posted `content` string as a v1 envelope, or raise `ValueError`."""
    try:
        env = json.loads(content)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("content is not an encrypted envelope") from e
    if not isinstance(env, dict):
        raise ValueError("content is not an encrypted envelope")
    if env.get("v") != ENVELOPE_VERSION or env.get("alg") != ENVELOPE_ALG:
        raise ValueError(
            f"unsupported envelope version/algorithm (expected v{ENVELOPE_VERSION} "
            f"{ENVELOPE_ALG})"
        )
    for field in ("epk", "n", "ct"):
        if not isinstance(env.get(field), str) or not env[field]:
            raise ValueError(f"envelope is missing required field '{field}'")
    if not isinstance(env.get("keys"), dict) or not env["keys"]:
        raise ValueError("envelope is missing wrapped recipient keys")
    return env


def validate_envelope_for(content: str, required_recipients: set[str]) -> None:
    """What the relay runs before storing a message on an encrypted thread.

    Two failures this catches, both of which are silent otherwise: plaintext
    posted to an encrypted thread (the worst failure mode this feature has), and
    a sender who wrapped only for themselves — a message the peer can never read,
    which without this check would be stored, acknowledged with a 200, and
    discovered only as permanent garbage in the peer's history.

    Reads no plaintext and needs no key: it inspects the envelope's structure and
    the *set of agent_ids* the CEK was wrapped to, nothing more.
    """
    env = parse_envelope(content)
    missing = sorted(required_recipients - set(env["keys"]))
    if missing:
        raise ValueError(
            "encrypted message is not readable by every thread participant — no "
            f"wrapped key for: {', '.join(missing)}"
        )


def looks_like_envelope(content: str) -> bool:
    """True if `content` is a well-formed v1 envelope. Used in the other
    direction from `validate_envelope_for()`: an envelope posted to a *plaintext*
    thread is a client bug worth a clear 400, not an opaque blob to store as if
    it were prose."""
    try:
        parse_envelope(content)
    except ValueError:
        return False
    return True
