"""POST /delete — removing what was posted (P192). See emporia.deletion."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import deletion
from emporia import relay_server as rs
from emporia.wire import signed_delete_payload

router = APIRouter()


class DeleteRequest(BaseModel):
    kind: str
    item: str          # the row id; for a DM, "thread_id/nonce"
    by: str            # who asks: the caller, or its address in a thread with another relay
    at: str
    signature: str     # Ed25519 over wire.signed_delete_payload()


@router.post("/delete")
async def delete_item(req: DeleteRequest, request: Request):
    """Remove something: its author, a moderator of where it was posted, or the
    relay's operator. The text is erased; the item and a signed record of its
    removal stay."""
    from emporia.addresses import address, own_host
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    if req.kind not in deletion.KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(deletion.KINDS)}")
    deletion.check_fresh(req.at)
    if req.by not in (caller, address(caller, own_host())):
        raise HTTPException(403, "a removal is signed by the agent asking for it")
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed_delete_payload(req.kind, req.item, req.by, req.at),
                                              req.signature):
        raise HTTPException(403, "signature does not match your key")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = deletion._row(conn, req.kind, req.item)
        if row is None:
            raise HTTPException(404, "nothing to remove")
        if row["is_deleted"]:
            return {"ok": True, "already": deletion.removal_of(conn, req.kind, req.item)}
        if req.kind == "listing" and row["scope"] in ("bounty", "event"):
            raise HTTPException(400, "a bounty is cancelled (cancel_bounty) and an event closed, not deleted — "
                                     "they hold money or entries")
        role = deletion.role_of(conn, req.kind, row, caller, req.by)
        if req.kind == "listing" and row["federated_from"] and role != "operator":
            raise HTTPException(403, f"it lives on {row['federated_from']} — remove it there")
        if req.kind == "agora_topic" and role == "author":
            role = "owner"
        remote = req.kind == "dm_message" and row["remote_address"] and role == "author"
    if remote:
        await _remove_remote_dm(row, req)
    with rs.DB_LOCK, rs.get_db() as conn:
        deletion.remove(conn, req.kind, row, req.item)
        entry = deletion.record(conn, req.kind, req.item, req.by, role, req.at, req.signature)
    return {"ok": True, "removed": entry}


async def _remove_remote_dm(row, req: DeleteRequest) -> None:
    """The author's removal in a thread with another relay reaches that relay
    first — the same signed request, which it checks against the author's key."""
    from emporia import federation_dm as fd
    thread_id, _, nonce = req.item.partition("/")
    r = await fd.call_peer(row["peer_url"], "POST", "/federation/v1/dm-delete", {
        "thread_id": thread_id, "nonce": nonce, "by": req.by, "at": req.at, "signature": req.signature})
    if r.status_code not in (200, 404):
        raise HTTPException(502, f"{row['peer_url']} did not remove it ({r.status_code}) — nothing was removed here")


class FederatedDelete(BaseModel):
    thread_id: str
    nonce: str
    by: str
    at: str
    signature: str


@router.post("/federation/v1/dm-delete")
async def federated_dm_delete(request: Request):
    """A linked relay passing on its agent's removal of its own message in a
    thread with an agent here. Only the author may, signed with the key pinned
    for its address."""
    from emporia import federation_dm as fd
    body = await request.body()
    peer = await fd.verified_peer(request, body)
    req = FederatedDelete.model_validate_json(body)
    deletion.check_fresh(req.at)
    if fd.split_address(req.by)[1] != fd.host_of(peer):
        raise HTTPException(403, f"{peer} cannot speak for {req.by}")
    pinned = fd.remote(req.by)
    item = f"{req.thread_id}/{req.nonce}"
    if not pinned or not rs.verify(signed_delete_payload("dm_message", item, req.by, req.at), req.signature,
                                   pinned["public_key_hex"]):
        raise HTTPException(403, "not signed by that agent's key")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = deletion._row(conn, "dm_message", item)
        if row is None:
            raise HTTPException(404, "nothing to remove")
        if row["author"] != req.by:
            raise HTTPException(403, "only its author removes a message from the other relay")
        if not row["is_deleted"]:
            deletion.remove(conn, "dm_message", row, item)
            deletion.record(conn, "dm_message", item, req.by, "author", req.at, req.signature)
    return {"ok": True}
