"""An agent's ERC-8004 link on its card (docs/protocol.md §3.20).

`POST /agents/{id}/links/erc8004` records what the agent signed (`emporia:erc8004-link:v1`: a wallet's EIP-712
statement and the agent's own signature). The relay checks the signatures and the registry (a canonical one for
that chain, from `networks.ERC8004_REGISTRIES`), and, where its operator configured an RPC for the chain
(`EMPORIA_RPC_<CHAINID>`), reads the chain once (`erc8004.verify`, one pinned block) to say what that proves.

Statuses: `claimed` (signatures check, no read yet or no RPC configured), `checked` (read at block N), `stale`
(a later read no longer proves control), `superseded` (another agent's newer link took the same 8004 agent over),
`removed` (unlinked by a signed removal; the marker stays). An RPC error changes nothing.

A link is a read plus an off-chain signature: no funds move, so the mainnet money gate does not apply here. The relay
reads only configured RPC URLs, fetches no URL named by a claim, and stores and serves no image or registration text.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import erc8004, networks, wire
from emporia import relay_server as rs
from emporia.chain_read import ChainUnknown, PinnedReader

router = APIRouter()
log = logging.getLogger(__name__)

MAX_LINKS = 3                       # live links an agent may have
LINK_MAX_AGE_DAYS = 30              # a wallet signature older than this is not recorded (sign a new one)
RECHECK_AFTER = 6 * 3600            # seconds: the last read must be older than this for a profile view to re-read
RETRY_AFTER = 60.0                  # seconds before a failed read of one link is tried again
READS_PER_MINUTE = 20               # all lazy re-reads together
LIVE = ("claimed", "checked", "stale")
NOTE = "the relay read the chain only where its operator configured an RPC; it stores no image and no registration text"

#: (caip2, urls) -> a pinned reader. A hook, so a test can put an in-memory chain behind the relay.
reader_factory: Callable[[str, list[str]], Any] = lambda caip2, urls: PinnedReader(caip2, urls)  # noqa: E731
#: starts a re-read: a daemon thread, or (in a test) a call made on the spot.
spawn: Callable[[Callable[[], None]], None] = lambda fn: threading.Thread(target=fn, daemon=True).start()  # noqa: E731

_inflight: set[tuple[str, str, str]] = set()
_last_try: dict[tuple[str, str, str], float] = {}
_reads: deque[float] = deque()
_guard = threading.Lock()


class ChainLinkIn(BaseModel):
    agent_registry: str
    agent_id: str
    signer: str
    issued_at: int
    wallet_sig: str
    at: str
    signature: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _chain_of(registry: str) -> str:
    return "eip155:" + registry.split(":")[1]


def read_link(link: dict[str, Any], *, urls: list[str] | None = None) -> dict[str, Any] | None:
    """`erc8004.verify` of this link on the operator's configured RPCs; None when none is configured. Blocking."""
    chain = _chain_of(link["agent_registry"])
    urls = networks.rpc_configured(chain) if urls is None else urls
    if not urls:
        return None
    try:
        reader = reader_factory(chain, urls)
    except ChainUnknown:
        return {"status": "unknown", "tier": "", "reason": "no reader"}
    return erc8004.verify(link, reader)


def _link_of(row: Any) -> dict[str, Any]:
    return {"agent_registry": row["agent_registry"], "agent_id": row["agent_id"], "signer": row["signer"],
            "stable_id": row["stable_id"], "issued_at": row["issued_at"], "wallet_sig": row["wallet_sig"]}


def card_entry(row: Any) -> dict[str, Any]:
    """One link as the card shows it. `checked` is the last read that proved control (a stale link keeps it)."""
    checked = ({"block_number": row["block_number"], "block_hash": row["block_hash"], "at": row["checked_at"]}
               if row["checked_at"] else None)
    return {"site": "erc8004", "agent_registry": row["agent_registry"], "agent_id": row["agent_id"],
            "signer": row["signer"], "tier": row["tier"], "status": row["status"], "checked": checked,
            "confirmed_by": 0, "testnet": networks.is_testnet(_chain_of(row["agent_registry"])),
            "issued_at": row["issued_at"], "wallet_sig": row["wallet_sig"], "at": row["at"], "declared": True}


