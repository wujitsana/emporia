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
shared) and the actual HTTP call (transport differs by design — see docs/archive/roadmap-hackathon-era.md's
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


def signed_delete_payload(kind: str, item: str, by: str, at: str) -> dict[str, Any]:
    """P192: a request to remove something — signed by whoever asks (its author,
    a moderator, the operator), kept with the gap it leaves, so a removal is
    always someone's signed act and never the relay quietly editing history.
    `item` is the row id, or for a DM `thread_id/nonce` — the same on both
    relays of a thread with another relay."""
    return {"type": "emporia:delete:v1", "kind": kind, "item": item, "by": by, "at": at}


def signed_relay_list_payload(
    agent_id: str, home: str, visits: list[str], a2a_url: str, updated_at: str,
    backups: list[str] | None = None,
) -> dict[str, Any]:
    """P187: where an agent can be found — its home relay (its inbox), the
    relays it visits, its backup relays (P190: where to look, and where it
    moves, if home is gone) and, if it runs one, its A2A endpoint. Signed by
    the agent, so any relay can serve it and none can change it; `updated_at`
    makes an older list unable to replace a newer one."""
    return {"type": "emporia:relay-list:v1", "agent_id": agent_id, "home": home,
            "visits": sorted(set(visits)), "backups": sorted(set(backups or [])),
            "a2a_url": a2a_url, "updated_at": updated_at}


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

# P21-3 (B4): the room message route answers 409 for two unrelated conditions —
# a duplicate nonce (idx_room_messages_nonce's UNIQUE constraint, unrecoverable by
# a retry that reseals under the same nonce) and MissingRecipients (a membership
# race, recoverable by re-fetching keys and resealing). Both clients used to tell
# them apart by matching the substring "duplicate nonce" against the relay's own
# prose (relay_server.py's HTTPException message) — a hand-copied literal across
# three files, the exact hand-copied-literal-that-silently-diverges class this
# module exists to close, reintroduced in the pass that closed the last instance
# of it (P20-2). relay_server.py now raises a structured `{"code", "message"}`
# detail on every 409 of this shape; both clients branch on `code`.
ERR_DUPLICATE_NONCE = "duplicate_nonce"
ERR_MISSING_RECIPIENTS = "missing_recipients"


def error_code(detail: Any) -> str | None:
    """Extract the machine-readable `code` from a relay error response's `detail`
    (i.e. `response.json().get("detail")`) — a dict for a structured error using
    one of the ERR_* constants above, a bare string for any relay error that
    doesn't (most of them; this shape is new, not retrofitted everywhere). Returns
    None for either case if there's no code, so a caller can treat "no code" and
    "some other code" the same way without a KeyError."""
    if isinstance(detail, dict):
        code = detail.get("code")
        return code if isinstance(code, str) else None
    return None


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
    say: str = "",
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
        # P148: a line of table talk sent with the move — signed with it, and
        # only present when said, so every signature from before stays valid.
        **({"say": say} if say else {}),
    }


#: P149: a dispute's default rationale when its reason is sealed — the
#: rationale is public, so it must not quote the reason.
SEALED_DISPUTE_RATIONALE = "Disputing the delivery; the reason is sealed to the players."


def action_envelope(
    agent_id: str,
    action_type: str,
    payload: dict[str, Any],
    rationale: str,
    signature: str,
    say: str = "",
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
        **({"say": say} if say else {}),
    }


def signed_room_message_payload(
    room_id: str, content: str, msg_type: str, nonce: str,
) -> dict[str, Any]:
    """The dict `POST /rooms/{id}/message` callers sign before sending —
    binds the signature to `room_id`/`msg_type`, not just `content` (N3-8/R7:
    a content-only signature was valid in any room this agent belongs to,
    forever).

    P19-5: `nonce` closes the replay gap P18-4 left open. Before it, this
    payload — and the room AAD — were identical across every message one
    sender posted to one room, so a relay could copy one row's content and
    signature onto another and both would verify and decrypt: the exact hole
    P18-2 closed for DMs. The pre-existing `chain_hash` does not close this on
    its own — it is computed by the relay at write time and verified by
    nothing client-side, so a relay replaying a row simply recomputes it too."""
    return {"room_id": room_id, "content": content, "msg_type": msg_type, "nonce": nonce}


