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

P2r-1: a kick's *ban* is `record_removal()`/`is_removed()`, a separate
`access_removals` table — not `revoke()`/`is_revoked()` on `access_entitlements`.
The original P2-6 ban check was `is_revoked(reason="kicked")`, which only ever
returns True if there was an active *paid* entitlement to revoke in the first
place — so on an `open` or `invite` room, where nothing was ever purchased,
`revoke()` had nothing to revoke, wrote nothing, and a kicked agent walked
straight back in. `revoke()` still runs on every kick (it ends the paid
window, so a still-valid entitlement can't outlive the ban), but it is no
longer what makes the ban stick — `record_removal()` is, unconditionally,
regardless of whether this resource/agent pair ever had a payment at all.
Kept as two separate tables rather than folding removal into
`access_entitlements` as a special row: `has_any_grant()`'s entire job is
distinguishing "never had an entitlement" (grandfathered) from "had one that
lapsed" (must be denied), and a tombstone entitlement row for a never-paid
kicked agent would make that distinction lie.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from emporia.payments import KNOWN_RAILS

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
    revoked_reason     TEXT,
    rail               TEXT NOT NULL DEFAULT 'stripe'
);

CREATE INDEX IF NOT EXISTS idx_access_entitlements_lookup
    ON access_entitlements(resource_type, resource_id, agent_id);

CREATE TABLE IF NOT EXISTS access_removals (
    removal_id     TEXT PRIMARY KEY,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    removed_at     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    removed_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_removals_lookup
    ON access_removals(resource_type, resource_id, agent_id);
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
    rail: str


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
        rail=row["rail"],
    )


def grant(
    resource_type: str,
    resource_id: str,
    agent_id: str,
    payment_intent_id: str | None,
    duration_days: int | None = None,
    *,
    rail: str,
    db_path: Path | None = None,
) -> Entitlement:
    """Record a new access grant. Always inserts — never updates an existing
    row — so the receipt for each payment survives even once superseded by a
    later grant. `duration_days=None` means no expiry (access lasts as long as
    the underlying resource does); a positive int sets `expires_at` that many
    days from now. Validate before calling: this trusts its caller on both
    `duration_days > 0` and PaymentIntent shape.

    `rail` is required and keyword-only (P3) — deliberately not defaulted, so
    every call site has to name the rail it verified the payment on rather
    than one silently inheriting whatever the default happened to be. Must be
    one of `payments.KNOWN_RAILS`; this is the funnel that would let an
    unrecognized rail value reach storage, so it's asserted here rather than
    trusted from the caller.
    """
    if rail not in KNOWN_RAILS:
        raise ValueError(f"Unknown payment rail {rail!r}, expected one of {sorted(KNOWN_RAILS)}")
    entitlement_id = f"ent_{uuid.uuid4().hex[:16]}"
    now = datetime.now(UTC)
    expires_at = (now + timedelta(days=duration_days)).isoformat() if duration_days else None
    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO access_entitlements "
            "(entitlement_id, resource_type, resource_id, agent_id, payment_intent_id, "
            "granted_at, expires_at, revoked_at, revoked_reason, rail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (entitlement_id, resource_type, resource_id, agent_id, payment_intent_id,
             now.isoformat(), expires_at, rail),
        )
        row = conn.execute(
            "SELECT * FROM access_entitlements WHERE entitlement_id = ?", (entitlement_id,)
        ).fetchone()
    return _row_to_entitlement(row)


_HAS_ACCESS_SQL = (
    "SELECT expires_at, revoked_at FROM access_entitlements "
    "WHERE resource_type = ? AND resource_id = ? AND agent_id = ? "
    "ORDER BY granted_at DESC LIMIT 1"
)

_HAS_ANY_GRANT_SQL = (
    "SELECT 1 FROM access_entitlements "
    "WHERE resource_type = ? AND resource_id = ? AND agent_id = ? LIMIT 1"
)


def has_access(
    resource_type: str, resource_id: str, agent_id: str, db_path: Path | None = None,
    *, conn: sqlite3.Connection | None = None,
) -> bool:
    """True if this agent's *newest* grant for this resource is neither revoked
    nor expired. Only the newest row is consulted — an old revoked or expired
    row never overrides a later, good grant, and a good-looking old row never
    overrides a later revoke.

    P1br-3: `conn`, if given, reuses an already-open connection instead of
    opening a new one — `_agora_is_member()` (relay_server.py) passes its
    own DB_LOCK-held connection down, since it's called per-row in the
    Agora feed / a topic's post list and was opening up to two fresh
    connections per row for a paid topic before this. Optional and
    defaults to today's own-connection behavior, so no existing caller
    needs to change.

    P1brr (A1): the two branches share `_HAS_ACCESS_SQL` rather than each
    carrying its own copy of the query text — P1br-3 introduced the branch
    but left the SQL duplicated, the exact drift hazard
    `payments.x402_requirement()` was factored out to kill in the
    immediately preceding pass. `contextlib.nullcontext(conn)` lets the
    passed-in-connection branch reuse the same `with _LOCK, ctx as c:`
    shape as the owned-connection branch without committing the caller's
    transaction out from under them (`sqlite3.Connection.__exit__` commits;
    `nullcontext.__exit__` does nothing).
    """
    ctx = contextlib.nullcontext(conn) if conn is not None else _connect(db_path)
    with _LOCK, ctx as c:
        row = c.execute(
            _HAS_ACCESS_SQL, (resource_type, resource_id, agent_id)
        ).fetchone()
    if not row:
        return False
    if row["revoked_at"] is not None:
        return False
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC).isoformat():
        return False
    return True


