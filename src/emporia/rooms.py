"""Emporia chat/collab rooms.

Two access tiers:
  public   — visible to all; any registered agent can join; gate_type='open'
  private  — hidden from listing unless member; gated by:
               invite          — creator explicitly invites agent_ids
               paid            — agent pays entry_fee_cents to join (rail-tagged, P3)

Three message types: chat, collab (structured work notes), code (fenced snippet).

Rooms link to sessions: a game session can have an associated room for negotiation
and commentary running alongside the formal move sequence.

Encryption flag:
  encrypted=False (default) — relay-enforced access control; guardrails run on content
  encrypted=True            — real end-to-end encryption (P18): the relay stores an
                              opaque `crypto_box` envelope, skips the guardrails scan,
                              and enforces membership coverage only — it never holds a
                              key. Both clients seal before POST and open after GET via
                              `crypto_box.py`, with peer keys published through
                              `identity.trust_peer_encryption_key()`. A member who joins
                              after a message was sent cannot read it — no key was
                              wrapped for them, and re-wrapping history would require the
                              relay to hold plaintext, the whole thing being avoided.

Negotiation routing:
  msg_type='counter_offer' in a 2-member private room triggers negotiation.process_offer()
  automatically. The relay responds with the broker's ACCEPT or COUNTER decision as a
  system message in the same room.

Message chain integrity:
  Each message hashes (prev_hash:sender:content:created_at) → chain_hash, computed by the
  relay at write time. Detects tampering with an already-stored row by an integrity check
  that recomputes and compares — nothing in this codebase does that yet (P19: recorded as
  an open item, not implied solved by the chain's existence). Replay — a relay copying one
  row's content and signature onto another — is a *different* threat the chain does not
  address on its own, since a relay replaying a row simply recomputes the chain too; P19-5
  closes that with a sender-chosen `nonce`, the same mechanism P18-2 gave DMs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple

from emporia import config, entitlements

# P55: no module-level default lives here any more. This one used to name
# `~/.hermes/emporia_relay.sqlite3` — a different file, in a different
# directory, from the one relay_server opened — and was reachable whenever a
# caller omitted `db_path=` before relay_server had been imported.
# `config.database_path()` is now the only resolver in the package.

_LOCK = threading.RLock()

RoomType = Literal["public", "private"]
GateType = Literal["open", "invite", "paid"]
MsgType = Literal["chat", "collab", "code", "counter_offer", "accept", "reject", "system"]


# ============================================================================
# Schema
# ============================================================================

ROOMS_DDL = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    room_type TEXT NOT NULL DEFAULT 'public',
    gate_type TEXT NOT NULL DEFAULT 'open',
    entry_fee_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    creator_id TEXT NOT NULL,
    members_json TEXT NOT NULL DEFAULT '[]',
    max_members INTEGER,
    encrypted INTEGER NOT NULL DEFAULT 0,
    linked_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    access_duration_days INTEGER
);

CREATE TABLE IF NOT EXISTS room_invites (
    room_id TEXT NOT NULL,
    invitee_id TEXT NOT NULL,
    invited_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (room_id, invitee_id)
);

CREATE TABLE IF NOT EXISTS room_messages (
    message_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    msg_type TEXT NOT NULL DEFAULT 'chat',
    content TEXT NOT NULL,
    parent_message_id TEXT,
    chain_hash TEXT,
    signature TEXT,
    nonce TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_room_messages_room ON room_messages(room_id);
CREATE INDEX IF NOT EXISTS idx_room_messages_created ON room_messages(room_id, created_at);
CREATE INDEX IF NOT EXISTS idx_room_invites_invitee ON room_invites(invitee_id);
CREATE INDEX IF NOT EXISTS idx_rooms_type ON rooms(room_type);
CREATE INDEX IF NOT EXISTS idx_rooms_session ON rooms(linked_session_id);
"""


def init_rooms_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ROOMS_DDL)
    # P192: when a message's text was removed — added to rooms made before it.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(room_messages)").fetchall()}
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE room_messages ADD COLUMN deleted_at TEXT")


# ============================================================================
# Data classes
# ============================================================================

