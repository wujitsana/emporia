"""Access entitlements — what a payment actually buys (P2-3).

P1a/P1r both assumed a paid room/Agora entry fee was charged *per join*: every
`join_room()`/`subscribe_agora_topic()` call re-ran the payment gate, so a kicked
member (or a disconnect, or an Agora unsubscribe/resubscribe) either had to pay
again or — after the P1r-1/P1r-2 fixes made the ledger correctly represent two
distinct payments — silently triggered a second real payout to the resource's
creator, both for one purchase. Reviewed this pass and corrected by decision, not
just by code: a payment buys **access**, not a single entry. One `grant()` covers
every future join until it's `revoke()`-d or its window (if any) expires.

Rows are append-only, one per grant — never one row updated in place per
(resource, agent) — so a re-grant after an entitlement expires doesn't overwrite
the receipt for the payment that bought the first one. "Currently entitled" is
answered by `has_access()`, which looks only at the *newest* row for that
(resource_type, resource_id, agent_id): a later grant supersedes an earlier
revocation (a fresh payment after expiry restores access) but nothing supersedes
a `revoke(reason="kicked")` — see that function's own docstring for why re-grants
are unreachable after a kick in this codebase, which is what makes "newest row"
and "ever revoked for this reason" equivalent in practice for the ban check
(`is_revoked`) without either function depending on the other's assumptions.

Deliberately import-independent of `relay_server` — `rooms.py` needs to reach
these helpers too (a room's membership check is where expiry is enforced,
P2-5), and `relay_server` already imports both, so the dependency direction
must run relay_server → {rooms, entitlements}, not the other way. Connection
handling mirrors `rooms.py`'s own module-local `_connect()`/`_LOCK` rather than
`relay_server.get_db()`/`DB_LOCK` — both ultimately point at the same
`EMPORIA_DB_PATH` file (WAL mode makes that safe), but this module has no
dependency on relay_server to share its lock object with.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ENTITLEMENTS_DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia_relay.sqlite3")
).expanduser()

_LOCK = threading.RLock()

ENTITLEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS access_entitlements (
    entitlement_id     TEXT PRIMARY KEY,
    resource_type      TEXT NOT NULL,
    resource_id        TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    payment_intent_id  TEXT,
    granted_at         TEXT NOT NULL,
    expires_at         TEXT,
    revoked_at         TEXT,
    revoked_reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_entitlements_lookup
    ON access_entitlements(resource_type, resource_id, agent_id);
"""


def init_entitlements_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ENTITLEMENTS_DDL)


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or ENTITLEMENTS_DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@dataclass(frozen=True)
class Entitlement:
    entitlement_id: str
    resource_type: str
    resource_id: str
    agent_id: str
    payment_intent_id: str | None
    granted_at: str
    expires_at: str | None
    revoked_at: str | None
    revoked_reason: str | None


def _row_to_entitlement(row: sqlite3.Row) -> Entitlement:
    return Entitlement(
        entitlement_id=row["entitlement_id"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        agent_id=row["agent_id"],
        payment_intent_id=row["payment_intent_id"],
        granted_at=row["granted_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"],
    )


def grant(
    resource_type: str,
    resource_id: str,
    agent_id: str,
    payment_intent_id: str | None,
    duration_days: int | None = None,
    db_path: Path | None = None,
) -> Entitlement:
    """Record a new access grant. Always inserts — never updates an existing
    row — so the receipt for each payment survives even once superseded by a
    later grant. `duration_days=None` means no expiry (access lasts as long as
    the underlying resource does); a positive int sets `expires_at` that many
    days from now. Validate before calling: this trusts its caller on both
    `duration_days > 0` and PaymentIntent shape.
    """
    entitlement_id = f"ent_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    expires_at = (now + timedelta(days=duration_days)).isoformat() if duration_days else None
    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO access_entitlements "
            "(entitlement_id, resource_type, resource_id, agent_id, payment_intent_id, "
            "granted_at, expires_at, revoked_at, revoked_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (entitlement_id, resource_type, resource_id, agent_id, payment_intent_id,
             now.isoformat(), expires_at),
        )
        row = conn.execute(
            "SELECT * FROM access_entitlements WHERE entitlement_id = ?", (entitlement_id,)
        ).fetchone()
    return _row_to_entitlement(row)


def has_access(
    resource_type: str, resource_id: str, agent_id: str, db_path: Path | None = None
) -> bool:
    """True if this agent's *newest* grant for this resource is neither revoked
    nor expired. Only the newest row is consulted — an old revoked or expired
    row never overrides a later, good grant, and a good-looking old row never
    overrides a later revoke."""
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT expires_at, revoked_at FROM access_entitlements "
            "WHERE resource_type = ? AND resource_id = ? AND agent_id = ? "
            "ORDER BY granted_at DESC LIMIT 1",
            (resource_type, resource_id, agent_id),
        ).fetchone()
    if not row:
        return False
    if row["revoked_at"] is not None:
        return False
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC).isoformat():
        return False
    return True


def revoke(
    resource_type: str,
    resource_id: str,
    agent_id: str,
    reason: str,
    db_path: Path | None = None,
) -> bool:
    """Revoke this agent's current (newest, not-yet-revoked) grant for this
    resource. Returns False if there was nothing active to revoke — a caller
    with no entitlement row at all (a free-room member, someone who never paid)
    is not an error case.

    No un-revoke, no un-ban: this codebase has no path that grants access to an
    agent this pass's `reason="kicked"` callers have revoked (see
    `is_revoked()`), by product decision — a kick is meant to be final. If a
    future pass adds a creator override, it belongs here as a new function, not
    as a parameter that quietly reopens this one.
    """
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT entitlement_id FROM access_entitlements "
            "WHERE resource_type = ? AND resource_id = ? AND agent_id = ? "
            "AND revoked_at IS NULL ORDER BY granted_at DESC LIMIT 1",
            (resource_type, resource_id, agent_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE access_entitlements SET revoked_at = ?, revoked_reason = ? "
            "WHERE entitlement_id = ?",
            (datetime.now(UTC).isoformat(), reason, row["entitlement_id"]),
        )
    return True


def is_revoked(
    resource_type: str,
    resource_id: str,
    agent_id: str,
    reason: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """True if this agent has *ever* been revoked for this resource (optionally
    filtered to a specific `reason`, e.g. `"kicked"`). Checks every row, not
    just the newest: this is a ban check, not an access check (that's
    `has_access()`), and in this codebase nothing ever grants a new entitlement
    to an agent once `revoke(reason="kicked")` has run for them on that
    resource (P2-6's join-time ban check runs before the payment gate that
    would otherwise create one) — so "any row revoked for this reason" and
    "the newest row is revoked for this reason" agree in practice, but this
    checks the former so the guarantee doesn't depend on that ordering holding.
    """
    with _LOCK, _connect(db_path) as conn:
        if reason is None:
            query = (
                "SELECT 1 FROM access_entitlements WHERE resource_type = ? "
                "AND resource_id = ? AND agent_id = ? AND revoked_at IS NOT NULL LIMIT 1"
            )
            params = (resource_type, resource_id, agent_id)
        else:
            query = (
                "SELECT 1 FROM access_entitlements WHERE resource_type = ? "
                "AND resource_id = ? AND agent_id = ? AND revoked_at IS NOT NULL "
                "AND revoked_reason = ? LIMIT 1"
            )
            params = (resource_type, resource_id, agent_id, reason)
        row = conn.execute(query, params).fetchone()
    return row is not None
