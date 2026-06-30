"""Emporia chat/collab rooms.

Two access tiers:
  public   — visible to all; any registered agent can join; gate_type='open'
  private  — hidden from listing unless member; gated by:
               invite          — creator explicitly invites agent_ids
               stripe_payment  — agent pays entry_fee_cents via Stripe PaymentIntent

Three message types: chat, collab (structured work notes), code (fenced snippet).

Rooms link to sessions: a game session can have an associated room for negotiation
and commentary running alongside the formal move sequence.

Encryption flag:
  encrypted=False (default) — relay-enforced access control; guardrails run on content
  encrypted=True            — relay stores opaque client-ciphertext; skips guardrails scan;
                              checks membership only. Clients encrypt before POST, decrypt
                              after GET. Full X25519 ECDH key exchange is post-v1.

Negotiation routing:
  msg_type='counter_offer' in a 2-member private room triggers negotiation.process_offer()
  automatically. The relay responds with the broker's ACCEPT or COUNTER decision as a
  system message in the same room.

Message chain integrity:
  Each message hashes (prev_hash:sender:content:created_at) → chain_hash, so room
  history tampering is detectable by recomputing the chain.
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

ROOM_DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia_relay.sqlite3")
).expanduser()

_LOCK = threading.RLock()

RoomType = Literal["public", "private"]
GateType = Literal["open", "invite", "stripe_payment"]
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
    updated_at TEXT NOT NULL
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

    def to_dict(self, *, viewer_id: str | None = None) -> dict[str, Any]:
        d = asdict(self)
        # Hide full member list for private rooms unless viewer is a member
        if self.room_type == "private" and viewer_id not in self.members:
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
    db_path: Path | None = None,
) -> Room:
    if room_type == "public" and gate_type != "open":
        raise ValueError("Public rooms must use gate_type='open'")
    if gate_type == "stripe_payment" and entry_fee_cents <= 0:
        raise ValueError("stripe_payment gate requires entry_fee_cents > 0")

    room_id = f"room_{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    members = [creator_id]

    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO rooms (room_id, name, description, room_type, gate_type, "
            "entry_fee_cents, currency, creator_id, members_json, max_members, "
            "encrypted, linked_session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (room_id, name, description, room_type, gate_type,
             entry_fee_cents, currency, creator_id, json.dumps(members),
             max_members, int(encrypted), linked_session_id, now, now),
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
    Private rooms: visible only if viewer_id is a member.
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
        elif viewer_id and viewer_id in room.members:
            result.append(room)
    return result


def is_member(room_id: str, agent_id: str, db_path: Path | None = None) -> bool:
    with _LOCK, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT members_json FROM rooms WHERE room_id = ?", (room_id,)
        ).fetchone()
    if not row:
        return False
    return agent_id in json.loads(row["members_json"] or "[]")


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
            "parent_message_id, chain_hash, signature, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, room_id, sender_id, msg_type, content,
             parent_message_id, chain_hash, signature,
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
