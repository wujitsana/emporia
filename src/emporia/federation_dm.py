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
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

from emporia.addresses import address, canonical_host, host_of, own_host, same_host, split_address
from emporia.identity import federation_signable_bytes, follow_key_log, normalize_peer_url, sign_raw_relay, verify_raw

SETTING = "EMPORIA_FEDERATED_DMS"
WINDOW_SECONDS = 300
_HOST_RE = re.compile(r"^[a-z0-9.-]+(?::\d{1,5})?$")


def enabled() -> bool:
    from emporia import relay_access
    return os.getenv("EMPORIA_FEDERATED_DMS", "").strip() == "1" and relay_access.federates()


def thread_id_for(a: str, b: str) -> str:
    """The same id on both relays: it is named by the two addresses."""
    first, second = sorted((a, b))
    return "dmx_" + hashlib.sha256(f"{first}|{second}".encode()).hexdigest()[:20]


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
    return agent_id, peer


# ─── relay-to-relay requests ─────────────────────────────────────────────────

def _request_bytes(method: str, to_host: str, path: str, ts: str, body: bytes) -> bytes:
    """P193: names the relay it is for, so a request signed for one relay is
    worthless at any other."""
    return "\n".join(("emporia:relay-request:v2", method.upper(), canonical_host(to_host), path, ts,
                      hashlib.sha256(body).hexdigest())).encode()


def signed_headers(method: str, to_host: str, path: str, body: bytes = b"") -> dict[str, str]:
    from emporia import relay_server as rs
    ts = str(int(time.time()))
    return {"X-Emporia-Relay": normalize_peer_url(rs.RELAY_BASE_URL), "X-Emporia-Relay-Time": ts,
            "X-Emporia-Relay-Signature": sign_raw_relay(_request_bytes(method, to_host, path, ts, body)),
            "Content-Type": "application/json"}


async def peer_key(peer: str) -> str:
    """The key pinned for a linked relay — pinned now, from its signed feed, if
    this relay has not pulled from it yet."""
    from emporia import relay_server as rs
    rs._ensure_federation_pins_loaded()
    pinned = rs._FEDERATION_PEER_PUBKEYS.get(peer)
    if pinned:
        return pinned
    data = await rs._fetch_federation_batch(f"{peer}/gaming/v1/federate/listings")
    key, sig = data.get("origin_public_key_hex", ""), data.get("signature", "")
    if not (key and sig and verify_raw(federation_signable_bytes(data.get("origin_relay", peer), "listings",
                                                                 data.get("listings", [])), sig, key)):
        raise HTTPException(502, f"{peer} did not prove its relay key")
    rs._FEDERATION_PEER_PUBKEYS[peer] = key
    rs.save_federation_peer_pubkeys(rs._FEDERATION_PEER_PUBKEYS)
    return key


async def verified_peer(request: Request, body: bytes) -> str:
    """The linked relay that signed this request, or a refusal."""
    if not enabled():
        raise HTTPException(404, "not found")
    claimed = normalize_peer_url(request.headers.get("X-Emporia-Relay", ""))
    ts = request.headers.get("X-Emporia-Relay-Time", "")
    sig = request.headers.get("X-Emporia-Relay-Signature", "")
    peer = linked_peer(host_of(claimed)) if claimed else ""     # the link as configured here
    if not peer:
        raise HTTPException(403, "not a relay linked to this one")
    if not ts.isdigit() or abs(time.time() - int(ts)) > WINDOW_SECONDS:
        raise HTTPException(401, "relay request outside its time window")
    signed = _request_bytes(request.method, own_host(), request.url.path, ts, body)
    if not verify_raw(signed, sig, await peer_key(peer)):
        # P191: it may have rotated its key — follow its signed hand-over, never a bare change.
        from emporia import relay_server as rs
        h = None
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                h = (await client.get(f"{peer}/health")).json()
        except Exception:  # noqa: BLE001
            h = None
        new = (h or {}).get("relay_public_key", "")
        if not (new and verify_raw(signed, sig, new) and await rs._repin_if_rotated(peer, new)):
            raise HTTPException(401, "relay request signature does not match the relay's pinned key")
    return peer


async def call_peer(peer: str, method: str, path: str, payload: dict | None = None) -> Any:
    import httpx
    body = json.dumps(payload).encode() if payload is not None else b""
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.request(method, f"{peer}{path}", content=body or None,
                                    headers=signed_headers(method, host_of(peer), path, body))


# ─── agents on other relays ──────────────────────────────────────────────────

def agent_record(agent_id: str) -> dict[str, Any] | None:
    """What a linked relay may know of an agent here: its address, public keys
    and the signed key log that leads to the current one. Nothing else — no
    wallets, no Nous account, no activity."""
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, public_key_hex, stable_id, encryption_public_key, encryption_key_signature "
            "FROM authorized_agents WHERE agent_id = ? AND is_active = 1", (agent_id,)).fetchone()
        log = rs.agent_key_log(conn, row["stable_id"]) if row else []
    if not row or not row["encryption_public_key"]:
        return None
    return {"address": address(row["agent_id"], own_host()), "public_key_hex": row["public_key_hex"],
            "stable_id": row["stable_id"], "key_log": log,    # P245: how a pin follows a rotation
            "encryption_public_key": row["encryption_public_key"],
            "encryption_key_signature": row["encryption_key_signature"]}


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
    if not re.fullmatch(r"[0-9a-f]{64}", pub) or not re.fullmatch(r"[0-9a-f]{64}", enc):
        raise HTTPException(422, f"{addr} has no usable keys")
    log = record.get("key_log") or []
    stable_id = str(record.get("stable_id") or "")
    windows = wire.key_log_windows(stable_id, log, rs.verify, pub)
    if not windows or windows[-1]["key"] != pub:
        raise HTTPException(422, f"{addr}'s key log does not lead from its stable id to the key it presents")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex, stable_id, key_log_json FROM remote_agents WHERE address = ?",
                           (addr,)).fetchone()
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
    if not rs.verify(signed_encryption_key_payload(agent_id, enc), enc_sig, pub):
        raise HTTPException(422, f"{addr}'s encryption key is not signed by its identity key")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO remote_agents (address, peer_url, public_key_hex, encryption_public_key, "
            "encryption_key_signature, updated_at, stable_id, key_log_json) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET public_key_hex = excluded.public_key_hex, "
            "encryption_public_key = excluded.encryption_public_key, "
            "encryption_key_signature = excluded.encryption_key_signature, updated_at = excluded.updated_at, "
            "stable_id = excluded.stable_id, key_log_json = excluded.key_log_json",
            (addr, peer, pub, enc, enc_sig, datetime.now(UTC).isoformat(), stable_id, json.dumps(log)))
    return {"address": addr, "public_key_hex": pub, "encryption_public_key": enc,
            "encryption_key_signature": enc_sig, "stable_id": stable_id, "key_log": log}


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