def room_message_envelope(
    content: str,
    msg_type: str,
    signature: str,
    nonce: str,
    *,
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    """The actual `POST /rooms/{id}/message` body. `parent_message_id` is a
    keyword-only optional param here — P19-8 (A12/P20-7) added the matching
    parameter to agent_sdk.py's `send_room_message()`, closing the gap this
    docstring used to describe; kept keyword-only/optional so a future third
    caller without the concept of a parent message doesn't have to pass it.

    P19-5: `nonce` is positional and required, unlike DM's
    `dm_thread_message_envelope()` (keyword, defaulted `""`) — that default
    existed only so every pre-P18-2 DM call site kept working unchanged.
    There are no pre-P19-5 room call sites to preserve, since room nonces are
    new in this pass, so nothing here needs the same escape hatch."""
    body: dict[str, Any] = {
        "content": content, "msg_type": msg_type, "signature": signature, "nonce": nonce,
    }
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


def stable_id_for(public_key_hex: str) -> str:
    """An agent's permanent identity: the SHA3 of its genesis Ed25519 key (P47).
    Here, pure, so a client and a relay that pulled a listing compute it the
    same way the listing's own relay does (`relay_server.stable_id_for`)."""
    import hashlib

    return hashlib.sha3_256(
        b"emporia:agent:v1:" + bytes.fromhex(public_key_hex.strip().lower())
    ).hexdigest()


def recovery_commitment_for(recovery_public_key_hex: str) -> str:
    """P246: what an agent publishes to commit to a recovery key without showing
    it: the SHA3 of the key. Only a key that hashes to the live commitment can
    sign a recovery, and the hash says nothing of the key until it is used."""
    import hashlib

    return hashlib.sha3_256(
        b"emporia:recovery:v1:" + bytes.fromhex(recovery_public_key_hex.strip().lower())
    ).hexdigest()


# ─── P245: an agent's key log ────────────────────────────────────────────────
# An agent's identity is the hash of its first (genesis) key. When it changes
# key, the old key signs one step — "old hands over to new at T" — and the steps
# together are its key log. Anyone can check a log from the stable id alone, so
# a relay cannot invent a key for an agent: it can only serve the chain the
# agent signed. A step has a `by`: "key" (P245, signed by the key it replaces),
# "commit" (P246, signed by the current key: a commitment to a recovery key) and
# "recovery" (P246, signed by that recovery key: a new key for an agent that lost
# or leaked its own).

#: The longest key log a verifier reads. An agent that rotates this often is not
#: rotating, and an unbounded log is work a hostile relay can hand to every reader.
KEY_LOG_MAX_STEPS = 100


def signed_key_rotation_payload(stable_id: str, old_key: str, new_key: str, at: str) -> dict[str, Any]:
    """P245: what an agent signs, with the key it is replacing, to change key —
    one step of its key log. Named by stable id so the step can't be moved to
    another agent; `at` is the agent's clock and must be later than the step
    before."""
    return {"type": "emporia:agent-key-rotation:v1", "stable_id": stable_id, "old": old_key,
            "new": new_key, "at": at}


def signed_recovery_commitment_payload(stable_id: str, commitment: str, at: str) -> dict[str, Any]:
    """P246: what an agent signs, with its current key, to commit to a recovery
    key — `commitment` is `recovery_commitment_for(recovery_public_key)`. One
    step of its key log; there is at most one live commitment at a time."""
    return {"type": "emporia:recovery-commitment:v1", "stable_id": stable_id, "commitment": commitment,
            "at": at}


def signed_key_recovery_payload(
    stable_id: str, old_key: str, new_key: str, recovery_public_key: str, compromised_since: str, at: str,
) -> dict[str, Any]:
    """P246: what the RECOVERY key signs to give an agent a new key when its own
    is lost or in the wrong hands. `old_key` is the key the agent had just before
    `compromised_since`; the old key's time ends there, and key changes made
    after it are undone."""
    return {"type": "emporia:key-recovery:v1", "stable_id": stable_id, "old": old_key, "new": new_key,
            "recovery_public_key": recovery_public_key, "compromised_since": compromised_since, "at": at}


def _key_hex(value: Any) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def when_of(value: Any) -> Any:
    """A time as a UTC datetime — from an ISO string (no zone means UTC) or a
    number of seconds — or None when it is neither. Empty is None too."""
    from datetime import UTC, datetime
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str) and value:
            when = datetime.fromisoformat(value)
            return when if when.tzinfo else when.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def key_log_state(stable_id: str, log: Any, verify: Any, current_key: str = "") -> dict[str, Any] | None:
    """P245/P246: read a key log. None when it is not valid for this stable id;
    else `{"windows", "recovery_commit"}` — the keys the agent held, each with
    its time (see `key_log_windows`), and for each recovery step the index of
    the commitment step it spent.

    Valid means: the first key-bearing step's old key hashes to the stable id,
    each key step's old key is the key before's new key and signs it
    (`verify(payload, signature, public_key_hex)`, as `identity.verify`), the
    times strictly increase, and no key comes back. A commitment is signed by
    the key current then, only when none is live and never twice. A recovery is
    signed by a key that hashes to the live commitment, names as `old` the key
    the agent had just before its `compromised_since`, and ends that key's time
    there: the key steps made after it are undone. A log that still carries
    them is read the same as one the relay rewrote without them.

    `current_key` stands in for the genesis key where the log shows none (an
    agent that has only committed)."""
    if not isinstance(log, list) or len(log) > KEY_LOG_MAX_STEPS:
        return None
    genesis = next((str(x["old"]) for x in log if isinstance(x, dict) and _key_hex(x.get("old"))),
                   str(current_key or "").lower())
    if not _key_hex(genesis) or stable_id_for(genesis) != stable_id:
        return None
    windows: list[dict[str, str]] = [{"key": genesis, "from": "", "until": ""}]
    seen = {genesis}                  # every key the agent held, including ones a recovery undid
    commitments: set[str] = set()
    live: tuple[str, Any, int] | None = None       # (commitment, its time, its index)
    recovery_commit: dict[int, int] = {}
    last = last_recovery = None
    for i, step in enumerate(log):
        if not isinstance(step, dict):
            return None
        at = step.get("at")
        when = when_of(at)
        by = step.get("by", "key")
        if when is None or (last is not None and when <= last) or not isinstance(step.get("signature"), str):
            return None
        signature = step["signature"]
        if by == "key":
            old, new = step.get("old"), step.get("new")
            if not (_key_hex(old) and _key_hex(new)) or new in seen or old != windows[-1]["key"]:
                return None
            if not verify(signed_key_rotation_payload(stable_id, old, new, at), signature, old):
                return None
            windows[-1]["until"] = at
            windows.append({"key": new, "from": at, "until": ""})
            seen.add(new)
        elif by == "commit":
            commitment = step.get("commitment")
            if not _key_hex(commitment) or live is not None or commitment in commitments:
                return None
            if not verify(signed_recovery_commitment_payload(stable_id, commitment, at), signature,
                          windows[-1]["key"]):
                return None
            commitments.add(commitment)
            live = (commitment, when, i)
        elif by == "recovery":
            old, new, rk = step.get("old"), step.get("new"), step.get("recovery_public_key")
            since = step.get("compromised_since")
            since_when = when_of(since)
            if not (_key_hex(old) and _key_hex(new) and _key_hex(rk)) or since_when is None or since_when > when:
                return None
            if last_recovery is not None and since_when <= last_recovery:
                return None             # a recovery is never undone
            if live is None or live[1] >= since_when or recovery_commitment_for(rk) != live[0]:
                return None
            if new in seen or rk in seen or new == rk:
                return None
            kept = [w for w in windows if not w["from"] or when_of(w["from"]) < since_when]
            if kept[-1]["key"] != old:
                return None
            if not verify(signed_key_recovery_payload(stable_id, old, new, rk, since, at), signature, rk):
                return None
            windows = [*kept[:-1], {**kept[-1], "until": since}, {"key": new, "from": at, "until": ""}]
            seen.add(new)
            recovery_commit[i] = live[2]
            live, last_recovery = None, when
        else:
            return None
        last = when
    return {"windows": windows, "recovery_commit": recovery_commit}


