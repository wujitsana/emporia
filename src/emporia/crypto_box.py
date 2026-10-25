"""End-to-end message encryption for Emporia — the layer the relay cannot see through.

Used by both encrypted DM threads (P16) and encrypted rooms (P18): one content key
wrapped to N recipients, so the same envelope serves a 2-party thread and an
N-member room without branching.

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

**AAD.** Every ciphertext is bound to `{v, kind, scope_id, sender_id, msg_type,
nonce}` via `message_aad()`. Without that binding a hostile relay could relocate a
ciphertext into a different thread, or relabel a "chat" as a negotiation "accept",
and it would still decrypt — turning opaque storage into a forgery primitive
requiring no key. With it, any such move fails authentication at the recipient.

`kind` (`"dm"` / `"room"`) is a P19 addition. Before it, a DM AAD and a room AAD
were distinguishable only because `dm_`-prefixed thread ids and `room_`-prefixed
room ids happen not to collide — a property of two `uuid` format strings in two
other modules, not of this one. An explicit domain tag makes cross-context
relocation structurally impossible rather than accidentally impossible.

Pure by design, same posture as `wire.py`: no I/O, no `httpx`, no `identity`
import, no relay import. Keys come in as arguments. That keeps every guarantee
above testable without HTTP, a DB, or a key file.

**What this does not do** (recorded in docs/security-log.md, not implied away): it does not
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

# P19: bumped from 1. P18-2 added `nonce` to the AAD without moving this, so every
# v1 envelope became undecryptable while still *claiming* to be a supported version
# — the failure surfaced as the generic "could not decrypt this message", which
# points at a key or a tamper, not at a format change. Backward compatibility is
# not owed here (hackathon-era testers restart on the current build), so the fix is
# to make the break loud rather than to carry two AAD shapes forever: a v1 envelope
# now fails the version check with a message that says exactly what happened.
ENVELOPE_VERSION = 2
ENVELOPE_ALG = "x25519-hkdf-sha256/aes-256-gcm"

# Distinct HKDF `info` strings for the two derivations, so a key derived for one
# purpose can never be valid for the other even if a future version reuses inputs.
_WRAP_INFO = b"emporia-dm-v1-wrap"

_NONCE_BYTES = 12   # AES-GCM standard nonce length
_CEK_BYTES = 32     # AES-256

# P28-2: membership is an input to every future message's size in an encrypted
# room, because `seal()` wraps the content key once per recipient. A wrapped
# entry measures ~111 bytes with short agent ids and grows with the id itself
# (the id is the map KEY), so ~2360 members exhausts `wire.MAX_CONTENT_BYTES`
# on an *empty* body: nobody can send anything, permanently, and on an open-gate
# room any registered agent can push it there simply by joining.
#
# 512 keeps the wrap set under a quarter of the content budget even with long
# ids, leaving the rest for actual messages. Enforced in `rooms.py`, which owns
# membership; the number lives here because this module creates the cost.
# `test_p28_the_cap_leaves_real_room_for_a_real_message` measures the real
# envelope in both directions, so this can't drift from the format it's derived
# from without failing.
MAX_ENCRYPTED_ROOM_MEMBERS = 512


class DecryptionError(Exception):
    """Raised for every failure to recover plaintext — a bad key, a tampered
    field, an AAD that doesn't match, an unknown version, a malformed envelope.

    Deliberately one exception type with a non-specific message: distinguishing
    "wrong key" from "tampered ciphertext" from "not a recipient" tells an
    attacker which of those they achieved, and none of the callers branch on it.
    """


class MissingRecipients(ValueError):
    """`validate_envelope_for()` found a participant with no wrapped key.

    A subclass of `ValueError` so every existing `except ValueError` around
    validation keeps working, but a *distinct* type because the relay has to tell
    this case apart from every other malformed envelope: a missing member is a
    recoverable membership race (409, retry), while a bad envelope is a client bug
    (400, terminal). P18-4 made that distinction by substring-matching this
    module's error text from `relay_server.py`, which meant rewording a sentence
    here would silently turn every room race into a terminal 400.
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "encrypted message is not readable by every thread participant — no "
            f"wrapped key for: {', '.join(missing)}"
        )


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(s: Any) -> bytes:
    if not isinstance(s, str):
        raise DecryptionError("malformed envelope")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise DecryptionError("malformed envelope") from e