@dataclass(frozen=True)
class Room:
    room_id: str
    name: str
    description: str
    room_type: str
    gate_type: str
    entry_fee_cents: int
    currency: str
    creator_id: str
    members: list[str]
    max_members: int | None
    encrypted: bool
    linked_session_id: str | None
    created_at: str
    updated_at: str
    access_duration_days: int | None = None

    def to_dict(self, *, viewer_id: str | None = None, db_path: Path | None = None) -> dict[str, Any]:
        d = asdict(self)
        # Hide full member list for private rooms unless viewer has active
        # access (P2r-4, R4) — previously `viewer_id not in self.members`,
        # which disclosed the full roster to a private paid room's member
        # even after their entitlement expired (see _access_active()'s
        # docstring, the module-level function this delegates to).
        is_active = viewer_id is not None and _access_active(
            self.room_id, self.gate_type, self.creator_id, self.members,
            viewer_id, db_path=db_path,
        )
        if self.room_type == "private" and not is_active:
            d["members"] = []
            d["member_count"] = len(self.members)
            d["encrypted"] = self.encrypted
        return d

    def is_negotiation_room(self) -> bool:
        """A 2-member private room is a negotiation channel."""
        return self.room_type == "private" and len(self.members) == 2


@dataclass(frozen=True)
class RoomMessage:
    message_id: str
    room_id: str
    sender_id: str
    msg_type: str
    content: str
    parent_message_id: str | None
    chain_hash: str | None
    signature: str | None
    nonce: str | None
    metadata: dict[str, Any]
    created_at: str
    # P31-3: this message's rowid — the second half of a room's total message
    # ordering, `(created_at, rowid)`, which `verify_chain()` and
    # `_last_chain_hash()` have both used since P21-7. Served so a paginating
    # client can build a cursor that can't skip a message sharing a timestamp
    # with the page boundary. None when the row wasn't read with it.
    seq: int | None = None
    # P192: when its text was removed (emporia.deletion); None if it was not.
    deleted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("metadata", None)
        d["metadata"] = self.metadata
        return d


# ============================================================================
# DB helpers
# ============================================================================

