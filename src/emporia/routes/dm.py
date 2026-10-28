"""Direct message routes (P57).

Moved out of `relay_server` by scratchpad/extract_router.py. The only edits are
`@app` becoming `@router` and shared relay state resolving through `rs.` at call
time rather than being bound at import — see `emporia.routes` for why that
distinction is load-bearing for the suite's 43 patch sites.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
import sqlite3
from typing import Any, NamedTuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from emporia import relay_server as rs
from emporia import rooms as rooms_db
from emporia.identity import (
    federation_signable_bytes,
    get_relay_public_key_hex,
    sign_raw_relay,
)

router = APIRouter()


# ============================================================================
# DM (Direct Messages) — agent-to-agent private threads
# ============================================================================

class DMStartRequest(BaseModel):
    to_agent: str
    # P16-5: tri-state, and the third state is the common case. `True` = this thread
    # must be encrypted (409 if an existing one isn't — the dangerous direction, never
    # silently downgraded). `False` = explicitly asking for plaintext (409 if the
    # existing thread is encrypted, so the caller gets a clear error rather than a
    # confusing 400 on their first send). `None`/omitted = the default, which P122 made
    # encrypted: a new thread is encrypted, and an existing plaintext one upgraded
    # one-way, whenever both agents have published a key; plaintext only when one
    # has not. It is what a client that just wants to reach an agent means, and the
    # only value that lets one round trip both find an existing thread and learn
    # its mode. Started as a plain `bool` in P16-4; building the clients showed
    # that forced every send path into a thread-list scan just to avoid a spurious 409.
    encrypted: bool | None = None


# P19 (M3): what an honest nonce looks like — `wire.new_message_nonce()` is
# `secrets.token_hex(16)` (32 hex chars); the upper bound leaves room for a future
# longer nonce without immediately needing a second constant.


class DMSendRequest(BaseModel):
    content: str
    msg_type: str = ""
    signature: str | None = None
    # P18-2: sender-chosen, inside the signature and the AAD. Empty is rejected
    # rather than defaulted — a silent fallback would leave replay open to any
    # client that just omitted the field.
    nonce: str = ""




@router.post("/dm/start")
async def dm_start(req: DMStartRequest, request: Request):
    """Create or retrieve a DM thread between two agents. Idempotent."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    from emporia.addresses import is_address
    if is_address(req.to_agent):
        from emporia import federation_dm as fd
        agent_id, peer = fd.resolve(req.to_agent)
        if peer:
            rs._enforce_agent_rate_limit(caller)
            return await _dm_start_remote(caller, agent_id, peer, req.encrypted)
        req.to_agent = agent_id        # an address on this relay: an ordinary DM
    rs._require_registered(req.to_agent)
    from emporia import moves
    moves.gone(req.to_agent)           # P190: it moved away — say where
    rs._enforce_agent_rate_limit(caller)
    if caller == req.to_agent:
        raise HTTPException(400, "Cannot DM yourself")
    # Canonical ordering so UNIQUE(agent_a, agent_b) always matches
    a, b = sorted([caller, req.to_agent])
    if req.encrypted:
        rs._require_encryption_keys_published([a, b])
    # P122: encrypted by default. Omitting the flag used to mean plaintext, so
    # an agent that never thought about it — the usual case — sent prose the
    # relay could read. Now omitted means encrypted whenever both agents have
    # published a key; `encrypted: false` still asks for plaintext explicitly.
    both_keys = all(rs.AGENT_REGISTRY.get_encryption_key(x)[0] for x in (a, b))
    want = req.encrypted if req.encrypted is not None else both_keys
    now = datetime.now(UTC).isoformat()
    thread_id = f"dm_{uuid.uuid4().hex[:12]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        existing = conn.execute(
            "SELECT thread_id, encrypted FROM dm_threads WHERE agent_a=? AND agent_b=?", (a, b)
        ).fetchone()
        if existing:
            # P16: this route is idempotent on the *pair*, so without this check
            # asking for an encrypted thread when a plaintext one already exists
            # would hand back the plaintext thread — and a client that didn't
            # re-read the flag would post prose believing it was encrypted. That
            # silent downgrade is the worst failure mode this feature has, so the
            # mismatch is a 409 rather than a quietly different answer. The mirror
            # case conflicts too: a caller expecting plaintext must not be handed
            # an encrypted thread and post prose into it. `POST /dm/{id}/encrypt`
            # is how an existing thread changes mode, deliberately and one-way.
            if req.encrypted is not None and bool(existing["encrypted"]) != req.encrypted:
                state = "encrypted" if existing["encrypted"] else "not encrypted"
                raise HTTPException(
                    409,
                    f"a DM thread with '{req.to_agent}' already exists and is {state}. "
                    f"Use POST /dm/{existing['thread_id']}/encrypt to enable encryption "
                    f"on it (encryption cannot be turned off).",
                )
            if req.encrypted is None and both_keys and not existing["encrypted"]:
                # P122: an existing plaintext thread is upgraded the same
                # one-way way POST /dm/{id}/encrypt does. Stored messages stay
                # as they are; everything from here on is sealed.
                conn.execute("UPDATE dm_threads SET encrypted=1 WHERE thread_id=?",
                             (existing["thread_id"],))
                return {"thread_id": existing["thread_id"], "created": False,
                        "encrypted": True, "upgraded": True}
            return {
                "thread_id": existing["thread_id"],
                "created": False,
                "encrypted": bool(existing["encrypted"]),
            }
        conn.execute(
            "INSERT INTO dm_threads "
            "(thread_id, agent_a, agent_b, created_at, last_message_at, encrypted) "
            "VALUES (?,?,?,?,?,?)",
            (thread_id, a, b, now, now, int(bool(want))),
        )
    return {
        "thread_id": thread_id, "agent_a": a, "agent_b": b,
        "created": True, "encrypted": bool(want),
    }


