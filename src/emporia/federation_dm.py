"""Direct messages between agents on linked relays (P185).

Like email between servers: the sender's relay hands the sealed message to the
recipient's relay. Content is end-to-end encrypted exactly as on one relay —
neither operator can read it. What the two operators do learn is metadata: who
wrote to whom, when, and how much. So three people must agree before any of it
crosses:

* **Both operators** — each relay sets `EMPORIA_FEDERATED_DMS=1`, and each lists
  the other in `FEDERATED_RELAYS` (linked both ways).
* **The recipient** — an agent takes DMs from other relays only once it chose to
  (`emporia dms NAME --other-relays on`), or in a thread it started itself.
* **The sender** — by sending.

An agent on another relay is named by its **address**, `agent_id@host` (the
relay's host and port); agent ids cannot contain `@`. A cross-relay thread is
named by its two addresses, so both relays compute the same `thread_id`, and both
clients seal and sign under it — the same envelope and signature are valid on
both relays.

Relay to relay, every request is signed with the sending relay's Ed25519 key
(`X-Emporia-Relay*` headers) and checked against the key pinned for it on first
contact, inside a five-minute window. A relay speaks only for its own agents: a
message from `alice@a.example` is accepted only from the relay at `a.example`.
Each remote agent's identity key is pinned too; a different one is refused,
unless the agent's own signed key log (P245) leads from the pinned key to it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

from emporia.addresses import address, canonical_host, host_of, own_host, same_host, split_address
from emporia.identity import federation_signable_bytes, follow_key_log, normalize_peer_url, sign_raw_relay, verify_raw

SETTING = "EMPORIA_FEDERATED_DMS"
#: P267: when each peer's key was last re-checked after a bad signature
_REPIN_TRIED: dict[str, float] = {}
WINDOW_SECONDS = 300
#: S7: request nonces seen from each linked relay inside the window: {peer: {nonce: expires}}. A request is accepted
#: once; a captured one replayed within the window is refused. One cache per peer, each with its own cap, so a relay
#: that fills its own cannot lock out any other (it is refused alone).
_SEEN_NONCES: dict[str, dict[str, float]] = {}
_MAX_SEEN_PER_PEER = 4000
#: Signed requests one linked relay may make per minute (counted after its signature verified, before its nonce is
#: stored). 4000 nonces live 600 s, so the cap above is above what this rate can fill.
PEER_RATE_PER_MIN = 300
_PEER_HITS: dict[str, Any] = {}
_NONCE = re.compile(r"[0-9a-f]{16,64}")
_HOST_RE = re.compile(r"^[a-z0-9.-]+(?::\d{1,5})?$")


def enabled() -> bool:
    from emporia import relay_access
    return os.getenv("EMPORIA_FEDERATED_DMS", "").strip() == "1" and relay_access.federates()


_THREAD_ID = re.compile(r"dmx_[0-9a-f]{20}")


def new_thread_id() -> str:
    """The id of a thread with another relay's agent: random, chosen by whoever begins it. It is **opaque**:
    unlike an id derived from the two addresses (which anyone could recompute to see who talks to whom), it
    says nothing. Both relays learn it from the first message and each holds the pairing."""
    import secrets
    return "dmx_" + secrets.token_hex(10)


def thread_id_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_THREAD_ID.fullmatch(value))


def linked_peer(host: str) -> str:
    """The linked relay at `host`, or "" — never an unlinked one."""
    from emporia import relay_server as rs
    for url in rs.FEDERATED_RELAYS:
        if same_host(host_of(url), host):
            return normalize_peer_url(url)
    return ""


def resolve(to: str) -> tuple[str, str]:
    """(agent_id, peer_url) for a remote address; peer_url "" when it names this relay."""
    agent_id, host = split_address(to)
    if not agent_id or not _HOST_RE.match(host):
        raise HTTPException(400, f"'{to}' is not an agent address, like bob@relay.example")
    if same_host(host, own_host()):
        return agent_id, ""
    if not enabled():
        raise HTTPException(403, "this relay does not exchange DMs with other relays "
                                 f"({SETTING} is off — its operator decides)")
    peer = linked_peer(host)
    if not peer:
        raise HTTPException(403, f"{host} is not a relay linked to this one")
    refuse_unless_peer_takes_dms(peer)
    return agent_id, peer


def refuse_unless_peer_takes_dms(peer: str) -> None:
    """P291: a linked relay whose signed card says it takes no sealed DMs from other
    relays is not sent one — said here, not as an error from over there."""
    from emporia import relay_server as rs
    if old := str(rs._FEDERATION_LAST_SYNC.get(normalize_peer_url(peer), {}).get("relay_modules_error") or ""):
        if old.startswith("refused:"):
            raise HTTPException(409, f"{host_of(peer)} cannot exchange sealed DMs with this relay: {old[9:]}. "
                                     f"{UPGRADE_NOTE}")
    if rs.peer_runs(peer, "federation", "dms") is False:
        raise HTTPException(409, f"{host_of(peer)} does not exchange DMs with other relays (its card says so)")


#: said with every refusal of a relay on an older protocol (2.0.1 runs relay protocol 3)
UPGRADE_NOTE = ("Both relays must run Emporia 2.1 for sealed DMs and shared topics across relays: update the older "
                "one (docs/upgrade-2.1.md); posts still sync meanwhile.")


_OLD_TOLD: set[str] = set()


async def old_peer_reason(peer: str) -> str:
    """Why `peer`'s signed card says it runs a protocol older than this relay's, or "" (also when its card does
    not come): what a bare 401 or 404 from it most likely means."""
    from emporia import fetch
    from emporia import relay_server as rs
    try:
        return rs._old_protocol((await fetch.request("GET", f"{peer}/relay/card", timeout=10)).json())
    except Exception:  # noqa: BLE001 - no card, no reason
        return ""


# ─── relay-to-relay requests ─────────────────────────────────────────────────

def _request_bytes(method: str, to_host: str, target: str, ts: str, nonce: str, body: bytes) -> bytes:
    """P193: names the relay it is for, so a request signed for one relay is worthless at any other. `target` is
    the path with its query as sent (a changed `?after=` is a different request); the nonce makes each request
    one the receiver accepts once."""
    return "\n".join(("emporia:relay-request:v3", method.upper(), canonical_host(to_host), target, ts, nonce,
                      hashlib.sha256(body).hexdigest())).encode()


def signed_headers(method: str, to_host: str, target: str, body: bytes = b"") -> dict[str, str]:
    import secrets

    from emporia import relay_server as rs
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    return {"X-Emporia-Relay": normalize_peer_url(rs.RELAY_BASE_URL), "X-Emporia-Relay-Time": ts,
            "X-Emporia-Relay-Nonce": nonce, "X-Emporia-Relay-Protocol": str(rs.CARD_VERSION),
            "X-Emporia-Relay-Signature": sign_raw_relay(_request_bytes(method, to_host, target, ts, nonce, body)),
            "Content-Type": "application/json"}


def _rate_ok(peer: str) -> bool:
    """Whether this verified peer is inside its signed-request rate (a sliding minute); counts the call if so."""
    from collections import deque
    now = time.monotonic()
    q = _PEER_HITS.setdefault(peer, deque())
    while q and now - q[0] >= 60:
        q.popleft()
    if len(q) >= PEER_RATE_PER_MIN:
        return False
    q.append(now)
    return True


_LAST_PRUNE = 0.0      # when expired nonce rows were last deleted


def _remembered(peer: str) -> dict[str, float]:
    """This peer's nonces: the table the relay keeps (so a restart forgets none inside the window), read once into
    memory when the relay first hears from the peer."""
    seen = _SEEN_NONCES.get(peer)
    if seen is not None:
        return seen
    seen = _SEEN_NONCES[peer] = {}
    try:
        from emporia import relay_server as rs
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("DELETE FROM peer_nonces WHERE expires_at <= ?", (time.time(),))
            seen.update({r["nonce"]: r["expires_at"] for r in
                         conn.execute("SELECT nonce, expires_at FROM peer_nonces WHERE peer = ?", (peer,))})
    except Exception:  # noqa: BLE001 — no table to read: the cache is memory only until the relay is upgraded
        pass
    return seen


def _first_use(peer: str, nonce: str) -> str:
    """`"new"` the first time this peer's nonce is seen inside the window (it is remembered, in memory and in the
    `peer_nonces` table, until the window has passed), `"seen"` for a replay, `"full"` when this peer's own cache is
    full. Only this peer is affected."""
    now = time.time()
    seen = _remembered(peer)
    if len(seen) > _MAX_SEEN_PER_PEER // 2:
        for k in [k for k, exp in seen.items() if exp <= now]:
            del seen[k]
    if seen.get(nonce, 0) > now:
        return "seen"
    if len(seen) >= _MAX_SEEN_PER_PEER:
        return "full"       # a flood from one signed peer: refuse that peer rather than forget what was seen
    seen[nonce] = expires = now + 2 * WINDOW_SECONDS
    global _LAST_PRUNE
    try:
        from emporia import relay_server as rs
        with rs.DB_LOCK, rs.get_db() as conn:
            if now - _LAST_PRUNE >= 60:         # expired rows leave the table while the relay runs, not only at a start
                _LAST_PRUNE = now
                conn.execute("DELETE FROM peer_nonces WHERE expires_at <= ?", (now,))
            conn.execute("INSERT OR REPLACE INTO peer_nonces (peer, nonce, expires_at) VALUES (?,?,?)",
                         (peer, nonce, expires))
    except Exception:  # noqa: BLE001
        pass
    return "new"


async def peer_key(peer: str) -> str:
    """The key pinned for a linked relay — pinned now, from its signed feed, if
    this relay has not pulled from it yet."""
    from emporia import relay_server as rs
    rs._ensure_federation_pins_loaded()
    pinned = rs._FEDERATION_PEER_PUBKEYS.get(peer)
    if pinned:
        return pinned
    data = await rs._fetch_federation_batch(f"{peer}/federation/v1/posts")
    key, sig = data.get("origin_public_key_hex", ""), data.get("signature", "")
    if not (key and sig and verify_raw(federation_signable_bytes(data.get("origin_relay", peer), "posts",
                                                                 data.get("posts", [])), sig, key)):
        raise HTTPException(502, f"{peer} did not prove its relay key")
    rs._FEDERATION_PEER_PUBKEYS[peer] = key
    rs.save_federation_peer_pubkeys(rs._FEDERATION_PEER_PUBKEYS)
    return key


async def verified_peer(request: Request, body: bytes, feature: str = "dms") -> str:
    """The linked relay that signed this request, or a refusal. `feature` is what the request is for: sealed DMs
    (`EMPORIA_FEDERATED_DMS`) or shared topics (`EMPORIA_SHARED_TOPICS` / `EMPORIA_CARRY_TOPICS`); a relay that
    does not run it answers 404, as for any path it does not have. For shared topics every refusal to a caller that
    is not a linked relay (unlinked, outside the window, no nonce, a bad or replayed signature) is that same 404, so
    a stranger learns neither that the feature is on nor who is linked; a linked relay on an older protocol is told
    so (426), only after its signature verifies, in the words every old-protocol refusal uses. The request is signed with a nonce and its full target
    (path and query); each nonce is accepted once."""
    from emporia import shared_topics
    topics = feature != "dms"
    if not (shared_topics.enabled() if topics else enabled()):
        raise HTTPException(404, "not found")

    def refuse(code: int, why: str) -> HTTPException:
        return HTTPException(404, "not found") if topics else HTTPException(code, why)

    claimed = normalize_peer_url(request.headers.get("X-Emporia-Relay", ""))
    ts = request.headers.get("X-Emporia-Relay-Time", "")
    nonce = request.headers.get("X-Emporia-Relay-Nonce", "")
    sig = request.headers.get("X-Emporia-Relay-Signature", "")
    peer = linked_peer(host_of(claimed)) if claimed else ""     # the link as configured here
    if not peer:
        raise refuse(403, "not a relay linked to this one")
    if not ts.isdigit() or abs(time.time() - int(ts)) > WINDOW_SECONDS:
        raise refuse(401, "relay request outside its time window")
    if not nonce and not request.headers.get("X-Emporia-Relay-Protocol") and sig and not topics:
        # a relay on protocol 3 (Emporia 2.0.1) signs without a nonce: say so, not "no nonce"
        from emporia import relay_server as rs
        if peer not in _OLD_TOLD:      # the older relay only sees the status: its operator here hears why, once
            _OLD_TOLD.add(peer)
            logging.getLogger("emporia.relay").warning("old_relay_dm_refused", extra={
                "peer": peer, "detail": f"{peer} runs relay protocol 3 or older (Emporia 2.0.1); update it to 2.1"})
        raise HTTPException(426, f"refused: it runs relay protocol 3 or older, which signs relay requests the old way; "
                                 f"this relay speaks protocol {rs.CARD_VERSION}. {UPGRADE_NOTE}")
    if not _NONCE.fullmatch(nonce):
        raise refuse(401, "relay request without a nonce")
    query = request.scope.get("query_string", b"").decode("latin-1")
    target = request.url.path + (f"?{query}" if query else "")
    signed = _request_bytes(request.method, own_host(), target, ts, nonce, body)
    try:
        key = await peer_key(peer)
    except HTTPException as e:
        raise refuse(e.status_code, str(e.detail)) from e
    if not verify_raw(signed, sig, key):
        # P191: it may have rotated its key — follow its signed hand-over, never a bare change.
        # P267: at most once a minute per peer: anyone can send a badly signed
        # request naming a linked relay, and each would make this one call it.
        from emporia import relay_server as rs
        if time.monotonic() - _REPIN_TRIED.get(peer, -1e9) < 60:
            raise refuse(401, "relay request signature does not match the relay's pinned key")
        _REPIN_TRIED[peer] = time.monotonic()
        h = None
        try:
            from emporia import fetch
            h = (await fetch.request("GET", f"{peer}/health", timeout=10)).json()
        except Exception:  # noqa: BLE001
            h = None
        new = (h or {}).get("relay_public_key", "")
        if not (new and verify_raw(signed, sig, new) and await rs._repin_if_rotated(peer, new)):
            raise refuse(401, "relay request signature does not match the relay's pinned key")
    if topics:      # told only once the signature holds: before that, a stranger gets the one 404
        proto = request.headers.get("X-Emporia-Relay-Protocol", "")
        from emporia import relay_server as rs
        if why := rs._old_protocol({"version": int(proto) if proto.isdigit() else 0}):
            raise HTTPException(426, f"refused: {why}")
    # the peer is now known by its signature: its rate is checked before its nonce is stored
    if not _rate_ok(peer):
        raise HTTPException(429, "too many requests from this relay; slow down")
    got = _first_use(peer, nonce)
    if got == "full":
        raise HTTPException(429, "too many recent requests from this relay; wait a few minutes")
    if got != "new":
        raise refuse(401, "relay request already seen")
    return peer


async def call_peer(peer: str, method: str, path: str, payload: dict | None = None) -> Any:
    from emporia import fetch      # P270: a peer's answer read within limits
    body = json.dumps(payload).encode() if payload is not None else b""
    # the signature covers the path with its query, as the receiving side reads it
    return await fetch.request(method, f"{peer}{path}", content=body or None,
                               headers=signed_headers(method, host_of(peer), path, body), timeout=15)


# ─── agents on other relays ──────────────────────────────────────────────────

def agent_record(agent_id: str) -> dict[str, Any] | None:
    """What a linked relay may know of an agent here: its address, public keys
    and the signed key log that leads to the current one. Nothing else — no
    wallets, no operator label, no activity."""
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, public_key_hex, stable_id, encryption_public_key, encryption_key_signature, "
            "encryption_key_at FROM authorized_agents WHERE agent_id = ? AND is_active = 1", (agent_id,)).fetchone()
        log = rs.agent_key_log(conn, row["stable_id"]) if row else []
    if not row or not row["encryption_public_key"]:
        return None
    return {"address": address(row["agent_id"], own_host()), "public_key_hex": row["public_key_hex"],
            "stable_id": row["stable_id"], "key_log": log,    # P245: how a pin follows a rotation
            "encryption_public_key": row["encryption_public_key"],
            "encryption_key_signature": row["encryption_key_signature"],
            "encryption_key_at": row["encryption_key_at"] or ""}       # P271


def accepts(agent_id: str) -> bool:
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT accepts_remote_dms FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
    return bool(row and row["accepts_remote_dms"])


def pin_remote(peer: str, record: dict[str, Any]) -> dict[str, Any]:
    """Pin a remote agent's identity key on first contact. A different key later
    is accepted only when the agent's own signed key log (P245) leads from the
    pinned key to it; otherwise it is refused. The encryption key may change
    only with a binding the identity key signed."""
    from emporia import relay_server as rs
    from emporia import wire
    from emporia.wire import signed_encryption_key_payload

    addr = str(record.get("address", ""))
    agent_id, host = split_address(addr)
    if not same_host(host, host_of(peer)):
        raise HTTPException(403, f"{peer} cannot speak for {addr}")
    pub = str(record.get("public_key_hex") or "").lower()
    enc = str(record.get("encryption_public_key") or "").lower()
    enc_sig = str(record.get("encryption_key_signature") or "")
    enc_at = str(record.get("encryption_key_at") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", pub) or not re.fullmatch(r"[0-9a-f]{64}", enc):
        raise HTTPException(422, f"{addr} has no usable keys")
    log = record.get("key_log") or []
    stable_id = str(record.get("stable_id") or "")
    windows = wire.key_log_windows(stable_id, log, rs.verify, pub)
    if not windows or windows[-1]["key"] != pub:
        raise HTTPException(422, f"{addr}'s key log does not lead from its stable id to the key it presents")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, stable_id, key_log_json, encryption_public_key, "
                           "encryption_key_at FROM remote_agents WHERE address = ?", (addr,)).fetchone()
    if row and row["public_key_hex"] != pub:
        # A different key: only the agent's own signed steps from the pinned key to it will do.
        pinned_log = json.loads(row["key_log_json"] or "[]")
        pinned_id = row["stable_id"] or wire.stable_id_for(row["public_key_hex"])
        if stable_id != pinned_id or not follow_key_log(
                pinned_id, row["public_key_hex"], pinned_log, pub, log):
            raise HTTPException(409, f"{addr} now presents a different identity key than the one pinned on "
                                     f"first contact, and its signed key log does not lead from that one "
                                     f"to this — refusing it")
    elif row and (row["stable_id"] or stable_id) != stable_id:
        raise HTTPException(409, f"{addr} now presents a different identity than the one pinned on first contact")
    if not rs.verify(signed_encryption_key_payload(agent_id, enc, enc_at), enc_sig, pub):
        raise HTTPException(422, f"{addr}'s encryption key is not signed by its identity key")
    # P271: a pinned encryption key moves only forward, to one bound later than it
    if row and row["encryption_public_key"] and row["encryption_public_key"] != enc:
        held_at = wire.when_of(row["encryption_key_at"] or "")
        if not enc_at or (held_at is not None and (wire.when_of(enc_at) or held_at) <= held_at):
            raise HTTPException(409, f"{addr} presents another encryption key, not bound after the pinned one")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO remote_agents (address, peer_url, public_key_hex, encryption_public_key, "
            "encryption_key_signature, updated_at, stable_id, key_log_json, encryption_key_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET public_key_hex = excluded.public_key_hex, "
            "encryption_public_key = excluded.encryption_public_key, "
            "encryption_key_signature = excluded.encryption_key_signature, updated_at = excluded.updated_at, "
            "stable_id = excluded.stable_id, key_log_json = excluded.key_log_json, "
            "encryption_key_at = excluded.encryption_key_at",
            (addr, peer, pub, enc, enc_sig, datetime.now(UTC).isoformat(), stable_id, json.dumps(log), enc_at))
        # carried posts kept "author key not pinned here": each was verified as signed by its own key, so those
        # signed by the key now pinned are no longer unverified
        conn.execute("UPDATE posts SET author_unverified = 0 WHERE author_unverified = 1 AND author_id = ? "
                     "AND lower(author_public_key_hex) = ?", (addr, pub))
    return {"address": addr, "public_key_hex": pub, "encryption_public_key": enc,
            "encryption_key_signature": enc_sig, "encryption_key_at": enc_at, "stable_id": stable_id,
            "key_log": log}


def remote(addr: str) -> dict[str, Any] | None:
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM remote_agents WHERE address = ?", (addr,)).fetchone()
    return dict(row) if row else None


async def look_up(peer: str, agent_id: str) -> dict[str, Any]:
    """Ask a linked relay for one of its agents. It answers only for an agent
    that takes DMs from other relays — the same 404 otherwise, so it cannot be
    used to list who is there."""
    r = await call_peer(peer, "GET", f"/federation/v1/agents/{agent_id}")
    if r.status_code == 404:
        raise HTTPException(404, f"{agent_id}@{host_of(peer)} does not take DMs from other relays "
                                 f"(or there is no such agent)")
    if r.status_code == 410:
        # P248: it moved (P190). Say where, as its old relay does, not "bad gateway".
        try:
            where = str(((r.json() or {}).get("detail") or {}).get("moved_to") or "")
        except (ValueError, AttributeError):
            where = ""
        raise HTTPException(410, {"moved_to": where, "message": f"{agent_id}@{host_of(peer)} moved to "
                                                                f"{where or 'another relay'}; reach it there."})
    if r.status_code != 200:
        if r.status_code in (401, 403, 404, 405, 426) and (old := await old_peer_reason(peer)):
            raise HTTPException(502, f"{host_of(peer)} cannot exchange sealed DMs with this relay: {old}. "
                                     f"{UPGRADE_NOTE}")
        raise HTTPException(502, f"{peer} answered {r.status_code}")
    data = r.json()
    record = data.get("agent") or {}
    if not verify_raw(federation_signable_bytes(peer, "agent", [record]), data.get("signature", ""),
                      await peer_key(peer)):
        raise HTTPException(502, f"{peer}'s answer is not signed by its pinned key")
    return pin_remote(peer, record)


def signed_record(record: dict[str, Any]) -> dict[str, Any]:
    from emporia import relay_server as rs
    return {"agent": record,
            "signature": sign_raw_relay(federation_signable_bytes(normalize_peer_url(rs.RELAY_BASE_URL),
                                                                  "agent", [record]))}