def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.database_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _row_to_room(row: sqlite3.Row) -> Room:
    return Room(
        room_id=row["room_id"],
        name=row["name"],
        description=row["description"] or "",
        room_type=row["room_type"],
        gate_type=row["gate_type"],
        entry_fee_cents=int(row["entry_fee_cents"]),
        currency=row["currency"] or "USD",
        creator_id=row["creator_id"],
        members=json.loads(row["members_json"] or "[]"),
        max_members=row["max_members"],
        encrypted=bool(row["encrypted"]),
        linked_session_id=row["linked_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        access_duration_days=row["access_duration_days"],
    )


def _row_to_message(row: sqlite3.Row) -> RoomMessage:
    return RoomMessage(
        message_id=row["message_id"],
        room_id=row["room_id"],
        sender_id=row["sender_id"],
        msg_type=row["msg_type"],
        content=row["content"],
        parent_message_id=row["parent_message_id"],
        chain_hash=row["chain_hash"],
        signature=row["signature"],
        nonce=row["nonce"],
        metadata=json.loads(row["metadata_json"] or "{}"),
        created_at=row["created_at"],
        seq=row["seq"] if "seq" in row.keys() else None,
        deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,
    )


def _removed(msg: Any) -> bool:
    try:
        return bool(msg["deleted_at"])
    except (KeyError, IndexError):
        return False


def link_ok(prev_hash: str | None, msg: Any) -> bool:
    """P192: does `msg` chain onto `prev_hash`? A removed message's text is gone,
    so its own link cannot be recomputed: its stored hash is taken, the next
    message must still chain onto it, and the gap carries its signed removal —
    a reader sees a removal, never what looks like tampering."""
    if _removed(msg):
        return True
    return compute_chain_hash(prev_hash, msg["sender_id"], msg["content"], msg["created_at"]) == msg["chain_hash"]


def compute_chain_hash(
    prev_hash: str | None,
    sender_id: str,
    content: str,
    created_at: str,
) -> str:
    """P21-7: made public (was `_compute_chain_hash`) — `verify_chain()` below
    needs to call the exact same frame the writer uses rather than
    reimplementing it, the DRY wart `session_audit.verify_chain()` has (its
    frame is hand-copied from `log_public_receipt()`, not shared)."""
    frame = f"{prev_hash or 'GENESIS'}:{sender_id}:{content}:{created_at}"
    return hashlib.sha256(frame.encode()).hexdigest()


def _last_chain_hash(room_id: str, conn: sqlite3.Connection) -> str | None:
    # P21-7: rowid tiebreak added so this agrees with verify_chain()'s walk
    # (ORDER BY created_at ASC, rowid ASC) in the case of a created_at tie —
    # two inserts serialized by _LOCK landing on the same microsecond is
    # unlikely but not impossible, and without a shared tiebreak the writer
    # could chain onto a different "last" row than the verifier computes,
    # producing a false break for every message after the tie.
    row = conn.execute(
        "SELECT chain_hash FROM room_messages WHERE room_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    return row["chain_hash"] if row else None


ChainScope = Literal["genesis", "recent_window"]


class ChainVerification(NamedTuple):
    """What `verify_chain()` actually established, kept apart from what it
    merely read off the newest row.

    `head` and `message_count` are the *attested* facts — a chain head and a
    total, both exact, both cheap, and both derived from indexed lookups rather
    than from the walk. They are what `GET /rooms/{id}/chain` signs and what a
    client pins.

    `verified_count` and `scope` describe the walk itself, and exist because
    P44 bounded it: a capped call cannot honestly report the same thing an
    uncapped one does, and a single `ok` flag has nowhere to say so.
    """

    ok: bool
    message: str
    head: str | None
    message_count: int
    verified_count: int
    scope: ChainScope


def verify_chain(
    room_id: str,
    db_path: Path | None = None,
    limit: int | None = None,
) -> ChainVerification:
    """Recompute a room's message chain and compare against each stored
    `chain_hash` — the verifier `chain_hash` never had (docs/security-log.md recorded it
    as "computed and stored, verified nowhere" since P18).

    Unlike `session_audit.verify_chain()`, which recomputes from *stored* fields
    it has no independent check on, a mismatch here is genuinely meaningful only
    combined with something the relay can't also forge — see the route's own
    docstring for why the signature this feeds is necessary but not sufficient,
    and what closes that gap.

    `limit` bounds the walk to the newest `limit` messages; `None` (the default)
    walks the whole history. P44 added it after measuring the uncapped walk at
    70ms for a 20,000-message room — the previous docstring called that "cheap
    even for a large room", which was wrong by two orders of magnitude, and the
    route serving it is open to any connection for a public room. The default
    stays uncapped deliberately: an offline audit wants the whole chain, and the
    cap is a *route* policy (`relay_server.CHAIN_VERIFY_LIMIT`) applied where the
    untrusted caller is, not a truncation this function does behind a caller's
    back.

    A bounded walk fetches `limit + 1` rows so the window's own oldest row can
    seed `prev_hash`; that seed row is taken on trust and is not itself counted
    as verified. `scope` says which happened — `"genesis"` when the walk reached
    the room's true first message, `"recent_window"` when it did not — because a
    bare `ok=True` from a window would claim history the call never read. Same
    reasoning P28-4 applied to the DM unchained prefix.

    Mismatch indices are absolute (an index into the room, not an offset into
    the window), so the same message gets the same number at any cap.
    """
    with _LOCK, _connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM room_messages WHERE room_id = ?", (room_id,),
        ).fetchone()["n"]
        if total == 0:
            return ChainVerification(
                True, "empty room — nothing to verify", None, 0, 0, "genesis",
            )
        if limit is None or total <= limit:
            rows = conn.execute(
                "SELECT sender_id, content, created_at, chain_hash, deleted_at FROM room_messages "
                "WHERE room_id = ? ORDER BY created_at ASC, rowid ASC",
                (room_id,),
            ).fetchall()
        else:
            # Newest-first with a LIMIT is the only shape the (room_id, created_at)
            # index can serve cheaply; reverse it back to the ascending order the
            # recompute needs. The rowid tiebreak mirrors _last_chain_hash() (P21-7).
            rows = conn.execute(
                "SELECT sender_id, content, created_at, chain_hash, deleted_at FROM room_messages "
                "WHERE room_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (room_id, limit + 1),
            ).fetchall()
            rows.reverse()

    head = rows[-1]["chain_hash"]
    at_genesis = limit is None or total <= limit
    if at_genesis:
        prev_hash: str | None = None
        walk, first_index, scope = rows, 0, "genesis"
    else:
        prev_hash = rows[0]["chain_hash"]
        walk, first_index, scope = rows[1:], total - len(rows) + 1, "recent_window"

    for offset, row in enumerate(walk):
        if not link_ok(prev_hash, row):
            return ChainVerification(
                False, f"hash mismatch at message {first_index + offset}",
                None, total, offset, scope,
            )
        prev_hash = row["chain_hash"]

    verified = len(walk)
    detail = "chain valid" if scope == "genesis" else "recent window valid"
    return ChainVerification(
        True, f"{detail} ({verified} of {total} messages)", head, total, verified, scope,
    )


# ============================================================================
# Room CRUD
# ============================================================================

def create_room(
    name: str,
    creator_id: str,
    room_type: RoomType = "public",
    gate_type: GateType = "open",
    entry_fee_cents: int = 0,
    currency: str = "USD",
    description: str = "",
    max_members: int | None = None,
    encrypted: bool = False,
    linked_session_id: str | None = None,
    access_duration_days: int | None = None,
    db_path: Path | None = None,
) -> Room:
    if room_type == "public" and gate_type != "open":
        raise ValueError("Public rooms must use gate_type='open'")
    if gate_type == "paid" and entry_fee_cents <= 0:
        raise ValueError("paid gate requires entry_fee_cents > 0")
    if access_duration_days is not None and access_duration_days <= 0:
        raise ValueError("access_duration_days must be a positive integer or None")
    if encrypted and max_members and max_members > effective_max_members(None, True):
        # P28-2: refuse rather than silently clamp. The creator asked for a room
        # size this format cannot deliver, and finding that out at member 513 --
        # when the room is already in use -- is strictly worse than here.
        raise ValueError(
            f"encrypted rooms are capped at {effective_max_members(None, True)} members "
            f"(requested {max_members}) — every message wraps the content key once per "
            "member, so a larger room would exceed the per-message size limit on every send"
        )

    room_id = f"room_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    members = [creator_id]

    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO rooms (room_id, name, description, room_type, gate_type, "
            "entry_fee_cents, currency, creator_id, members_json, max_members, "
            "encrypted, linked_session_id, created_at, updated_at, "
            "access_duration_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (room_id, name, description, room_type, gate_type,
             entry_fee_cents, currency, creator_id, json.dumps(members),
             max_members, int(encrypted), linked_session_id, now, now,
             access_duration_days),
        )
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    return _row_to_room(row)