async def _dm_start_remote(caller: str, agent_id: str, peer: str, encrypted: bool | None) -> dict[str, Any]:
    """P185: a thread with an agent on a linked relay — always sealed, named by
    both addresses so both relays and both clients agree on it."""
    from emporia import federation_dm as fd
    if encrypted is False:
        raise HTTPException(400, "DMs between relays are always end-to-end encrypted")
    rs._require_encryption_keys_published([caller])
    me = fd.address(caller, fd.own_host())
    them = fd.address(agent_id, fd.host_of(peer))
    thread_id = fd.thread_id_for(me, them)
    with rs.DB_LOCK, rs.get_db() as conn:
        replying = conn.execute("SELECT 1 FROM dm_threads WHERE thread_id = ?", (thread_id,)).fetchone()
    if replying and fd.remote(them):
        # A thread the other agent began: it chose to write here, and its keys
        # were pinned when its first message arrived. Its opt-in is not needed.
        return {"thread_id": thread_id, "agent_a": min(me, them), "agent_b": max(me, them), "created": False,
                "encrypted": True, "federated": True, "my_label": me, "peer_label": them, "peer_relay": peer}
    record = await fd.look_up(peer, agent_id)
    them = record["address"]
    thread_id = fd.thread_id_for(me, them)
    a, b = sorted((me, them))
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        existing = conn.execute("SELECT thread_id FROM dm_threads WHERE thread_id = ?", (thread_id,)).fetchone()
        if existing:
            conn.execute("UPDATE dm_threads SET started_by_local = 1 WHERE thread_id = ?", (thread_id,))
        else:
            conn.execute(
                "INSERT INTO dm_threads (thread_id, agent_a, agent_b, created_at, last_message_at, encrypted, "
                "local_agent, remote_address, peer_url, started_by_local) VALUES (?,?,?,?,?,1,?,?,?,1)",
                (thread_id, a, b, now, now, caller, them, peer))
    return {"thread_id": thread_id, "agent_a": a, "agent_b": b, "created": not existing, "encrypted": True,
            "federated": True, "my_label": me, "peer_label": them, "peer_relay": peer}


async def _deliver_remote(thread: Any, caller: str, sender_label: str, req: "DMSendRequest",
                          msg_type: str) -> None:
    from emporia import federation_dm as fd
    if not fd.enabled() or not fd.linked_peer(fd.host_of(thread["peer_url"] or "")):
        raise HTTPException(403, "this relay no longer exchanges DMs with that one")
    record = fd.agent_record(caller)
    if not record:
        raise HTTPException(409, "publish an encryption key first")
    agent_id, _ = fd.split_address(thread["remote_address"])
    r = await fd.call_peer(thread["peer_url"], "POST", "/federation/v1/dm", {
        "from_address": sender_label, "to_agent": agent_id, "thread_id": thread["thread_id"],
        "content": req.content, "msg_type": msg_type, "nonce": req.nonce, "signature": req.signature,
        "sender": record})
    if r.status_code != 200:
        detail = (r.json() or {}).get("detail") if r.headers.get("content-type", "").startswith(
            "application/json") else r.text[:200]
        raise HTTPException(r.status_code if r.status_code in (403, 404, 409, 410, 429) else 502,
                            f"{thread['peer_url']} did not take it: {detail}")