def message_aad(kind: str, scope_id: str, sender_id: str, msg_type: str, nonce: str) -> bytes:
    """The additional authenticated data every ciphertext is bound to.

    Canonicalized the same way `identity.sign()` canonicalizes a signed payload
    (`sort_keys=True`), for the same reason: sender and recipient must produce
    byte-identical AAD from the same facts or decryption fails with an error that
    points nowhere near the cause. `separators` is pinned too — the default
    `json.dumps` separators include a space after ": ", and a future formatting
    change would silently break every existing message.

    `v` is included so a version bump changes the AAD, making cross-version
    confusion a decryption failure rather than a subtle mismatch.

    `kind` is `"dm"` or `"room"` and `scope_id` is the thread id or room id it
    belongs to. Both are required positionally, with no default between them: P19
    added `kind`, and a defaulted domain tag is worth nothing — the whole point is
    that a caller cannot omit it and silently produce the other context's AAD.

    `nonce` (P18-2) binds the ciphertext to *this* message. Without it the AAD is
    identical across every message one sender posts to one scope, so a relay could
    lift a ciphertext onto another row and it would decrypt cleanly. Also required
    with no default, for the same reason and because P18-2's `""` default is what
    let the room path ship without one at all.
    """
    return json.dumps(
        {
            "v": ENVELOPE_VERSION,
            "kind": kind,
            "scope_id": scope_id,
            "sender_id": sender_id,
            "msg_type": msg_type,
            "nonce": nonce,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def session_aad(session_id: str, field: str) -> bytes:
    """P149: what a sealed field of a private session is bound to — its session
    and its field ("deliverable", "chat", …). Who wrote it is already bound by
    the action's signature; this keeps a relay from moving ciphertext between
    sessions or fields."""
    return message_aad("session", session_id, "-", field, "-")


def _wrap_key_for(
    eph_priv: X25519PrivateKey, eph_pub: bytes, agent_id: str, recipient_pub_hex: str,
    cek: bytes, aad: bytes,
) -> dict[str, str]:
    try:
        recipient_pub = X25519PublicKey.from_public_bytes(bytes.fromhex(recipient_pub_hex))
    except ValueError as e:
        raise ValueError(
            f"not a valid X25519 public key for '{agent_id}': {recipient_pub_hex!r}"
        ) from e
    try:
        shared = eph_priv.exchange(recipient_pub)
    except ValueError as e:
        # P19: the write-side half of P17-5. `exchange()` raises ValueError("Error
        # computing shared key.") for a low-order or all-zeros peer point, and
        # `from_public_bytes()` above does *not* reject one — any 32 bytes are a
        # well-formed X25519 public key. P17-5 guarded `open_sealed()`; this path
        # was never audited, and P18-3 made it far worse than a 2-party problem:
        # `seal()` wraps to every room member, so one member publishing "00"*32
        # took down sending for the entire room, for everyone, permanently.
        #
        # Named rather than opaque. Unlike a decryption failure — where telling the
        # attacker which mode they hit is itself a leak — this one is a peer's
        # published key being unusable, and the sender can act on it (kick, or ask
        # them to re-publish) only if they know who.
        raise ValueError(
            f"'{agent_id}' published an X25519 public key that cannot be used for key "
            f"agreement (low-order or all-zeros point): {recipient_pub_hex!r}"
        ) from e
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
            agent_id: _wrap_key_for(eph_priv, eph_pub, agent_id, pub_hex, cek, aad)
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
    except (json.JSONDecodeError, TypeError, RecursionError) as e:
        # P19: RecursionError is not a subclass of ValueError, and `json.loads` raises
        # it — not JSONDecodeError — on deeply nested input (`"[" * 100000` is enough,
        # and that is ~100 KB, comfortably inside both of P18-1's size limits). Every
        # caller here catches ValueError or nothing, so it escaped as an unhandled 500
        # on both message routes. P18-4 widened the blast radius by routing *plaintext*
        # room messages through `looks_like_envelope()` too, so a route that had never
        # touched this module became crashable by the same body.
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
        raise MissingRecipients(missing)


def looks_like_envelope(content: str) -> bool:
    """True if `content` is a well-formed envelope of the *current* protocol
    version. Used in the write direction from `validate_envelope_for()`: an
    envelope posted to a *plaintext* thread is a client bug worth a clear 400,
    not an opaque blob to store as if it were prose. Also used to reject an
    envelope of the wrong version at write time — a fresh post is a client bug,
    not something to accept silently.

    Deliberately version-strict: `looks_like_any_envelope()` below is the
    version-tolerant sibling for the read path, where a stale-version envelope
    is old ciphertext, not a bug to reject.
    """
    try:
        parse_envelope(content)
    except ValueError:
        return False
    return True


def looks_like_any_envelope(content: str) -> bool:
    """True if `content` has the general shape of a crypto_box envelope,
    regardless of protocol version.

    P20-3: readers used `looks_like_envelope()` (version-strict) to decide
    `encrypted`, so a stored envelope from a since-retired protocol version
    failed that check and fell straight through as `(encrypted=False,
    decrypted=False)` — the documented *plaintext* state (P17-3) — instead of
    `(True, False)`, ciphertext that failed to open. Reading is not the place
    to reject an old version outright: `open_sealed()` already raises
    `DecryptionError("unsupported envelope version or algorithm")` for exactly
    this shape, which every reader already catches and folds into the
    (True, False) state. This function is the read-path gate that lets a
    stale-version envelope reach that existing, already-tested path instead of
    being misreported as plaintext.
    """
    try:
        env = json.loads(content)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return False
    if not isinstance(env, dict):
        return False
    if not isinstance(env.get("v"), int) or not isinstance(env.get("alg"), str):
        return False
    for field in ("epk", "n", "ct"):
        if not isinstance(env.get(field), str) or not env[field]:
            return False
    return isinstance(env.get("keys"), dict) and bool(env["keys"])