def get_room(room_id: str, db_path: Path | None = None) -> Room | None:
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    return _row_to_room(row) if row else None


def get_room_for_session(session_id: str, db_path: Path | None = None) -> Room | None:
    """Return the room linked to a game session, if any."""
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM rooms WHERE linked_session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
    return _row_to_room(row) if row else None


def list_rooms(
    viewer_id: str | None = None,
    room_type: RoomType | None = None,
    limit: int = 50,
    db_path: Path | None = None,
) -> list[Room]:
    """List rooms visible to viewer_id.

    Public rooms: always visible.
    Private rooms: visible only if viewer_id has *active* access (P2r-4, R4)
    — see `_access_active()`. For most rooms (gate_type != "paid")
    this is a plain membership check with no extra query; an entitlement
    lookup only happens for the private paid rooms in the listed page, not
    the whole table, so this stays a single extra round-trip per *paid* room
    shown rather than per room total.
    """
    with _LOCK, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM rooms ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    result = []
    for row in rows:
        room = _row_to_room(row)
        if room_type and room.room_type != room_type:
            continue
        if room.room_type == "public":
            result.append(room)
        elif viewer_id and _access_active(
            room.room_id, room.gate_type, room.creator_id, room.members, viewer_id, db_path=db_path
        ):
            result.append(room)
    return result


