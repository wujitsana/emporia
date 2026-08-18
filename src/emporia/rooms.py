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
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from emporia import entitlements

ROOM_DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia_relay.sqlite3")
).expanduser()

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
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_room_messages_room ON room_messages(room_id);
CREATE INDEX IF NOT EXISTS idx_room_messages_created ON room_messages(room_id, created_at);
CREATE INDEX IF NOT EXISTS idx_room_invites_invitee ON room_invites(invitee_id);
CREATE INDEX IF NOT EXISTS idx_rooms_type ON rooms(room_type);
CREATE INDEX IF NOT EXISTS idx_rooms_session ON rooms(linked_session_id);
"""


def init_rooms_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ROOMS_DDL)


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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("metadata", None)
        d["metadata"] = self.metadata
        return d


# ============================================================================
# DB helpers
# ============================================================================

def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or ROOM_DB_PATH
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
    )


def _compute_chain_hash(
    prev_hash: str | None,
    sender_id: str,
    content: str,
    created_at: str,
) -> str:
    frame = f"{prev_hash or 'GENESIS'}:{sender_id}:{content}:{created_at}"
    return hashlib.sha256(frame.encode()).hexdigest()


def _last_chain_hash(room_id: str, conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT chain_hash FROM room_messages WHERE room_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (room_id,),
    ).fetchone()
    return row["chain_hash"] if row else None


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
    if agent_id not in members:
        return False
    if gate_type != "paid" or agent_id == creator_id:
        return True
    if entitlements.has_access("room", room_id, agent_id, db_path=db_path):
        return True
    return not entitlements.has_any_grant("room", room_id, agent_id, db_path=db_path)


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


def add_member(room_id: str, agent_id: str, db_path: Path | None = None) -> bool:
    """Add agent to room members. Returns True if added, False if already present."""
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not row:
            return False
        members = json.loads(row["members_json"] or "[]")
        max_members = row["max_members"]
        if agent_id in members:
            return False
        if max_members and len(members) >= max_members:
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
        chain_hash = _compute_chain_hash(prev_hash, sender_id, content, created_at)
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


def get_messages(
    room_id: str,
    limit: int = 50,
    before: str | None = None,
    db_path: Path | None = None,
) -> list[RoomMessage]:
    with _LOCK, _connect(db_path) as conn:
        if before:
            rows = conn.execute(
                "SELECT * FROM room_messages WHERE room_id = ? AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (room_id, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM room_messages WHERE room_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (room_id, limit),
            ).fetchall()
    return [_row_to_message(row) for row in reversed(rows)]