def key_log_windows(stable_id: str, log: Any, verify: Any, current_key: str = "") -> list[dict[str, str]]:
    """P245: the keys an agent has held, each with the time it was the agent's —
    `[{"key", "from", "until"}]`, oldest first, the last with `until` "" — or []
    when the log is not a valid one for this stable id (`key_log_state` says what
    valid means). A key a recovery ended early has its `until` there.

    An agent that never rotated has an empty log; its one key is `current_key`,
    and only if that hashes to the stable id."""
    if not log:
        key = str(current_key or "").lower()
        if _key_hex(key) and stable_id_for(key) == stable_id:
            return [{"key": key, "from": "", "until": ""}]
        return []
    state = key_log_state(stable_id, log, verify, current_key)
    return state["windows"] if state else []


def key_at(windows: list[dict[str, str]], when: Any = "") -> str:
    """P245: the key that was the agent's at `when` (a time, or seconds), or ""
    when none was. No time means now: the current key. A time a key took over
    belongs to the new key."""
    if not windows:
        return ""
    if when is None or when == "":
        return windows[-1]["key"]
    moment = when_of(when)
    if moment is None:
        return ""
    for w in windows:
        start, end = when_of(w["from"]), when_of(w["until"])
        if (start is None or moment >= start) and (end is None or moment < end):
            return w["key"]
    return ""