def _access_active(
    room_id: str,
    gate_type: str,
    creator_id: str,
    members: list[str],
    agent_id: str,
    db_path: Path | None = None,
) -> bool:
    """The expiry-aware access check itself (P2-5), factored out of
    `is_member()` so a caller that already has a `Room` loaded —
    `list_rooms()`, `Room.to_dict()` (P2r-4, R4) — doesn't re-fetch the row
    from `rooms` just to ask the same question `is_member()` would. Those two
    callers previously compared against `room.members` directly, bypassing
    this check entirely: a private paid room's expired member still showed up
    in `GET /rooms` with the room's full member roster, even though
    `GET /rooms/{id}` correctly 403'd them — the single-enforcement-point
    property `is_member()`'s own docstring claims wasn't actually true.

    For a `paid` room, membership requires an *active* entitlement
    — the whole point of `access_duration_days` is that a window that closes
    actually closes, not just that it stops future free rejoins (P2-4). Two
    exemptions, both intentional: the room creator (never paid to be there,
    can't expire out of their own room) and, for anyone else, an agent with
    no entitlement row at all for this resource — see
    `entitlements.has_any_grant()`'s docstring for why "predates
    entitlements" must never be conflated with "entitlement lapsed."

    For any other gate_type, this is a pure in-memory check — no entitlement
    query, hence no extra DB round-trip — since only `paid` rooms
    have an expiry concept at all.
    """
    # P59: the rule itself now lives in `entitlements.access_active()`, shared
    # with the Agora subscribe path, which had the same algebra open-coded in
    # its handler. This stays as the room-shaped door onto it — three callers
    # already have a `Room` loaded and should not have to know the gate
    # vocabulary or the creator exemption.
    return entitlements.access_active(
        "room", room_id, agent_id,
        is_member=agent_id in members,
        gate_has_expiry=gate_type == "paid",
        is_exempt=agent_id == creator_id,
        db_path=db_path,
    )


def is_member(room_id: str, agent_id: str, db_path: Path | None = None) -> bool:
    """P2-5: for a `paid` room, membership requires an *active*
    entitlement. `relay_server.py` has multiple call sites that gate access on
    room membership; routing expiry through this one function (and
    `_access_active()`, shared with `list_rooms()`/`Room.to_dict()`) means
    every one of them enforces it automatically, instead of each needing its
    own copy of the same check.
    """
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT members_json, gate_type, creator_id FROM rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
    if not row:
        return False
    members = json.loads(row["members_json"] or "[]")
    return _access_active(
        room_id, row["gate_type"], row["creator_id"], members, agent_id, db_path=db_path
    )


def effective_max_members(max_members: int | None, encrypted: bool) -> int | None:
    """P28-2: the member limit actually in force — the operator's `max_members`
    and, for an encrypted room, `MAX_ENCRYPTED_ROOM_MEMBERS`, whichever binds
    first.

    An encrypted room's cap is not a policy choice: `crypto_box.seal()` wraps
    the content key once per member, so membership sets a floor on every future
    message's size, and past a threshold the wrap set alone exceeds
    `wire.MAX_CONTENT_BYTES` and the room can never be posted to again. See that
    constant for the derivation. Unencrypted rooms have no such coupling and are
    left entirely to the operator.

    Kept as one function rather than inlined at each check so the join route's
    pre-payment test and `add_member()`'s authoritative one can't disagree about
    what "full" means (P28-1 is what that disagreement costs).
    """
    from emporia.crypto_box import MAX_ENCRYPTED_ROOM_MEMBERS

    if not encrypted:
        return max_members
    if not max_members:
        return MAX_ENCRYPTED_ROOM_MEMBERS
    return min(max_members, MAX_ENCRYPTED_ROOM_MEMBERS)


def room_is_full(room_id: str, db_path: Path | None = None) -> bool:
    """P28-1: is this room at its effective limit? Read-only, so the join route
    can refuse *before* charging an entry fee. `add_member()` re-checks under
    the write lock — this is the early answer, not the authoritative one."""
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT members_json, max_members, encrypted FROM rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
    if not row:
        return False
    cap = effective_max_members(row["max_members"], bool(row["encrypted"]))
    return bool(cap and len(json.loads(row["members_json"] or "[]")) >= cap)


def add_member(room_id: str, agent_id: str, db_path: Path | None = None) -> bool:
    """Add agent to room members. Returns True if added, False if already present."""
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not row:
            return False
        members = json.loads(row["members_json"] or "[]")
        encrypted = bool(row["encrypted"])
        max_members = effective_max_members(row["max_members"], encrypted)
        if agent_id in members:
            return False
        if max_members and len(members) >= max_members:
            # P28-2: name the reason, because the two are fixed by different
            # people — an operator can raise their own `max_members`, but the
            # encrypted limit is a property of the message format and raising it
            # would just move the room's bricking point.
            if encrypted and max_members == effective_max_members(None, True):
                raise ValueError(
                    f"Room is full ({max_members} members) — an encrypted room is "
                    "capped because every message wraps the content key once per "
                    "member, and a larger room could not be posted to at all"
                )
            raise ValueError(f"Room is full ({max_members} members)")
        members.append(agent_id)
        conn.execute(
            "UPDATE rooms SET members_json = ?, updated_at = ? WHERE room_id = ?",
            (json.dumps(members), datetime.now(UTC).isoformat(), room_id),
        )
    return True


