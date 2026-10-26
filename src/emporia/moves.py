"""An agent changing its home relay (P190).

A move is the agent's own signed relay list naming a new home — signed by its
key, so any relay can check it and no relay, the old one included, can forge or
block it. The old relay, once it has that list, answers "moved to" for 90 days
wherever the agent is asked for: its card, its record, a DM. Open games and
escrow holds stay where they are and must finish first; the record the old
relay signed travels with the agent and is shown as that relay's word.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

FORWARD_DAYS = 90
# What GET /agents/{id}/record adds around the record its relay signed.
RECORD_UNSIGNED = ("relay", "relay_public_key", "signature", "elsewhere", "moved_to")


def signed_part(record_response: dict[str, Any]) -> dict[str, Any]:
    """The part of a record response its relay's signature covers."""
    return {k: v for k, v in record_response.items() if k not in RECORD_UNSIGNED}


def moved_to(agent_id: str) -> str:
    """`agent@new-host` while this relay says the agent moved away; "" otherwise."""
    from emporia import relay_server as rs
    from emporia.addresses import address, host_of
    from emporia.identity import normalize_peer_url
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT relay_list_json FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
    if not row or not row["relay_list_json"]:
        return ""
    listed = json.loads(row["relay_list_json"])["list"]
    if normalize_peer_url(listed.get("home", "")) == normalize_peer_url(rs.RELAY_BASE_URL):
        return ""
    try:
        when = datetime.fromisoformat(listed["updated_at"])
    except (KeyError, ValueError):
        return ""
    if datetime.now(UTC) - when > timedelta(days=FORWARD_DAYS):
        return ""
    return address(agent_id, host_of(listed["home"]))


def gone(agent_id: str) -> None:
    """Refuse, with where it went, anything addressed to an agent that moved away."""
    from fastapi import HTTPException
    if where := moved_to(agent_id):
        raise HTTPException(410, {"moved_to": where,
                                  "message": f"{agent_id} moved to {where}; reach it there."})


def carried_record(signed: dict[str, Any], agent_key: str) -> dict[str, Any]:
    """Check a record another relay signed for this agent, before it is shown here
    as that relay's word: it names this agent's key, and its signature matches
    the key that relay presented (not one this relay pinned — shown as such)."""
    from fastapi import HTTPException

    from emporia.addresses import fingerprint
    from emporia.identity import federation_signable_bytes, verify_raw
    record = signed_part(signed)
    if record.get("public_key_hex") != agent_key:
        raise HTTPException(403, "that record is of another key, not this agent's")
    if not verify_raw(federation_signable_bytes(signed.get("relay", ""), "record", [record]),
                      signed.get("signature", ""), signed.get("relay_public_key", "")):
        raise HTTPException(422, "the record's signature does not match the key its relay presented")
    return {**record, "relay": signed.get("relay"), "relay_public_key": signed.get("relay_public_key"),
            "signature": signed.get("signature"),
            "relay_key_fingerprint": fingerprint(signed.get("relay_public_key", ""))}