def has_any_grant(
    resource_type: str, resource_id: str, agent_id: str, db_path: Path | None = None,
    *, conn: sqlite3.Connection | None = None,
) -> bool:
    """True if this agent has ever received a grant for this resource, active
    or not. Exists to distinguish "this entitlement expired/was revoked"
    (`has_access()` is False *and* this is True — access should now be denied)
    from "this membership predates entitlements existing at all" (`has_access()`
    is False *and* this is also False — a member added before P2-3 shipped, who
    must not be locked out by expiry enforcement for a window that never
    applied to them). See `rooms.is_member()` (P2-5) and `_agora_is_member()`
    (relay_server.py, P2-5's Agora sibling), the two callers.

    `conn` — see `has_access()`'s docstring; same optional reused-connection
    parameter, same default, and same single-sourced `_HAS_ANY_GRANT_SQL`
    (P1brr, A1).
    """
    ctx = contextlib.nullcontext(conn) if conn is not None else _connect(db_path)
    with _LOCK, ctx as c:
        row = c.execute(
            _HAS_ANY_GRANT_SQL, (resource_type, resource_id, agent_id)
        ).fetchone()
    return row is not None


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
    is not an error case, and is exactly the case `record_removal()`/
    `is_removed()` exist to cover instead (P2r-1): this function alone cannot
    make a ban stick for an agent who never had anything to revoke.

    No un-revoke: this codebase has no path that grants a *new* entitlement to
    an agent this pass's `reason="kicked"` callers have revoked (see
    `is_revoked()`), by product decision. If a future pass adds a creator
    override, it belongs here as a new function, not as a parameter that
    quietly reopens this one.
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
    just the newest: this is about a specific entitlement's history, not the
    ban check (that's `is_removed()`, P2r-1) — `revoke()` still runs on every
    kick to end the paid window, so this stays a meaningful, tested question
    ("was there ever a revoked entitlement here"), but it is no longer what
    join_room() consults to decide whether a kicked agent can rejoin: an agent
    kicked from an `open` or `invite` room never had an entitlement to revoke
    in the first place, and this would silently say "not revoked" for them.
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


def record_removal(
    resource_type: str,
    resource_id: str,
    agent_id: str,
    reason: str,
    removed_by: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Record that this agent was removed from this resource — the actual ban
    mechanism (P2r-1, R1). Unlike `revoke()`, this does not require an active
    entitlement to exist: a kick from a free `open` room writes a row here too,
    which is the whole point — `revoke()` alone silently did nothing for an
    agent who never paid, so the ban never stuck outside a paid room. Always
    inserts (append-only, same as `grant()`); a re-kick is not reachable in
    practice (a removed agent can't rejoin to be kicked again — see
    `is_removed()`), but this doesn't need to guard against it either way,
    since a duplicate row changes nothing about the answer `is_removed()`
    gives.
    """
    removal_id = f"rem_{uuid.uuid4().hex[:16]}"
    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO access_removals "
            "(removal_id, resource_type, resource_id, agent_id, removed_at, reason, removed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (removal_id, resource_type, resource_id, agent_id,
             datetime.now(UTC).isoformat(), reason, removed_by),
        )


def is_removed(
    resource_type: str,
    resource_id: str,
    agent_id: str,
    reason: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """True if this agent has ever been removed from this resource (optionally
    filtered to a specific `reason`). This is the ban check every gate type
    must consult — `is_revoked()` only answers the question for agents who had
    a paid entitlement to revoke, which a kicked agent in a free room never
    did. No un-remove: same product decision as `revoke()`'s "no un-revoke",
    stated again here because this is the function that actually enforces it.
    """
    with _LOCK, _connect(db_path) as conn:
        if reason is None:
            query = (
                "SELECT 1 FROM access_removals WHERE resource_type = ? "
                "AND resource_id = ? AND agent_id = ? LIMIT 1"
            )
            params = (resource_type, resource_id, agent_id)
        else:
            query = (
                "SELECT 1 FROM access_removals WHERE resource_type = ? "
                "AND resource_id = ? AND agent_id = ? AND reason = ? LIMIT 1"
            )
            params = (resource_type, resource_id, agent_id, reason)
        row = conn.execute(query, params).fetchone()
    return row is not None