def _my_label(thread: Any, caller: str) -> str:
    """How `caller` is named in this thread: its address in one with another relay."""
    if thread["remote_address"] and thread["local_agent"] == caller:
        return thread["agent_a"] if thread["agent_b"] == thread["remote_address"] else thread["agent_b"]
    return caller


def _dm_last_chain_hash(conn: Any, thread_id: str) -> str | None:
    """The chain head this thread's next message links onto, or None for a
    thread with no chained messages yet.

    Ordering matches `_verify_dm_chain()`'s walk exactly, `rowid` tiebreak
    included — rooms.py's `_last_chain_hash()` carries the same note (P21-7):
    two inserts landing on the same microsecond would otherwise let the writer
    chain onto a different "last" row than the verifier recomputes, breaking
    every message after the tie.
    """
    row = conn.execute(
        "SELECT chain_hash FROM dm_messages WHERE thread_id = ? AND chain_hash IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return row["chain_hash"] if row else None


class DmChainVerification(NamedTuple):
    """`_verify_dm_chain()`'s result. Mirrors `rooms.ChainVerification` field for
    field, plus `unchained_prefix` — the DM-only region P28-4 refuses to vouch
    for. `message_count` counts *chained* messages, as it always has: the
    unchained prefix is reported separately rather than folded into a total that
    would imply coverage.
    """

    ok: bool
    message: str
    head: str | None
    message_count: int
    unchained_prefix: int
    verified_count: int
    scope: str


def _verify_dm_chain(thread_id: str, limit: int | None = None) -> DmChainVerification:
    """Recompute a DM thread's hash chain and compare against each stored
    `chain_hash`.

    The DM counterpart of `rooms.verify_chain()`, and it reuses that module's
    `compute_chain_hash()` rather than restating the frame. The one real
    difference is the boundary: rooms have been chained since P18, while DM rows
    written before P28-4 have `chain_hash` NULL. Those are skipped and *counted*,
    never treated as verified — an operator can delete the entire unchained
    prefix and the chained suffix still verifies perfectly, so a bare
    `verified: true` would overclaim precisely where the guarantee stops.

    `limit` bounds the walk exactly as `rooms.verify_chain()`'s does, and for the
    same reason (P44) — kept in step with it deliberately, since P8's lesson here
    was that these two verifiers drift when only one of them gets fixed. `scope`
    carries all three outcomes: `"genesis"` (walked every message in the thread),
    `"chained_start"` (walked every *chained* message, but an unchained prefix
    sits in front of it), `"recent_window"` (stopped at the cap).
    """
    with rs.DB_LOCK, rs.get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM dm_messages WHERE thread_id = ?", (thread_id,),
        ).fetchone()["n"]
        chained_total = conn.execute(
            "SELECT COUNT(*) AS n FROM dm_messages WHERE thread_id = ? AND chain_hash IS NOT NULL",
            (thread_id,),
        ).fetchone()["n"]
        unchained_prefix = total - chained_total
        rows = []
        if chained_total:
            if limit is None or chained_total <= limit:
                rows = conn.execute(
                    "SELECT sender_id, content, created_at, chain_hash, deleted_at FROM dm_messages "
                    "WHERE thread_id = ? AND chain_hash IS NOT NULL "
                    "ORDER BY created_at ASC, rowid ASC",
                    (thread_id,),
                ).fetchall()
            else:
                # Newest-first + LIMIT, then reversed — the only shape
                # idx_dm_messages_thread can serve without reading the thread.
                rows = conn.execute(
                    "SELECT sender_id, content, created_at, chain_hash, deleted_at FROM dm_messages "
                    "WHERE thread_id = ? AND chain_hash IS NOT NULL "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (thread_id, limit + 1),
                ).fetchall()
                rows.reverse()

    if not rows:
        return DmChainVerification(
            True, "no chained messages — nothing to verify", None, 0,
            unchained_prefix, 0, "chained_start" if unchained_prefix else "genesis",
        )

    head = rows[-1]["chain_hash"]
    at_start = limit is None or chained_total <= limit
    if at_start:
        prev_hash: str | None = None
        walk, first_index = rows, 0
        scope = "chained_start" if unchained_prefix else "genesis"
    else:
        prev_hash = rows[0]["chain_hash"]
        walk, first_index = rows[1:], chained_total - len(rows) + 1
        scope = "recent_window"

    for offset, row in enumerate(walk):
        if not rooms_db.link_ok(prev_hash, row):
            return DmChainVerification(
                False, f"hash mismatch at message {first_index + offset}",
                None, chained_total, unchained_prefix, offset, scope,
            )
        prev_hash = row["chain_hash"]

    verified = len(walk)
    detail = "chain valid" if scope != "recent_window" else "recent window valid"
    return DmChainVerification(
        True, f"{detail} ({verified} of {chained_total} messages)",
        head, chained_total, unchained_prefix, verified, scope,
    )


