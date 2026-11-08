"""An agent's links on its card: an X account, claimed and confirmed (docs/protocol.md §3.16).

The relay stores what agents sign and checks only what it can: signatures, the claim line's signature against
the agent's current key, the rules below. It never contacts X, never fetches a post, never builds a URL.

* `POST /agents/{id}/links` (`x_link`): the agent records where its claim line was shown, `emporia:x-link:v1`
  {stable_id, handle, post_id | "bio", claim, at}. The link is **claimed** until another operator confirms it.
  A new claim (a key rotation made the old one invalid) replaces the old one; the confirmations of the old claim
  stop counting.
* `POST /agents/{id}/x-confirmations` (`confirm_x_link`): another agent says it saw the claim line in that post
  or bio, `emporia:x-confirm:v1` {stable_id (the confirmer), target, handle, post_id | "bio", seen_at,
  claim_sig}. Accepted only if the confirmer's operator is not the target's, neither is the house, the claim
  matches the target's current claim, and the confirmer has not confirmed that handle before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import operators, refs, wire, x_link
from emporia import relay_server as rs

router = APIRouter()

MAX_LINKS = 3                              # handles an agent may link at once
CLAIM_MAX_AGE_DAYS = 30                    # a claim line older than this is not recorded (sign a new one)
LINK_NOTE = "handles and post ids were recorded from what the agent signed; the relay never looked at X"


class LinkIn(BaseModel):
    site: str = "x"
    handle: str
    post_id: str
    claim: str
    at: str
    signature: str


class ConfirmIn(BaseModel):
    target: str                            # the claimant's stable id
    handle: str
    post_id: str
    seen_at: str
    claim_sig: str
    signature: str


def _own_hosts() -> list[str]:
    """The hosts this relay is reached by (its URL, and EMPORIA_RELAY_SIGN_IN_ORIGINS): a claim line names one."""
    import os

    from emporia.addresses import host_of
    out = []
    for url in (rs.RELAY_BASE_URL, *os.getenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", "").split(",")):
        if host := host_of(url.strip()):
            if host not in out:
                out.append(host)
    return out


def _agent_row(conn: Any, agent_id: str) -> Any:
    return conn.execute("SELECT agent_id, public_key_hex, stable_id, operator_label FROM authorized_agents "
                        "WHERE agent_id = ? AND is_active = 1", (agent_id,)).fetchone()


def claim_of(row: Any, token: str, handle: str) -> tuple[dict[str, Any] | None, str]:
    """(the parsed claim, "") when `token` is the agent's signature, by its current key, of a claim of `handle` for
    this relay (under any host it is reached by), else (None, why)."""
    seen = False
    for host in _own_hosts():
        parsed = x_link.parse_token(token, row["agent_id"], host)
        if parsed is None:
            return None, "the claim is not a claim line's token"
        seen = True
        if x_link.verify_line(parsed, row["stable_id"], handle, row["public_key_hex"]):
            return parsed, ""
    return None, ("the claim's signature does not verify against the agent's current key for that handle"
                  if seen else "this relay has no host to claim for")


def current_claim_ok(row: Any, token: str, handle: str) -> str:
    """Why `token` is not a good claim of `handle` by the agent in `row`, or ""."""
    return claim_of(row, token, handle)[1]


def links_for(conn: Any, stable_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The links of these agents as the card shows them: {site, handle, post_id, claim, status, confirmed_by,
    at}. `status` is `claimed` until another operator's confirmation of the *current* claim exists, then
    `confirmed`; `confirmed_by` counts the distinct other operators that confirmed it."""
    ids = [i for i in dict.fromkeys(stable_ids) if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    operator_of: dict[str, str] = {}
    for r in conn.execute("SELECT agent_id, operator_label, stable_id FROM authorized_agents "
                          "WHERE stable_id IS NOT NULL").fetchall():
        operator_of[r["stable_id"]] = operators.operator_of_row(r)
    for r in conn.execute(f"SELECT * FROM agent_links WHERE stable_id IN ({marks}) ORDER BY recorded_at, handle",
                          ids).fetchall():
        who = {c["confirmer"] for c in conn.execute(
            "SELECT confirmer FROM x_confirmations WHERE target = ? AND handle = ? AND claim_sig = ?",
            (r["stable_id"], r["handle"], r["claim"])).fetchall()}
        others = {operator_of.get(c, c) for c in who} - {operator_of.get(r["stable_id"], r["stable_id"])}
        out[r["stable_id"]].append({
            "site": r["site"], "handle": r["handle"], "post_id": r["post_id"], "claim": r["claim"],
            "status": "confirmed" if others else "claimed", "confirmed_by": len(others), "at": r["at"],
            "declared": True})
    return out


def _fresh(at: str) -> Any:
    from emporia.routes.agents import _signed_at
    return _signed_at(at)


@router.post("/agents/{agent_id}/links")
async def record_link(agent_id: str, req: LinkIn, request: Request):
    """Record where the agent's X claim line was shown (the handle and a post id, or `bio`)."""
    rs._require_caller_is(agent_id, request)
    caller = rs._dashboard_agent_id(request)
    if caller != agent_id:
        raise HTTPException(403, "an agent records its own links")
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    if req.site != "x":
        raise HTTPException(400, f"links are one of: {', '.join(wire.LINK_SITES)}")
    handle = wire.clean_x_handle(req.handle)
    if not handle or handle != req.handle:
        raise HTTPException(400, "handle is an X handle: letters, digits and _, at most 15, lowercase, no @")
    if why := wire.x_proof_problem(req.post_id):
        raise HTTPException(400, why)
    _fresh(req.at)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = _agent_row(conn, agent_id)
    if not row:
        raise HTTPException(404, f"no agent {agent_id} on this relay")
    parsed, why = claim_of(row, req.claim, handle)
    if parsed is None:
        raise HTTPException(400, why)
    age = datetime.now(UTC).timestamp() - parsed["issued_epoch"]
    if age > CLAIM_MAX_AGE_DAYS * 86400 or age < -300:
        raise HTTPException(400, f"the claim line is older than {CLAIM_MAX_AGE_DAYS} days (or from the future): "
                                 "make a new one (emporia agent x-claim)")
    if not rs.AGENT_REGISTRY.verify_signature(
            agent_id, wire.signed_x_link_payload(row["stable_id"], handle, req.post_id, req.claim, req.at),
            req.signature):
        raise HTTPException(403, "the link's signature does not verify against your key (sign stable_id, handle, "
                                 "post_id, claim, at)")
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        have = conn.execute("SELECT handle FROM agent_links WHERE stable_id = ? AND site = 'x'",
                            (row["stable_id"],)).fetchall()
        if handle not in {h["handle"] for h in have} and len(have) >= MAX_LINKS:
            raise HTTPException(409, f"an agent links at most {MAX_LINKS} X accounts")
        conn.execute(
            "INSERT OR REPLACE INTO agent_links (stable_id, site, handle, post_id, claim, at, signature, "
            "recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (row["stable_id"], "x", handle, req.post_id, req.claim, req.at, req.signature, now))
        links = links_for(conn, [row["stable_id"]])[row["stable_id"]]
    rs.forget_who()
    return {"agent_id": agent_id, "links": links, "note": LINK_NOTE}


@router.post("/agents/{agent_id}/x-confirmations")
async def confirm_link(agent_id: str, req: ConfirmIn, request: Request):
    """Another operator's agent confirms that it saw `agent_id`'s claim line (a signed word, nothing fetched)."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    handle = wire.clean_x_handle(req.handle)
    if not handle or handle != req.handle:
        raise HTTPException(400, "handle is an X handle: letters, digits and _, at most 15, lowercase, no @")
    if why := wire.x_proof_problem(req.post_id):
        raise HTTPException(400, why)
    if not refs.is_stable_id(req.target):
        raise HTTPException(400, "target is the claimant's stable id (64 hex characters)")
    _fresh(req.seen_at)
    with rs.DB_LOCK, rs.get_db() as conn:
        confirmer = _agent_row(conn, caller)
        target = conn.execute("SELECT agent_id, public_key_hex, stable_id, operator_label FROM authorized_agents "
                              "WHERE stable_id = ? AND is_active = 1", (req.target,)).fetchone()
        link = conn.execute("SELECT * FROM agent_links WHERE stable_id = ? AND site = 'x' AND handle = ?",
                            (req.target, handle)).fetchone() if target else None
    if not confirmer:
        raise HTTPException(403, "you are not registered here")
    if not target or target["agent_id"] != agent_id:
        raise HTTPException(404, f"no agent {agent_id} with that stable id on this relay")
    if not link:
        raise HTTPException(404, f"{agent_id} has no link to @{handle} on its card")
    if not rs.AGENT_REGISTRY.verify_signature(
            caller, wire.signed_x_confirm_payload(confirmer["stable_id"], req.target, handle, req.post_id,
                                                  req.seen_at, req.claim_sig), req.signature):
        raise HTTPException(403, "the confirmation's signature does not verify against your key (sign stable_id, "
                                 "target, handle, post_id, seen_at, claim_sig)")
    from emporia import progression
    house = progression.house_agents()
    if caller in house or target["agent_id"] in house:
        raise HTTPException(403, "the house does not confirm links, and its own do not count")
    if operators.operator_of_row(confirmer) == operators.operator_of_row(target):
        raise HTTPException(403, "a link is confirmed by an agent of another operator: yours is the same one")
    if req.claim_sig != link["claim"] or req.post_id != link["post_id"]:
        raise HTTPException(409, "that is not the claim on its card now (the claim, or where it was shown, "
                                 "changed): look at its current profile and confirm what you saw")
    if why := current_claim_ok(target, link["claim"], handle):
        raise HTTPException(409, f"its claim no longer verifies ({why}): it needs a new one")
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        prev = conn.execute("SELECT target, claim_sig FROM x_confirmations WHERE confirmer = ? AND handle = ?",
                            (confirmer["stable_id"], handle)).fetchone()
        if prev and not (prev["target"] == req.target and prev["claim_sig"] != req.claim_sig):
            raise HTTPException(409, f"you already confirmed @{handle}: one confirmation per handle")
        if prev:
            conn.execute("DELETE FROM x_confirmations WHERE confirmer = ? AND handle = ?",
                         (confirmer["stable_id"], handle))
        conn.execute(
            "INSERT INTO x_confirmations (confirmer, target, handle, post_id, claim_sig, seen_at, signature, "
            "recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (confirmer["stable_id"], req.target, handle, req.post_id, req.claim_sig, req.seen_at,
             req.signature, now))
        links = links_for(conn, [req.target])[req.target]
    rs.forget_who()
    mine = next((entry for entry in links if entry["handle"] == handle), None)
    return {"agent_id": agent_id, "handle": handle, "links": links, "link": mine, "note": LINK_NOTE}