def remove_member(room_id: str, agent_id: str, db_path: Path | None = None) -> bool:
    """Remove an agent from a room (kick or leave)."""
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not row:
            return False
        members = json.loads(row["members_json"] or "[]")
        if agent_id not in members:
            return False
        members = [m for m in members if m != agent_id]
        conn.execute(
            "UPDATE rooms SET members_json = ?, updated_at = ? WHERE room_id = ?",
            (json.dumps(members), datetime.now(UTC).isoformat(), room_id),
        )
    return True


def add_invite(
    room_id: str, invitee_id: str, invited_by: str, db_path: Path | None = None
) -> None:
    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO room_invites "
            "(room_id, invitee_id, invited_by, created_at) VALUES (?, ?, ?, ?)",
            (room_id, invitee_id, invited_by, datetime.now(UTC).isoformat()),
        )


def has_invite(room_id: str, agent_id: str, db_path: Path | None = None) -> bool:
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM room_invites WHERE room_id = ? AND invitee_id = ?",
            (room_id, agent_id),
        ).fetchone()
    return row is not None


def remove_invite(room_id: str, agent_id: str, db_path: Path | None = None) -> None:
    """P2-6: called on kick, so a stale invite can't undo the kick — without
    this, an invite-gated room's `has_invite()` check would still pass for a
    just-kicked agent, since add_invite()/has_invite() have no concept of
    revocation of their own. Agora's equivalent (`DELETE FROM agora_invites`)
    already runs unconditionally on every subscribe; this is the room-side
    sibling, run specifically on kick rather than on every join, since a room's
    own has_invite() check has no "consume on use" step to piggyback on."""
    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM room_invites WHERE room_id = ? AND invitee_id = ?",
            (room_id, agent_id),
        )


# ============================================================================
# Messages
# ============================================================================