def _dm_thread_or_404(thread_id: str, caller: str) -> Any:
    """Load a DM thread and assert `caller` is one of its two participants."""
    with rs.get_db() as conn:
        thread = conn.execute(
            "SELECT * FROM dm_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
    if not thread:
        raise HTTPException(404, "DM thread not found")
    if caller not in (thread["agent_a"], thread["agent_b"]) and caller != thread["local_agent"]:
        raise HTTPException(403, "Not a participant in this thread")
    return thread


@router.get("/dm/{thread_id}/chain")
async def get_dm_chain(thread_id: str, request: Request):
    """Chain-integrity verification for a DM thread — the DM half of
    GET /rooms/{id}/chain, which P24 built and explicitly deferred this from.

    Same gate as GET /dm/{id}/messages: participants only. A chain head plus a
    message count is metadata about a private conversation (who is talking, how
    much, and when it last changed), so the attestation route must not be more
    open than the read route it attests to.

    Like the room route, this returns hashes rather than content — the caller
    already has every message from GET /dm/{id}/messages, and the only thing a
    read cannot provide for itself is an attestation, signed with a key the
    caller does not hold, of what the head actually is.

    **What it proves, and what it does not.** Identical limits to the room
    route, and worth restating rather than cross-referencing away: the signature
    proves the response came from this relay unaltered, not that the relay is
    honest. An operator with DB write access can rewrite a message, recompute a
    self-consistent chain forward from it, and sign the new head — recomputation
    catches inconsistency, not an internally consistent rewrite. Client-side
    head pinning is what catches that; this route is the half a client pins.

    `unchained_prefix` is the DM-specific caveat: messages written before P28-4
    have no `chain_hash` and are not covered by `chain_head`. A client treating
    a nonzero value as "verified" would be trusting exactly the region the relay
    is declining to vouch for.

    `chain_verified_scope` and `verified_message_count` carry P44's bounded walk,
    with the same guarantees the room route's docstring spells out: the signed
    payload is unchanged by the cap, and a client wanting the full history
    recomputes it itself against a pinned head.
    """
    _dm_thread_or_404(thread_id, rs._dashboard_agent_id(request) or "")
    result = await run_in_threadpool(_verify_dm_chain, thread_id, rs.CHAIN_VERIFY_LIMIT)
    signature = sign_raw_relay(federation_signable_bytes(
        rs.RELAY_BASE_URL, "dm_chain",
        [{
            "thread_id": thread_id,
            "chain_head": result.head or "",
            "message_count": result.message_count,
        }],
    ))
    return {
        "thread_id": thread_id,
        "verified": result.ok,
        "message": result.message,
        "chain_head": result.head,
        "message_count": result.message_count,
        "unchained_prefix": result.unchained_prefix,
        "verified_message_count": result.verified_count,
        "chain_verified_scope": result.scope,
        "origin_relay": rs.RELAY_BASE_URL,
        "origin_public_key_hex": get_relay_public_key_hex(),
        "signature": signature,
    }


@router.get("/dm/{thread_id}")
async def dm_thread_info(thread_id: str, request: Request):
    """Thread metadata — participants and whether it is encrypted.

    Exists so a client holding only a `thread_id` (from an inbox event, a stored
    reference) can learn the mode before it sends or displays anything, without
    fetching the message history to infer it.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    thread = _dm_thread_or_404(thread_id, caller)
    return {
        "thread_id": thread["thread_id"],
        "agent_a": thread["agent_a"],
        "agent_b": thread["agent_b"],
        "encrypted": bool(thread["encrypted"]),
        "created_at": thread["created_at"],
        "last_message_at": thread["last_message_at"],
        # P185: in a thread with another relay's agent, both are named by
        # address — the label a client seals, signs and opens under.
        "federated": bool(thread["remote_address"]),
        "my_label": _my_label(thread, caller),
    }


@router.post("/dm/{thread_id}/encrypt")
async def dm_encrypt(thread_id: str, request: Request):
    """Turn on end-to-end encryption for an existing thread. Idempotent.

    **One-way, on purpose.** Either participant may enable it unilaterally — there
    is nothing to negotiate, since the peer's client handles both modes and the
    flag only ever adds protection. There is deliberately no route that clears it:
    a downgrade endpoint is a force-plaintext attack, and one participant being
    able to strip a thread's encryption is exactly what an attacker with a stolen
    session would reach for. The only way back to plaintext is a different thread.

    Messages already stored stay as they are — an upgrade is not a purge. Old
    plaintext remains readable; everything after this point must be an envelope.
    """
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    thread = _dm_thread_or_404(thread_id, caller)
    rs._require_encryption_keys_published([thread["agent_a"], thread["agent_b"]])
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE dm_threads SET encrypted=1 WHERE thread_id=?", (thread_id,))
    return {"thread_id": thread_id, "encrypted": True}


@router.post("/dm/{thread_id}/send")
async def dm_send(thread_id: str, req: DMSendRequest, request: Request):
    """Send a message in a DM thread. Pushes inbox event to the recipient."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    # P15: thread lookup + participant check hoisted above the guardrails scan. They
    # used to sit in the write block below, so any registered agent could force a scan —
    # including the optional paid NIM layer — against a thread_id they have nothing to
    # do with. Bounded by _enforce_agent_rate_limit() above, so this is "stop doing
    # expensive work for a request that's already rejected", not a live DoS fix. Moving
    # only the *signature* check up (considered at P14) would not have helped: signing
    # is free for the attacker, so it discriminates nothing. Read-only, so it takes its
    # own connection rather than holding DB_LOCK across the scan; the resulting TOCTOU
    # window is immaterial (threads are never deleted).
    thread = _dm_thread_or_404(thread_id, caller)
    # Local import + resolved-once pattern matches P12-3's room-message fix.
    from emporia.wire import ERR_DUPLICATE_NONCE, DEFAULT_MSG_TYPE, signed_dm_thread_payload
    effective_msg_type = req.msg_type or DEFAULT_MSG_TYPE
    rs._require_valid_msg_type(effective_msg_type)
    rs._require_content_within_limit(req.content)
    # P16: enforce the thread's mode. The relay reads no plaintext and holds no key
    # here — `validate_envelope_for()` inspects the envelope's structure and the set
    # of agent_ids the content key was wrapped to, nothing more. It catches the two
    # failures that actually happen: prose posted to an encrypted thread (a silent
    # plaintext leak into a store the sender believes is opaque), and a sender who
    # wrapped only for themselves — a message the peer can never read, which without
    # this check would be stored, 200'd, and discovered as permanent garbage. The
    # mode comes from the thread row, never from the request, so there is nothing
    # here for a client to lie about.
    from emporia import crypto_box
    if thread["encrypted"]:
        # P17-1: the AAD binds msg_type, and this handler *resolves* msg_type just
        # above (`req.msg_type or DEFAULT_MSG_TYPE`). So a client that omits the
        # field seals under "" while this row stores "chat" — the recipient then
        # computes different AAD and the message is permanently unreadable, having
        # been accepted with a 200. Verified before the fix: row stored
        # msg_type='chat', open_sealed() raised for the recipient.
        #
        # Refusing is the only sound option: the relay cannot know what value the
        # client sealed under, so it can't repair the mismatch. Dropping msg_type
        # from the AAD was considered and rejected — it would throw away
        # relabelling protection (which rooms will reuse when they adopt
        # crypto_box) to fix something a clear 400 fixes outright. Same class as
        # validate_envelope_for() below, and deliberately sitting next to it: both
        # turn "accepted but nobody can ever read it" into an actionable error.
        # Plaintext threads keep the omit path — no AAD, nothing to disagree about.
        if not req.msg_type:
            raise HTTPException(
                400,
                "msg_type must be sent explicitly on an end-to-end encrypted thread: "
                "it is bound into the message's AAD, and the relay's own default "
                f"resolution (to '{DEFAULT_MSG_TYPE}') would not match what you "
                "sealed under, making the message permanently unreadable",
            )
        try:
            crypto_box.validate_envelope_for(
                req.content, {thread["agent_a"], thread["agent_b"]},
            )
        except ValueError as e:
            # P20-5: audited whether this should split MissingRecipients into a
            # 409 the way the room route does (A10) and concluded it shouldn't —
            # a DM thread's recipient set is exactly {agent_a, agent_b}, fixed at
            # creation, with no join path and so no membership race to recover
            # from. A missing-recipient failure here is always the same client
            # bug the room route calls out as the *other* case: wrapped only for
            # self. 400 is correct and test_encrypted_message_must_be_readable_by_
            # every_participant pins it.
            raise HTTPException(400, f"this DM thread is end-to-end encrypted — {e}")
    elif crypto_box.looks_like_envelope(req.content):
        raise HTTPException(
            400,
            "this DM thread is not encrypted, so an encrypted envelope cannot be "
            f"stored in it — enable encryption first via POST /dm/{thread_id}/encrypt",
        )

    if not req.signature:
        raise HTTPException(401, "Signature required")
    # P18-2: no silent fallback — an absent nonce means a message bound to nothing
    # unique, which is exactly the replay hole this closes. Checked here rather than
    # earlier so a missing *signature* still answers 401 first: that is the more
    # fundamental failure, and its status code predates this field.
    if not req.nonce:
        raise HTTPException(
            400,
            "nonce is required — it binds this message's signature and ciphertext to "
            "this message, so a stored row cannot be replayed as another. Generate "
            "one with wire.new_message_nonce().",
        )
    # P19 (M3): "non-empty" was the entire validation. `wire.new_message_nonce()`
    # produces 32 hex chars, but nothing enforced clients use it — a nonce sat
    # outside `_require_content_within_limit`'s reach, so an ~800KB nonce was
    # accepted, stored, *indexed* in idx_dm_messages_nonce, and echoed back on
    # every read. Bounded to what an honest nonce actually looks like, generously
    # (32-64 hex chars covers 128-256 bits of a future longer nonce without
    # forcing a re-tune).
    # P20-5 (A9): moved ahead of the guardrails scan below, same reasoning as the
    # room route — a cheap regex shouldn't run after the expensive (paid NIM) scan.
    if not rs._NONCE_RE.fullmatch(req.nonce):
        raise HTTPException(
            400, "nonce must be 32-64 hex characters (see wire.new_message_nonce())",
        )

    # Same N3-8/R7 shape as room messages: bound to thread_id + msg_type, not just
    # content — a content-only signature was valid for delivery into any thread this
    # agent is a participant of, forever (replayable across threads and across
    # msg_type).
    # P21-2 (B3): moved ahead of the guardrails scan below — Ed25519 verification is
    # microseconds; the scan is a paid NIM call. A9 hoisted the cheap nonce-shape
    # check over the scan and stopped one statement short of this one, so a
    # registered participant sending a garbage signature still burned scan budget
    # on every plaintext-thread request until now.
    signed_envelope = signed_dm_thread_payload(
        thread_id, req.content, effective_msg_type, req.nonce,
    )
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed_envelope, req.signature):
        raise HTTPException(403, "Invalid message signature")

    # P15: msg_type dropped from this payload for the same reason as the room route
    # above — the closed enum checked immediately above supersedes scanning it.
    # P16: and the content scan is skipped entirely on an encrypted thread, the same
    # carve-out (and the same reason) encrypted rooms already have — ciphertext is
    # base64 and *will* false-positive on injection patterns. This is the real price
    # of E2E, stated plainly in docs/security-log.md rather than implied away: the relay
    # cannot scan what it cannot read. msg_type is unaffected — it sits outside the
    # envelope and is already restricted to a closed six-value enum.
    if not thread["encrypted"]:
        try:
            await rs._assert_payload_safe_counted({"content": req.content})
        except PermissionError as e:
            raise HTTPException(403, str(e))
    # P185: in a thread with another relay's agent the sender is named by its
    # address, and the message goes to that relay first — stored here only once
    # it was accepted there, so the two copies never disagree.
    sender_label = _my_label(thread, caller)
    if thread["remote_address"]:
        await _deliver_remote(thread, caller, sender_label, req, effective_msg_type)
    now = datetime.now(UTC).isoformat()
    message_id = f"dmm_{uuid.uuid4().hex[:12]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        try:
            # P28-4: computed inside the same DB_LOCK as the INSERT, so two
            # concurrent sends can't both read the same predecessor and chain
            # onto it. The frame is rooms.compute_chain_hash() itself rather
            # than a copy — P21-7 made it public for exactly this reason.
            chain_hash = rooms_db.compute_chain_hash(
                _dm_last_chain_hash(conn, thread_id), sender_label, req.content, now,
            )
            conn.execute(
                "INSERT INTO dm_messages (message_id, thread_id, sender_id, content, "
                "msg_type, created_at, signature, nonce, chain_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (message_id, thread_id, sender_label, req.content, effective_msg_type, now,
                 req.signature, req.nonce, chain_hash),
            )
        except sqlite3.IntegrityError as e:
            # P19 (H3): idx_dm_messages_nonce's UNIQUE(thread_id, nonce) is the
            # database-level half of the replay defense — the one that survives an
            # operator dropping the application check — but nothing caught the
            # constraint firing, and `relay_server.py` registers no
            # `@router.exception_handler`. So the primary case this feature exists for
            # (a captured request replayed verbatim) surfaced as an unhandled 500
            # with a traceback, not the 409 the client-facing contract implies.
            # Reachable by an honest client too: the nonce is free-form, so a
            # counter-based generator 500s on its very second message.
            # P21-3 (B4): same structured code as the room route's duplicate-nonce
            # 409 (wire.ERR_DUPLICATE_NONCE) -- no client branches on this one
            # today (send_dm doesn't retry on 409 at all), but there's no reason
            # to leave this the one hand-copied-string holdout of the shape B4
            # just closed everywhere else.
            raise HTTPException(409, {
                "code": ERR_DUPLICATE_NONCE,
                "message": "a message with this nonce already exists in this thread — "
                "this looks like a replayed request, not a new message",
            }) from e
        conn.execute(
            "UPDATE dm_threads SET last_message_at=? WHERE thread_id=?", (now, thread_id)
        )
        recipient = thread["agent_b"] if caller == thread["agent_a"] else thread["agent_a"]
    if thread["remote_address"]:
        return {"message_id": message_id, "thread_id": thread_id, "created_at": now,
                "delivered_to": thread["peer_url"]}
    event = {
        "type": "dm_received",
        "thread_id": thread_id,
        "message_id": message_id,
        "sender_id": caller,
        "content": req.content,
        # P14: was req.msg_type, which is "" on the omit path while the row above
        # stores effective_msg_type ("chat") — the same message reached its two
        # consumers (this inbox/WS event vs. GET /dm/{id}/messages) with different
        # values. The room route avoids this structurally by broadcasting the stored
        # message object; this one hand-builds its event, so both sites have to use
        # the resolved value explicitly.
        "msg_type": effective_msg_type,
        # P16: a client holding only this event has to know whether `content` is
        # ciphertext before it tries to display it. Read from the thread row, the
        # same source the mode check above used — not re-derived from the content,
        # which would be a second definition of the same fact.
        "encrypted": bool(thread["encrypted"]),
        "created_at": now,
    }
    await rs.broadcast_to_agent(recipient, event)
    return {"message_id": message_id, "thread_id": thread_id, "created_at": now}


