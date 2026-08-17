"""Shared wire-contract functions for Emporia's two client implementations.

`mcp_server.py` (52 sync tool functions, module-level `httpx.get`/`.post`) and
`agent_sdk.py` (40 async `EmporiaAgent` methods, `httpx.AsyncClient`) each wrap
the same relay REST endpoints with separate HTTP client code. Every duplication
bug this codebase has found in that overlap (N2's bearer/retry semantics, N3-8's
signed-envelope shape needing the identical fix in three files) was in *request
construction* — the dict shape sent over the wire — not transport. Both files
already build that shape purely and synchronously; this module is the one
definition of it, so the two clients can't drift on what a request looks like
even though they stay on separate (sync vs. async) transports.

Deliberately excludes signing (`identity.sign`/`identity.sign_raw`, already
shared) and the actual HTTP call (transport differs by design — see ROADMAP.md's
"transport dedup" note, explicitly out of scope for this module). Every function
here is pure: no I/O, no `httpx`, no `identity` import.
"""

from __future__ import annotations

from typing import Any


def dashboard_session_body(agent_id: str, challenge_id: str, signature_hex: str) -> dict[str, Any]:
    """Body for `POST /dashboard/session` — the second half of the
    challenge/sign/session handshake both clients use to mint a bearer JWT
    for their own outbound calls (mcp_server.py's
    `_dashboard_bearer_headers()`, agent_sdk.py's `_bearer_token_value()`)."""
    return {"agent_id": agent_id, "challenge_id": challenge_id, "signature_hex": signature_hex}


def registration_body(
    agent_id: str,
    public_key_hex: str,
    display_name: str,
    challenge_id: str,
    challenge_signature: str,
    *,
    nous_jwt: str = "",
    encryption_public_key_hex: str = "",
    encryption_key_signature: str = "",
) -> dict[str, Any]:
    """Body for `POST /agents/register` — the second half of the
    challenge/sign/register handshake both clients use
    (mcp_server.py's `register_agent()`, agent_sdk.py's `register()`).

    P16: the X25519 encryption key can ride along here so a fresh agent publishes
    both keys in one round trip. Both encryption fields are omitted entirely when
    empty rather than sent as `""` — an agent who genuinely opted out and an agent
    whose client forgot the second call are indistinguishable to peers otherwise,
    and this keeps the body identical to the pre-P16 shape for callers that don't
    encrypt."""
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "public_key_hex": public_key_hex,
        "display_name": display_name,
        "challenge_id": challenge_id,
        "challenge_signature": challenge_signature,
    }
    if nous_jwt:
        body["identity_claims"] = [{"provider": "nous", "token": nous_jwt}]
    if encryption_public_key_hex:
        body["encryption_public_key_hex"] = encryption_public_key_hex
        body["encryption_key_signature"] = encryption_key_signature
    return body


def signed_encryption_key_payload(agent_id: str, encryption_public_key_hex: str) -> dict[str, Any]:
    """The dict an agent signs (Ed25519) to bind their X25519 encryption key to
    their identity — the fifth signed contract, and the second this module
    single-sources on the *verifier* side as well as both clients
    (`relay_server.py`'s `set_encryption_key()` reconstructs this same dict).

    The binding exists because the relay is the one serving encryption keys to
    peers. Without it, "the key the relay handed me" and "the key this agent
    published" are the same claim; with it, a peer can check the second against
    an Ed25519 identity independently of what the relay says. `agent_id` is inside
    the signature so a published key can't be lifted and re-published under
    someone else's id."""
    return {"agent_id": agent_id, "encryption_public_key_hex": encryption_public_key_hex}


def encryption_key_body(encryption_public_key_hex: str, signature: str) -> dict[str, Any]:
    """The actual `POST /agents/{id}/encryption-key` body. `agent_id` is in the
    URL (and in what was signed), so it isn't repeated here."""
    return {
        "encryption_public_key_hex": encryption_public_key_hex,
        "signature": signature,
    }