def links_for(conn: Any, stable_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The ERC-8004 links of these agents as their cards show them (removed ones are off the card)."""
    ids = [i for i in dict.fromkeys(stable_ids) if i]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(f"SELECT * FROM agent_chain_links WHERE stable_id IN ({marks}) AND status <> 'removed' "
                          "ORDER BY recorded_at, agent_registry, agent_id", ids).fetchall():
        out.setdefault(r["stable_id"], []).append(card_entry(r))
    return out


# -- recording ------------------------------------------------------------------------------------------
def _outcome(link: dict[str, Any], chain: str) -> tuple[str, dict[str, Any]]:
    """(status, the read) for a new link: refuses what is provably not controlled, else claimed or checked."""
    got = read_link(link)
    if got is None:                              # no RPC of ours for this chain
        recovered = erc8004.signature_recovers(link)
        if recovered is False:
            raise HTTPException(400, "this relay has no RPC for that chain, so it can check only a signature from an "
                                     "ordinary account, and this one is not (a contract wallet's is settled by a read)")
        return "claimed", {}
    if got["status"] == "proven":
        return "checked", got
    if got["status"] in ("not-proven", "invalid"):
        raise HTTPException(409, f"the chain does not show that wallet controls it: {got['reason']}")
    return "claimed", {}                          # unknown or not-checked: say nothing more than claimed


def _rank(status: str, issued_at: int) -> tuple[int, int]:
    return (1 if status == "checked" else 0, issued_at)


@router.post("/agents/{agent_id}/links/erc8004")
async def record_chain_link(agent_id: str, req: ChainLinkIn, request: Request):
    """Record that a wallet controls an ERC-8004 agent, as the agent signed it."""
    rs._require_caller_is(agent_id, request)
    if rs._dashboard_agent_id(request) != agent_id:
        raise HTTPException(403, "an agent records its own links")
    rs._require_registered(agent_id)
    rs._enforce_agent_rate_limit(agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT agent_id, stable_id FROM authorized_agents WHERE agent_id = ? AND is_active = 1",
                           (agent_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"no agent {agent_id} on this relay")
    stable = row["stable_id"]
    link = {"agent_registry": req.agent_registry, "agent_id": req.agent_id, "signer": req.signer,
            "stable_id": stable, "issued_at": req.issued_at, "wallet_sig": req.wallet_sig}
    if why := wire.erc8004_link_problem(link):
        raise HTTPException(400, why)
    chain = _chain_of(req.agent_registry)
    canonical = networks.ERC8004_REGISTRIES.get(chain)
    if canonical is None:
        raise HTTPException(400, f"no ERC-8004 registry is known for {chain}")
    if req.agent_registry.split(":")[2] != canonical.lower():
        raise HTTPException(400, "that is not the canonical ERC-8004 registry for that chain")
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    age = datetime.now(UTC).timestamp() - req.issued_at
    if age > LINK_MAX_AGE_DAYS * 86400 or age < -300:
        raise HTTPException(400, f"the wallet signature is older than {LINK_MAX_AGE_DAYS} days (or from the future): "
                                 "sign a new one (emporia agent link-8004)")
    if not rs.AGENT_REGISTRY.verify_signature(
            agent_id, wire.signed_erc8004_link_payload(stable, req.agent_registry, req.agent_id, req.signer,
                                                       req.issued_at, req.wallet_sig, req.at), req.signature):
        raise HTTPException(403, "the link's signature does not verify against your key (sign stable_id, "
                                 "agent_registry, agent_id, signer, issued_at, wallet_sig, at)")
    status, got = await asyncio.to_thread(_outcome, link, chain)
    now = _now()
    key = (stable, req.agent_registry, req.agent_id)
    with rs.DB_LOCK, rs.get_db() as conn:
        mine = conn.execute("SELECT status, issued_at FROM agent_chain_links WHERE stable_id = ? AND "
                            "agent_registry = ? AND agent_id = ?", key).fetchone()
        if mine and mine["status"] != "removed" and mine["issued_at"] >= req.issued_at:
            raise HTTPException(409, "a link as new or newer is already on file for that agent: sign a new one")
        live = conn.execute("SELECT COUNT(*) FROM agent_chain_links WHERE stable_id = ? AND status IN "
                            "('claimed','checked','stale') AND NOT (agent_registry = ? AND agent_id = ?)", key).fetchone()[0]
        if live >= MAX_LINKS:
            raise HTTPException(409, f"an agent links at most {MAX_LINKS} ERC-8004 identities: unlink one first")
        loser_to = ""
        for other in conn.execute("SELECT stable_id, status, issued_at FROM agent_chain_links WHERE agent_registry = ? "
                                  "AND agent_id = ? AND stable_id <> ? AND status IN ('claimed','checked','stale')",
                                  (req.agent_registry, req.agent_id, stable)).fetchall():
            if _rank(status, req.issued_at) > _rank("checked" if other["status"] == "checked" else "claimed",
                                                    other["issued_at"]):
                conn.execute("UPDATE agent_chain_links SET status = 'superseded', superseded_by = ? WHERE stable_id = ? "
                             "AND agent_registry = ? AND agent_id = ?",
                             (stable, other["stable_id"], req.agent_registry, req.agent_id))
            else:
                loser_to = other["stable_id"]
        shown = "superseded" if loser_to else status
        conn.execute(
            "INSERT OR REPLACE INTO agent_chain_links (stable_id, agent_registry, agent_id, signer, issued_at, wallet_sig, "
            "at, signature, recorded_at, status, tier, owner, block_number, block_hash, checked_at, attempted_at, "
            "superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stable, req.agent_registry, req.agent_id, req.signer, req.issued_at, req.wallet_sig, req.at, req.signature,
             now, shown, got.get("tier", "") if status == "checked" else "", got.get("owner", ""),
             got.get("block_number"), got.get("block_hash", ""), now if status == "checked" else "",
             now if got else "", loser_to))
        stored = conn.execute("SELECT * FROM agent_chain_links WHERE stable_id = ? AND agent_registry = ? AND agent_id = ?",
                              key).fetchone()
        links = links_for(conn, [stable]).get(stable, [])
    rs.forget_who()
    return {"agent_id": agent_id, "link": card_entry(stored), "links": links, "note": NOTE}


# -- re-checks ------------------------------------------------------------------------------------------
def _due(row: Any, now: float) -> bool:
    last = row["attempted_at"] or row["checked_at"]
    if not last:
        return True
    when = wire.when_of(last)
    return when is None or now - when.timestamp() > RECHECK_AFTER


def recheck_link(key: tuple[str, str, str]) -> str:
    """Read one link again and update it: `checked` when the chain proves control, `stale` when it no longer does,
    nothing at all on an RPC error. Only a change of status, tier or owner touches the logged columns. Returns what happened (for a test). Blocking."""
    try:
        with rs.DB_LOCK, rs.get_db() as conn:
            row = conn.execute("SELECT * FROM agent_chain_links WHERE stable_id = ? AND agent_registry = ? "
                               "AND agent_id = ?", key).fetchone()
        if row is None or row["status"] not in LIVE:
            return "gone"
        got = read_link(_link_of(row))
        if got is None:
            return "no-rpc"
        now = _now()
        with rs.DB_LOCK, rs.get_db() as conn:
            if got["status"] == "proven":
                if (row["status"], row["tier"], row["owner"]) == ("checked", got["tier"], got["owner"]):
                    # nothing changed: only the re-read clock moves. The block of the last good read stays in the
                    # table (and on the card), and the transparency log gets no entry for a re-read.
                    conn.execute("UPDATE agent_chain_links SET attempted_at = ? WHERE stable_id = ? AND "
                                 "agent_registry = ? AND agent_id = ? AND status = 'checked'", (now, *key))
                    return "checked"
                conn.execute("UPDATE agent_chain_links SET status = 'checked', tier = ?, owner = ?, block_number = ?, "
                             "block_hash = ?, checked_at = ?, attempted_at = ? WHERE stable_id = ? AND agent_registry = ? "
                             "AND agent_id = ? AND status IN ('claimed','checked','stale')",
                             (got["tier"], got["owner"], got["block_number"], got["block_hash"], now, now, *key))
                return "checked"
            if got["status"] in ("not-proven", "invalid"):
                if row["status"] == "stale":
                    conn.execute("UPDATE agent_chain_links SET attempted_at = ? WHERE stable_id = ? AND "
                                 "agent_registry = ? AND agent_id = ? AND status = 'stale'", (now, *key))
                    return "stale"
                conn.execute("UPDATE agent_chain_links SET status = 'stale', attempted_at = ? WHERE stable_id = ? AND "
                             "agent_registry = ? AND agent_id = ? AND status IN ('claimed','checked','stale')",
                             (now, *key))
                return "stale"
        return "unknown"                            # an RPC error: nothing changes
    finally:
        with _guard:
            _inflight.discard(key)
        rs.forget_who()


def schedule_rechecks(stable_ids: list[str], *, now: float | None = None) -> list[tuple[str, str, str]]:
    """On a profile view: start a re-read of each of these agents' links whose last read is older than 6 hours.
    One read in flight per link, a pause after a failure, and a budget across all links. Returns those started."""
    ids = [i for i in dict.fromkeys(stable_ids) if i]
    if not ids:
        return []
    clock = time.monotonic() if now is None else now
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM agent_chain_links WHERE stable_id IN ({','.join('?' * len(ids))}) "
                            "AND status IN ('claimed','checked','stale')", ids).fetchall()
    started = []
    wall = datetime.now(UTC).timestamp()
    for row in rows:
        key = (row["stable_id"], row["agent_registry"], row["agent_id"])
        if not _due(row, wall) or not networks.rpc_configured(_chain_of(row["agent_registry"])):
            continue
        with _guard:
            while _reads and _reads[0] <= clock - 60:
                _reads.popleft()
            if key in _inflight or len(_reads) >= READS_PER_MINUTE or clock - _last_try.get(key, -1e9) < RETRY_AFTER:
                continue
            _inflight.add(key)
            _reads.append(clock)
            _last_try[key] = clock
        started.append(key)
        spawn(lambda key=key: recheck_link(key))
    return started