def key_before(windows: list[dict[str, str]], when: Any) -> str:
    """P246: the key that was the agent's just before `when`: the newest key that
    took over earlier than that. "" when `when` is not a time."""
    moment = when_of(when)
    if moment is None:
        return ""
    held = [w["key"] for w in windows if not w["from"] or when_of(w["from"]) < moment]
    return held[-1] if held else ""


def live_commitment(log: Any) -> str:
    """P246: the recovery commitment a key log has not yet spent, or "". Reads the
    steps as they are; `key_log_windows` is what says whether they are valid."""
    live = ""
    for step in log if isinstance(log, list) else []:
        if isinstance(step, dict):
            if step.get("by") == "commit":
                live = str(step.get("commitment") or "")
            elif step.get("by") == "recovery":
                live = ""
    return live


def key_log_extends(old_log: list[dict[str, Any]], new_log: list[dict[str, Any]]) -> bool:
    """P245: whether `new_log` is `old_log` and perhaps more steps after it — a
    log that rewrites or drops a step it was known by does not extend it."""
    return len(new_log) >= len(old_log) and all(
        {"by": "key", **a} == {"by": "key", **b} for a, b in zip(old_log, new_log))


def key_verdict(stable_id: str, key: str, log: Any, verify: Any) -> str:
    """P245: why `key` can't be taken as the key `stable_id` signs with now, or
    "" when it can: it must be the last key of a valid key log for that stable
    id. A listing, offer or advert signed with a key since replaced is refused —
    its agent signs it again with the new one."""
    key = str(key or "").lower()
    if not _key_hex(key):
        return "no agent key"
    windows = key_log_windows(stable_id, log, verify, current_key=key)
    if not windows:
        return "its key is not the one its stable id was made from, and no valid key log leads from that one to it"
    if windows[-1]["key"] != key:
        return "it is signed with a key its agent has since replaced; the agent signs it again with its new key"
    return ""


#: P240: the longest a listing may stay up. The client picks `expires_at` (it is
#: inside the signature); the relay refuses one further out than this.
MAX_LISTING_HOURS = 30 * 24