# P18: the largest a single message's `content` may be, in **bytes** — a character
# bound is meaningless against multi-byte input (a 4-bytes-per-char string sails
# through a len()-based check at four times the intended size). Lives here rather
# than in relay_server.py so both clients can check before sending rather than
# discovering the limit as a 400 after a round trip.
#
# Recorded as an open gap since P15 and deferred twice as "belongs at the app/proxy
# layer". True of the general case, but this relay runs with nothing in front of
# it, so nothing enforced it at all: a multi-MB message was accepted, stored,
# scanned and broadcast. `relay_server.MAX_REQUEST_BYTES` is the general ceiling;
# this is the per-field one, and it exists because an opaque 413 names nothing.
#
# Consequence worth knowing: on an encrypted thread this budget is spent on
# *ciphertext*, so the usable plaintext allowance is smaller — and smaller again
# for each additional room member, since the wrapped-key set grows with the member
# count.
MAX_CONTENT_BYTES = 256 * 1024

DEFAULT_CONFIRM_RATIONALE = "Buyer confirmed delivery."

# P12: room-message default msg_type — same hand-copied-across-signer-and-verifier
# shape as DEFAULT_CONFIRM_RATIONALE above (relay_server.py's SendRoomMessageRequest
# resolves `req.msg_type or DEFAULT_MSG_TYPE` once, same pattern as the rationale
# constants, and reuses that one resolved value for both signature verification and
# persistence — msg_type is also stored/routed on, not signature-only, so nothing here
# can land as an empty string the way a bare field default could).
DEFAULT_MSG_TYPE = "chat"

# P15: the msg_type values a *client* may post. `rooms.MsgType` (rooms.py) is the full
# declared domain and includes "system", which is server-owned — the relay posts it
# itself for negotiation-broker output, under sender_id="emporia:broker". Clients get
# everything except that. Lives here rather than in rooms.py because both the room route
# and the thread-DM route validate against it and only this module is shared by all of
# them; a test pins it against rooms.MsgType so the two can't drift.
CLIENT_MSG_TYPES = frozenset({"chat", "collab", "code", "counter_offer", "accept", "reject"})


def default_dispute_rationale(reason: str) -> str:
    """The default `peer_text_rationale` for a dispute when the caller omits
    one explicitly. Signer and verifier must resolve to the identical string —
    it's part of what gets signed (`signed_action_payload()`'s `rationale`
    arg), so a divergence between the two sides breaks signature verification
    with a 403 that points nowhere near the actual cause."""
    return f"Dispute: {reason}"


def signed_action_payload(
    agent_id: str,
    action_type: str,
    payload: dict[str, Any],
    rationale: str,
    session_id: str,
    step_number: int,
) -> dict[str, Any]:
    """The dict `POST /sessions/{id}/action` callers sign before sending —
    binds the signature to `session_id`/`step_number` so it can't be
    replayed against a different session or turn (N3-8). Key order here
    doesn't need to match `action_envelope()`'s: `identity.sign()`
    canonicalizes via `json.dumps(..., sort_keys=True)`."""
    return {
        "agent_id": agent_id,
        "action_type": action_type,
        "payload": payload,
        "peer_text_rationale": rationale,
        "session_id": session_id,
        "step_number": step_number,
    }


def action_envelope(
    agent_id: str,
    action_type: str,
    payload: dict[str, Any],
    rationale: str,
    signature: str,
) -> dict[str, Any]:
    """The actual `POST /sessions/{id}/action` body — `signed_action_payload()`'s
    fields minus `session_id`/`step_number` (both already in the URL/relay
    state), plus the signature computed over that payload."""
    return {
        "agent_id": agent_id,
        "action_type": action_type,
        "payload": payload,
        "peer_text_rationale": rationale,
        "signature": signature,
    }


def signed_room_message_payload(room_id: str, content: str, msg_type: str) -> dict[str, Any]:
    """The dict `POST /rooms/{id}/message` callers sign before sending —
    binds the signature to `room_id`/`msg_type`, not just `content` (N3-8/R7:
    a content-only signature was valid in any room this agent belongs to,
    forever)."""
    return {"room_id": room_id, "content": content, "msg_type": msg_type}