@router.get("/dm")
async def dm_list(agent_id: str, request: Request, limit: int = 50):
    """List DM threads for an agent, most recent first."""
    rs._require_caller_is(agent_id, request)
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT t.*, "
            "(SELECT content FROM dm_messages WHERE thread_id=t.thread_id "
            " ORDER BY created_at DESC LIMIT 1) AS last_content, "
            "(SELECT sender_id FROM dm_messages WHERE thread_id=t.thread_id "
            " ORDER BY created_at DESC LIMIT 1) AS last_sender "
            "FROM dm_threads t WHERE t.agent_a=? OR t.agent_b=? OR t.local_agent=? "
            "ORDER BY t.last_message_at DESC LIMIT ?",
            (agent_id, agent_id, agent_id, limit),
        ).fetchall()
    return [
        {
            "thread_id": r["thread_id"],
            "other_agent": r["remote_address"] or (r["agent_b"] if r["agent_a"] == agent_id else r["agent_a"]),
            "federated": bool(r["remote_address"]),
            "last_message_at": r["last_message_at"],
            # P16: on an encrypted thread this preview is the raw envelope, which
            # is exactly right — the relay has nothing else to give. The `encrypted`
            # flag beside it is how a client knows to decrypt rather than display.
            "last_content": r["last_content"],
            "last_sender": r["last_sender"],
            "encrypted": bool(r["encrypted"]),
        }
        for r in rows
    ]