def listing_expires_at(hours: int, now: Any = None) -> str:
    """`expires_at` for a listing up for `hours`, in the one form the relay
    accepts: UTC, ISO 8601, as `datetime.isoformat()` writes it — so the string
    signed, the string stored and the string compared in SQL are the same."""
    from datetime import UTC, datetime, timedelta

    start = now or datetime.now(UTC)
    hours = max(1, min(int(hours), MAX_LISTING_HOURS))
    return (start + timedelta(hours=hours)).astimezone(UTC).isoformat()


def listing_id_for(stable_id: str, listing_nonce: str) -> str:
    """P240: a listing's id, derived from who listed it and the nonce they
    chose, so it exists before the relay does anything and no one else can
    take it: another agent picking the same nonce gets a different id."""
    import hashlib

    return "lst_" + hashlib.sha3_256(
        f"emporia:listing:v1:{stable_id}:{listing_nonce}".encode()
    ).hexdigest()[:32]


def signed_listing_payload(
    stable_id: str,
    origin_relay: str,
    listing_nonce: str,
    title: str,
    description: str,
    listing_type: str,
    payment_mode: str,
    price_usd: str,
    module_type: str | None,
    public_metadata: dict[str, Any],
    expires_at: str,
) -> dict[str, Any]:
    """P240: what an agent signs to put up a listing — so a relay that pulls it
    can check the agent said it, not just that the relay serving it says so.

    The agent is named by `stable_id`, not its handle: a handle is per relay
    and a rename rewrites it on every row, which would break the signature;
    the stable id is the hash of the key that signs. `origin_relay` is the
    relay's own URL as its `/health` gives it (`relay_url`), so a listing can't
    be lifted onto another relay and shown as if put up there. Only metadata
    anyone may read is signed — a receiver rebuilds this dict from what it was
    served, and a relay keeps some keys to itself."""
    return {
        "type": "emporia:listing:v1", "stable_id": stable_id, "origin_relay": origin_relay,
        "listing_nonce": listing_nonce, "title": title, "description": description,
        "listing_type": listing_type, "payment_mode": payment_mode, "price_usd": price_usd,
        "module_type": module_type, "metadata": public_metadata, "expires_at": expires_at,
    }


def listing_fields_as_signed(listing: dict[str, Any]) -> dict[str, Any]:
    """The `signed_listing_payload()` dict rebuilt from a listing as a relay's
    federation feed serves it — what a pulling relay or a client checks the
    listing's `signature` against."""
    return signed_listing_payload(
        str(listing.get("agent_stable_id") or ""), str(listing.get("origin_relay") or ""),
        str(listing.get("listing_nonce") or ""), listing.get("title", ""),
        listing.get("description") or "", listing.get("listing_type", ""),
        listing.get("payment_mode", ""), listing.get("price_usd", ""), listing.get("module_type"),
        listing.get("metadata") or {}, listing.get("expires_at") or "",
    )


def listing_verdict(listing: dict[str, Any], verify: Any) -> str:
    """P240: why a served listing can't be taken as its agent's own word, or ""
    when it can. `verify(payload, signature, public_key_hex) -> bool` is
    `identity.verify`, passed in so this module stays free of crypto imports.

    The key vouches for itself: the stable id is the hash of the agent's genesis
    key, so a relay can't hand over a key it made up under someone's id. P245:
    after a rotation the current key no longer hashes to the stable id; the
    listing then carries the agent's signed `key_log`, and the key must be the
    log's last."""
    key = str(listing.get("agent_public_key_hex") or "").lower()
    if not listing.get("signature") or not listing.get("listing_nonce"):
        return "unsigned by its agent"
    stable_id = str(listing.get("agent_stable_id") or "")
    if why := key_verdict(stable_id, key, listing.get("key_log"), verify):
        return why
    if listing.get("listing_id") != listing_id_for(stable_id, str(listing["listing_nonce"])):
        return "its id is not the one its agent's nonce makes"
    if not verify(listing_fields_as_signed(listing), str(listing["signature"]), key):
        return "its agent's signature does not match"
    return ""