def room_message_envelope(
    content: str,
    msg_type: str,
    signature: str,
    *,
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    """The actual `POST /rooms/{id}/message` body. `parent_message_id` is
    accepted here even though agent_sdk.py's `send_room_message()` doesn't
    have that parameter yet (only mcp_server.py's does) — so adding it to
    the SDK later doesn't require touching this function."""
    body: dict[str, Any] = {"content": content, "msg_type": msg_type, "signature": signature}
    if parent_message_id:
        body["parent_message_id"] = parent_message_id
    return body


def new_message_nonce() -> str:
    """A fresh per-message nonce, chosen by the *sender*.

    P18-2: before this, neither the signature (`{thread_id, content, msg_type}`)
    nor the AAD (`{v, thread_id, sender_id, msg_type}`) bound anything unique to
    the message — so a relay could copy one row's content *and its signature* onto
    another row and have both verify and decrypt. P17-2 recorded that honestly
    rather than claiming it closed; this is the close.

    Sender-chosen rather than server-assigned because it has to be inside what the
    sender signs, and a server-assigned `message_id` doesn't exist yet at signing
    time. 128 bits from `secrets`, so two honest senders never collide and a
    hostile one can only squat ids in their own thread.
    """
    import secrets

    return secrets.token_hex(16)


def signed_dm_thread_payload(
    thread_id: str, content: str, msg_type: str, nonce: str,
) -> dict[str, Any]:
    """The dict `POST /dm/{thread_id}/send` callers sign before sending — binds the
    signature to `thread_id`/`msg_type`, not just `content` (same N3-8/R7 shape as
    `signed_room_message_payload()`: a content-only signature would be valid in any
    thread this agent is a participant of, forever). Distinct from
    `signed_dm_envelope()` above, which is `POST /messages`'s shape
    (`to_agent`/`session_id`/`payload`) — thread DMs route on `thread_id`, a
    structurally different key, not `to_agent`.

    P18-2: `nonce` binds the signature to *this* message, closing the replay P17-2
    recorded — without it a relay could lift a valid (content, signature) pair onto
    a second row and both would verify."""
    return {
        "thread_id": thread_id, "content": content, "msg_type": msg_type, "nonce": nonce,
    }


def dm_thread_message_envelope(
    content: str, msg_type: str, signature: str, *, nonce: str = "",
) -> dict[str, Any]:
    """The actual `POST /dm/{thread_id}/send` body — `signed_dm_thread_payload()`'s
    fields minus `thread_id` (already in the URL), plus the signature computed over
    that payload.

    Deliberately separate from `room_message_envelope()` despite the two shapes being
    identical today: they're different routes whose bodies are free to diverge, and
    `room_message_envelope()` already carries a `parent_message_id` kwarg this route
    doesn't accept. Sharing one function would mean the next field added for rooms
    silently changes the DM wire body.

    P18-2: `nonce` is keyword-only and defaults to `""` purely so the three
    positional arguments keep their meaning at every existing call site. The relay
    *rejects* an empty nonce rather than falling back to an unbound message — a
    silent fallback would leave the replay hole open to any client that simply
    omitted the field, which is the failure mode a default invites."""
    return {
        "content": content, "msg_type": msg_type, "signature": signature, "nonce": nonce,
    }


def confirm_delivery_body(agent_id: str, rationale: str, signature: str) -> dict[str, Any]:
    """The actual `POST /sessions/{id}/confirm-delivery` body. Signer-side only —
    the relay parses this into `ConfirmDeliveryRequest` rather than building it, so
    unlike `signed_action_payload()` there's no verifier call site to single-source."""
    return {"agent_id": agent_id, "rationale": rationale, "signature": signature}


def dispute_delivery_body(
    agent_id: str, reason: str, rationale: str, signature: str,
) -> dict[str, Any]:
    """The actual `POST /sessions/{id}/dispute-delivery` body — `confirm_delivery_body()`
    plus the `reason` the dispute is recorded under. Signer-side only, same as above."""
    return {
        "agent_id": agent_id, "reason": reason, "rationale": rationale, "signature": signature,
    }


def signed_dm_envelope(
    to_agent: str, msg_type: str, session_id: str | None, payload: dict[str, Any],
) -> dict[str, Any]:
    """The dict `POST /messages` callers sign (a direct agent-to-agent message,
    not a room post) — binds `to_agent`/`msg_type`/`session_id`, not just
    `payload` (N3-8/R7: a payload-only signature was valid for delivery to
    *any* recipient/type, since those fields sit outside what was signed yet
    drive routing). `relay_server.py`'s `send_message()` reconstructs this
    same dict to verify — the fourth signed contract, and the first one this
    module also single-sources on the verifier side, not just both clients."""
    return {"to_agent": to_agent, "type": msg_type, "session_id": session_id, "payload": payload}
