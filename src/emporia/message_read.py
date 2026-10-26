"""Shared response-side read-loop for DM/room messages.

`wire.py` unified request *construction* across `relay_server.py`, `agent_sdk.py`
and `mcp_server.py` (P6, P10) — the same dict shape sent over the wire, built once.
The response side never was: `agent_sdk.get_dm_messages`/`get_room_messages` and
`mcp_server.get_room_messages`/`get_dm_messages` each independently re-implement
an identical per-message annotation loop (nonce-replay tracking, signature
verification against the stored content, then the encrypted/decrypted/content
three-state decrypt attempt) — four near-byte-for-byte copies, growing by one
divergence per pass since P17 (most recently P20-3's `looks_like_any_envelope`
read-path gate, applied to all four independently). `annotate_messages()` below
is the one definition of that loop.

The only I/O any of the four originals did inside the loop was the per-sender
public-key lookup, and it differs by transport (async in `agent_sdk.py`, sync in
`mcp_server.py`) and by how the caller identity is threaded through
(`agent_sdk.py`'s `_sender_public_key` closes over the bearer identity implicitly;
`mcp_server.py`'s takes the caller explicitly as a first argument). Rather than
inject that asymmetry into this module, callers resolve every distinct
`sender_id` in `messages` *before* calling this function and pass the result as
`pubkeys` — this function is then pure and synchronous, callable unchanged from
both transports. Likewise `verify` is a callback the caller closes over
`identity.verify_room_message`/`verify_dm_message` and `scope_id` with, so this
module needs no `identity` import (matching `wire.py`'s own "no `identity`
import" invariant) — only `crypto_box`, which every one of the four originals
already needed directly.
"""

from __future__ import annotations

import json
from typing import Any, Callable


def annotate_messages(
    messages: list[dict[str, Any]],
    *,
    kind: str,
    scope_id: str,
    viewer_agent_id: str,
    private_key: Any,
    pubkeys: dict[str, str],
    verify: Callable[[dict[str, Any], str], bool | None],
) -> None:
    """Mutate `messages` in place with the shared per-message annotation all
    four original read loops performed identically:

    - `replayed`: a nonce seen earlier *on this page* (P18-2/P19-5). Per-call
      state only — a duplicate arriving on a later page or a later call is not
      detected; recorded as a standing limit, not new here.
    - `verified`: the sender's signature checked against the *stored* content,
      before any decryption attempt — for an encrypted thread/room that
      envelope is what was actually signed. `None` means unknown (no
      signature/nonce on the row), not tampered with.
    - `encrypted`/`decrypted`: crypto_box's three-state contract (P17-3).
      `(False, False)` = plaintext, `(True, True)` = opened, `(True, False)` =
      ciphertext that failed to open — including a stale-version envelope,
      which must reach `open_sealed()` and land here rather than being
      misreported as plaintext (P20-3; see `crypto_box.looks_like_any_envelope`).
    - `decrypt_error` (P21-4, B6): the `DecryptionError` reason, when decryption
      was attempted and failed. All four original sites discarded this with a
      bare `except crypto_box.DecryptionError: pass` — `(True, False)` alone
      cannot distinguish a stale version from tampered ciphertext from a wrong
      key. `None` whenever decryption wasn't attempted or succeeded.

    `kind`/`scope_id` are `("dm", thread_id)` or `("room", room_id)` — passed
    straight through to `crypto_box.message_aad()`, matching what the sender
    signed/sealed under. `viewer_agent_id` is the recipient identity handed to
    `crypto_box.open_sealed()` (the agent whose wrapped key to unwrap).
    """
    from emporia import crypto_box

    seen_nonces: set[str] = set()
    for msg in messages:
        nonce = msg.get("nonce") or ""
        msg["replayed"] = bool(nonce) and nonce in seen_nonces
        if nonce:
            seen_nonces.add(nonce)

        sender = msg.get("sender_id", "")
        if msg.get("deleted_at"):
            # P192: its text was removed, so its own signature has nothing to
            # check; the removal's signature is checked instead, when the one
            # who removed it is the one who wrote it.
            msg["verified"] = None
            msg["decrypted"] = False
            msg["decrypt_error"] = None
            msg["encrypted"] = False
            removal = msg.get("removed") or {}
            if removal and removal.get("by") == sender and pubkeys.get(sender):
                from emporia.identity import verify as _verify
                from emporia.wire import signed_delete_payload
                item = (f"{scope_id}/{nonce}" if kind == "dm" else msg.get("message_id", ""))
                removal["verified"] = _verify(
                    signed_delete_payload(f"{kind}_message", item, removal.get("by", ""), removal.get("at", "")),
                    removal.get("signature", ""), pubkeys[sender])
            continue
        msg["verified"] = verify(msg, pubkeys.get(sender, ""))

        msg["decrypted"] = False
        msg["decrypt_error"] = None
        msg["encrypted"] = crypto_box.looks_like_any_envelope(msg.get("content", ""))
        if not msg["encrypted"]:
            continue
        try:
            msg["content"] = crypto_box.open_sealed(
                json.loads(msg["content"]), viewer_agent_id, private_key,
                crypto_box.message_aad(
                    kind, scope_id, msg.get("sender_id", ""), msg.get("msg_type", ""),
                    msg.get("nonce") or "",
                ),
            )
            msg["decrypted"] = True
        except crypto_box.DecryptionError as e:
            msg["decrypt_error"] = str(e)