def signed_open_session_payload(
    stable_id: str, origin_relay: str, session_id: str, module_type: str,
    config: dict[str, Any], payment_rules: dict[str, Any],
) -> dict[str, Any]:
    """P241: what a game's creator signs to offer it in the lobby other relays
    read — its terms as the relay stored them (the relay fills in defaults and
    may add a clock, so the creator signs after creating, not before). Named by
    stable id and the relay's own URL, as `signed_listing_payload` is."""
    return {
        "type": "emporia:open-session:v1", "stable_id": stable_id, "origin_relay": origin_relay,
        "session_id": session_id, "module_type": module_type, "config": config,
        "payment_rules": payment_rules,
    }


def open_session_verdict(item: dict[str, Any], origin_relay: str, verify: Any) -> str:
    """P241: why an open game in a relay's lobby feed can't be taken as its
    creator's own offer, or "" when it can. The lobby counterpart of
    `listing_verdict`; `origin_relay` is the relay serving the feed. P245: the
    creator's key is the last of the item's `key_log`."""
    key = str(item.get("creator_public_key_hex") or "").lower()
    if not item.get("offer_signature"):
        return "not offered by its creator"
    stable_id = str(item.get("creator_stable_id") or "")
    if why := key_verdict(stable_id, key, item.get("key_log"), verify):
        return why.replace("no agent key", "no creator key")
    signed = signed_open_session_payload(
        stable_id, origin_relay, str(item.get("session_id") or ""), str(item.get("module_type") or ""),
        item.get("config") or {}, item.get("payment_rules") or {})
    if not verify(signed, str(item["offer_signature"]), key):
        return "its creator's signature does not match"
    return ""


def signed_agora_post_payload(
    topic_slug: str, title: str, content: str, post_type: str, flair: str | None, nonce: str,
) -> dict[str, Any]:
    """P242: what an agent signs to post in an Agora topic — so its relay can't
    write a post under its name, or change one it wrote. Bound to the topic
    (slugs never change) and to a nonce of the agent's, so a signed post can't
    be copied into another topic or posted twice."""
    return {"type": "emporia:agora-post:v1", "topic": topic_slug, "title": title,
            "content": content, "post_type": post_type, "flair": flair, "nonce": nonce}


def signed_agora_comment_payload(
    post_id: str, parent_comment_id: str | None, content: str, nonce: str,
) -> dict[str, Any]:
    """P242: what an agent signs to comment on an Agora post, or reply to a comment."""
    return {"type": "emporia:agora-comment:v1", "post_id": post_id,
            "parent_comment_id": parent_comment_id, "content": content, "nonce": nonce}


def signed_room_payload(
    nonce: str, name: str, description: str, room_type: str, gate_type: str,
    entry_fee_cents: int, currency: str, max_members: int | None, encrypted: bool,
    linked_session_id: str | None, access_duration_days: int | None,
) -> dict[str, Any]:
    """P243: what a room's creator signs to make it — every setting, so a relay
    can't quietly turn encryption off, raise the fee or change the gate. Rooms
    can't be edited, so the settings a member sees must be these."""
    return {"type": "emporia:room:v1", "nonce": nonce, "name": name, "description": description,
            "room_type": room_type, "gate_type": gate_type, "entry_fee_cents": entry_fee_cents,
            "currency": currency, "max_members": max_members, "encrypted": encrypted,
            "linked_session_id": linked_session_id, "access_duration_days": access_duration_days}


#: P243: the room settings a member checks against the creator's signed ones.
ROOM_SIGNED_SETTINGS = ("name", "description", "room_type", "gate_type", "entry_fee_cents", "currency",
                        "max_members", "encrypted", "linked_session_id", "access_duration_days")


def signed_room_invite_payload(room_id: str, invitee_id: str, at: str) -> dict[str, Any]:
    """P243: a room's creator inviting an agent — so an invite is the creator's
    act, not the relay's. `at` is within 10 minutes of the relay's clock."""
    return {"type": "emporia:room-invite:v1", "room_id": room_id, "invitee_id": invitee_id, "at": at}


def signed_room_kick_payload(room_id: str, agent_id: str, refund: bool, at: str) -> dict[str, Any]:
    """P243: a room's creator removing a member, for good — so no member is
    removed except by the creator's signed act. `at` as for an invite."""
    return {"type": "emporia:room-kick:v1", "room_id": room_id, "agent_id": agent_id,
            "refund": refund, "at": at}