@router.get("/dm/{thread_id}/messages")
async def dm_messages(
    thread_id: str, agent_id: str, request: Request, limit: int = 100,
    before: str | None = None, before_seq: int | None = None,
):
    """Fetch messages in a DM thread. Agent must be a participant.

    P31-2: `before` alone is not a total cursor. Two messages can share a
    `created_at` — this send path computes it *before* taking DB_LOCK, so two
    concurrent sends genuinely can land on one microsecond, which is the same
    case P21-7 added the chain writer's `rowid` tiebreak for. When such a pair
    straddles a page boundary, `created_at < ?` excludes the older one from
    every subsequent page: it can never be fetched at all, and a client walking
    the chain backward sees an untampered thread as unverifiable.

    `before_seq` (each message's `seq`, its rowid) makes the cursor total,
    ordered exactly the way `_verify_dm_chain()` and `_dm_last_chain_hash()`
    already order: `(created_at, rowid)`. Optional, so a caller that doesn't
    paginate is unaffected.
    """
    rs._require_caller_is(agent_id, request)
    limit = rs._clamp_limit(limit)
    with rs.DB_LOCK, rs.get_db() as conn:
        thread = conn.execute(
            "SELECT * FROM dm_threads WHERE thread_id=?", (thread_id,)
        ).fetchone()
        if not thread:
            raise HTTPException(404, "DM thread not found")
        if agent_id not in (thread["agent_a"], thread["agent_b"]) and agent_id != thread["local_agent"]:
            raise HTTPException(403, "Not a participant in this thread")
        if before and before_seq is not None:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM dm_messages WHERE thread_id=? AND "
                "(created_at < ? OR (created_at = ? AND rowid < ?)) "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, before, before, before_seq, limit),
            ).fetchall()
        elif before:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM dm_messages WHERE thread_id=? AND created_at < ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM dm_messages WHERE thread_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (thread_id, limit),
            ).fetchall()
    messages = [
        {
            "message_id": r["message_id"],
            "sender_id": r["sender_id"],
            "content": r["content"],
            "msg_type": r["msg_type"],
            "created_at": r["created_at"],
            # P17-2: served so the *client* can re-verify, not just the relay at
            # write time. NULL for pre-P17 rows, which clients surface as "unknown".
            "signature": r["signature"],
            # P18-2: served so the client can spot a duplicate itself — the UNIQUE
            # index stops the easy replay, but an operator who can write the DB can
            # also drop an index, and this is what keeps that visible.
            "nonce": r["nonce"],
            # P30-1: same reason as the two above, and the one that makes
            # independent chain verification possible at all — without it a
            # client has nothing to compare its own recomputation against, and
            # P28-4's chain could only ever be checked by the relay redoing its
            # own work, which is precisely the party the check exists to doubt.
            # NULL on pre-P28 rows: nothing was stored, so nothing is claimed.
            "chain_hash": r["chain_hash"],
            # P31-2: the second half of this thread's total ordering, so a
            # paginating client can build a cursor that can't skip a message
            # sharing a timestamp with the page boundary. See `before_seq`.
            "seq": r["seq"],
            # P192: when its text was removed; the record of who did is below.
            "deleted_at": r["deleted_at"],
        }
        for r in reversed(rows)
    ]
    gone = {f"{thread_id}/{m['nonce']}": m for m in messages if m.get("deleted_at")}
    if gone:
        from emporia import deletion
        with rs.DB_LOCK, rs.get_db() as conn:
            for item, removal in deletion.removals_for(conn, "dm_message", list(gone)).items():
                gone[item]["removed"] = removal
    return {"thread_id": thread_id, "messages": messages, "my_label": _my_label(thread, agent_id)}


# ============================================================================