def post_message(
    room_id: str,
    sender_id: str,
    content: str,
    msg_type: MsgType = "chat",
    parent_message_id: str | None = None,
    signature: str | None = None,
    nonce: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> RoomMessage:
    message_id = f"rmsg_{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(UTC).isoformat()

    with _LOCK, _connect(db_path) as conn:
        prev_hash = _last_chain_hash(room_id, conn)
        chain_hash = compute_chain_hash(prev_hash, sender_id, content, created_at)
        conn.execute(
            "INSERT INTO room_messages "
            "(message_id, room_id, sender_id, msg_type, content, "
            "parent_message_id, chain_hash, signature, nonce, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, room_id, sender_id, msg_type, content,
             parent_message_id, chain_hash, signature, nonce,
             json.dumps(metadata or {}), created_at),
        )
        row = conn.execute(
            "SELECT * FROM room_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    return _row_to_message(row)


def verify_chain_page(
    page: list[Any],
    expected: str | Any,
    at_genesis: bool,
) -> tuple[bool, Any | None, str]:
    """Pure decision core for one page of a *backward* (newest-to-oldest) room
    chain walk (P24-1, W1) — shared by `agent_sdk.py`'s async
    `verify_room_chain()` and `mcp_server.py`'s sync twin, closing the
    duplication risk P24's own audit flagged (P21-7's rowid tiebreak reached
    two of three call sites because the two copies drifted). This is the
    piece `message_read.py`'s own precedent says to extract — each caller
    keeps its own ~10-line fetch loop; only the verification decision moves
    here.

    Exists because a room's chain can no longer be verified in one
    forward pass that loads every message at once (docs/security-log.md recorded that
    a room over the old 1000-message cap "can silently never be verifiable
    from genesis"). Walking backward instead — the only direction the relay's
    own pagination (`get_messages`, `before=created_at`) supports — lets a
    caller hold one page in memory rather than the whole room, at the cost of
    one message per page boundary needing a *deferred* check: a page's own
    oldest message can't be verified against its true predecessor until the
    next (older) page — containing that predecessor — has been fetched.

    `page`: one page of messages, each needing `.sender_id`/`.content`/
    `.created_at`/`.chain_hash` (dict-style `__getitem__` access — works
    against both `RoomMessage.to_dict()` output and the relay's JSON), in
    the same ascending (oldest → newest) order `get_messages()`/
    `GET /rooms/{id}/messages` already return — no caller needs to reverse
    anything.

    `expected` carries state between calls and takes two different shapes
    depending on which check it drives:
      - a bare `chain_hash` string, on the *first* call only: checked for
        direct equality against this page's newest message's own stored
        `chain_hash`. This is a legitimate equality, not a recompute — a
        room's relay-attested `chain_head` *is defined as* its newest
        message's `chain_hash`.
      - a message-shaped value (the *previous*, newer page's oldest message),
        on every call after the first: that message couldn't be verified
        using its own page alone, since its true predecessor lives in the
        page being processed now. This call recomputes
        `compute_chain_hash(<this page's newest>.chain_hash, pending.sender_id,
        pending.content, pending.created_at)` and compares it against
        `pending.chain_hash`.

    Every message strictly between a page's oldest and newest is verified by
    an ordinary adjacent-pair recompute, entirely within this one page.

    Returns `(ok, new_expected, reason)`:
      - `ok=False`: verification failed; `reason` says why (and — for the
        deferred cross-page check specifically — says so distinctly, since a
        failure there can also mean `get_messages`' own known page-boundary
        gap silently dropped a message on a `created_at` tie, not just
        tampering; see that function's docstring and docs/security-log.md); `new_expected`
        is `None` and the caller must stop.
      - `ok=True, at_genesis=False`: `new_expected` is this page's own oldest
        message, deferred for the *next* (older) page to verify.
      - `ok=True, at_genesis=True`: this page reached the room's true first
        message, and it was independently checked against a `None` prev_hash
        (real chain genesis, not merely "ran out of fetch budget").
        `new_expected` is `None`; the walk is complete.
    """
    if not page:
        return False, None, "empty page"

    newest = page[-1]
    if isinstance(expected, str):
        if newest["chain_hash"] != expected:
            return (
                False, None,
                "relay-signed chain head does not match the newest message's "
                "own stored hash — history may have been rewritten",
            )
    else:
        pending = expected
        if not link_ok(newest["chain_hash"], pending):
            return (
                False, None,
                "hash mismatch at a page boundary — either history was "
                "rewritten, or a message was lost at a created_at tie "
                "spanning the page split (see get_messages()'s known "
                "ordering-tiebreak gap)",
            )

    for older, newer in zip(page, page[1:]):
        if not link_ok(older["chain_hash"], newer):
            return False, None, "hash mismatch within a page — history may have been rewritten"

    oldest = page[0]
    if at_genesis:
        if not link_ok(None, oldest):
            return (
                False, None,
                "genesis message does not chain from an empty prior — "
                "chain does not anchor at a true beginning",
            )
        return True, None, f"genesis reached and verified ({len(page)} messages in final page)"
    return True, oldest, f"page verified ({len(page)} messages), boundary check deferred"


def get_messages(
    room_id: str,
    limit: int = 50,
    before: str | None = None,
    db_path: Path | None = None,
    before_seq: int | None = None,
) -> list[RoomMessage]:
    """P31-3: `before` alone is not a total cursor. Two messages can share a
    `created_at` — `post_message()` computes it before taking `_LOCK`, the same
    case P21-7 added the chain writer's `rowid` tiebreak for — and when such a
    pair straddles a page boundary, `created_at < ?` excludes the older one from
    every later page: it can never be fetched, and a client walking the chain
    backward reports an untampered room as unverifiable.

    `before_seq` (a message's `seq`, its rowid) completes the cursor, ordered
    exactly as `verify_chain()` orders. Optional, so non-paginating callers are
    unaffected.
    """
    with _LOCK, _connect(db_path) as conn:
        if before and before_seq is not None:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM room_messages WHERE room_id = ? AND "
                "(created_at < ? OR (created_at = ? AND rowid < ?)) "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (room_id, before, before, before_seq, limit),
            ).fetchall()
        elif before:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM room_messages WHERE room_id = ? AND created_at < ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (room_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT *, rowid AS seq FROM room_messages WHERE room_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
    return [_row_to_message(row) for row in reversed(rows)]