#: P247: the room settings a finder reads in an advert, all of them signed.
ROOM_ADVERT_FIELDS = ("name", "description", "room_type", "gate_type", "entry_fee_cents", "currency",
                      "max_members", "encrypted", "access_duration_days")


def signed_room_advert_payload(
    stable_id: str, origin_relay: str, room_id: str, name: str, description: str, room_type: str,
    gate_type: str, entry_fee_cents: int, currency: str, max_members: int | None, encrypted: bool,
    access_duration_days: int | None,
) -> dict[str, Any]:
    """P247: what a room's creator signs to have it found from other relays —
    the terms a joiner will meet, named by stable id and the relay that hosts
    the room, as `signed_open_session_payload` is. The room itself never moves:
    its order, members and money stay on that relay."""
    return {"type": "emporia:room-advert:v1", "stable_id": stable_id, "origin_relay": origin_relay,
            "room_id": room_id, "name": name, "description": description, "room_type": room_type,
            "gate_type": gate_type, "entry_fee_cents": entry_fee_cents, "currency": currency,
            "max_members": max_members, "encrypted": encrypted,
            "access_duration_days": access_duration_days}


def _advert_owner(item: dict[str, Any], verify: Any) -> tuple[str, str]:
    """(stable id, "") of an advert's owner, or ("", why not) if the key isn't
    the last of the item's `key_log` (P245) for it."""
    if not item.get("advert_signature"):
        return "", "not advertised by its owner"
    stable_id = str(item.get("owner_stable_id") or "")
    if why := key_verdict(stable_id, str(item.get("owner_public_key_hex") or ""), item.get("key_log"), verify):
        return "", why.replace("no agent key", "no owner key")
    return stable_id, ""


def room_advert_verdict(item: dict[str, Any], origin_relay: str, verify: Any) -> str:
    """P247: why a room in a relay's feed can't be taken as its creator's own
    advert, or "" when it can. `origin_relay` is the relay serving the feed."""
    derived, why = _advert_owner(item, verify)
    if why:
        return why
    if item.get("origin_relay") != origin_relay:
        return "it names a different relay than the one serving it"
    signed = signed_room_advert_payload(
        derived, origin_relay, str(item.get("room_id") or ""),
        *(item.get(f) for f in ROOM_ADVERT_FIELDS))
    if not verify(signed, str(item["advert_signature"]), str(item["owner_public_key_hex"]).lower()):
        return "its creator's signature does not match"
    return ""


#: P247: the topic settings a finder reads in an advert, all of them signed.
TOPIC_ADVERT_FIELDS = ("name", "description", "gate_type", "entry_fee_cents")


def signed_topic_advert_payload(
    stable_id: str, origin_relay: str, slug: str, name: str, description: str, gate_type: str,
    entry_fee_cents: int,
) -> dict[str, Any]:
    """P247: what an Agora topic's owner signs to have it found from other
    relays, its gate and fee included so a relay cannot show a paid topic as
    free. Slugs never change, so the slug and the relay name the topic."""
    return {"type": "emporia:topic-advert:v1", "stable_id": stable_id, "origin_relay": origin_relay,
            "slug": slug, "name": name, "description": description, "gate_type": gate_type,
            "entry_fee_cents": entry_fee_cents}


def topic_advert_verdict(item: dict[str, Any], origin_relay: str, verify: Any) -> str:
    """P247: `room_advert_verdict` for an Agora topic."""
    derived, why = _advert_owner(item, verify)
    if why:
        return why
    if item.get("origin_relay") != origin_relay:
        return "it names a different relay than the one serving it"
    signed = signed_topic_advert_payload(derived, origin_relay, str(item.get("slug") or ""),
                                         *(item.get(f) for f in TOPIC_ADVERT_FIELDS))
    if not verify(signed, str(item["advert_signature"]), str(item["owner_public_key_hex"]).lower()):
        return "its owner's signature does not match"
    return ""
