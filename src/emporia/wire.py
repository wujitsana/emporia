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
) -> dict[str, Any]:
    """Body for `POST /agents/register` — the second half of the
    challenge/sign/register handshake both clients use
    (mcp_server.py's `register_agent()`, agent_sdk.py's `register()`)."""
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "public_key_hex": public_key_hex,
        "display_name": display_name,
        "challenge_id": challenge_id,
        "challenge_signature": challenge_signature,
    }
    if nous_jwt:
        body["identity_claims"] = [{"provider": "nous", "token": nous_jwt}]
    return body


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
