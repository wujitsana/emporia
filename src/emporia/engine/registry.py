"""Universal identity registry for Emporia.

Maps agent names to deterministic content-addressable player IDs:

    alice_1a2b3c4d
    alice_1a2b3c4d_1   (collision suffix for hash prefix clashes)

When nous_user_id is provided, the same Nous user resolves to the same
player_id across machines (partial UNIQUE index enforces this).

No 0x prefix — Emporia is blockchain-free.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from emporia.identity import content_address_for

DEFAULT_DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia.sqlite3")
).expanduser()
_HANDLE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PlayerIdentity:
    id: str
    profile_name: str
    normalized_name: str
    content_address: str
    short_address: str
    collision_index: int
    gateway_url: str = ""
    public_key_hex: str = ""
    nous_user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_profile_name(profile_name: str) -> str:
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise ValueError("profile_name must be a non-empty string")
    normalized = _HANDLE_RE.sub("_", profile_name.strip().lower()).strip("_")
    return normalized or "player"


class IdentityRegistry:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identities (
                    id TEXT PRIMARY KEY,
                    profile_name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    identity_material TEXT NOT NULL,
                    content_address TEXT NOT NULL,
                    short_address TEXT NOT NULL,
                    collision_index INTEGER NOT NULL DEFAULT 0,
                    gateway_url TEXT NOT NULL DEFAULT '',
                    public_key_hex TEXT NOT NULL DEFAULT '',
                    nous_user_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE(normalized_name, identity_material),
                    UNIQUE(normalized_name, short_address, collision_index)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_identities_profile_name ON identities(profile_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_identities_address ON identities(content_address)"
            )
            # Same Nous user on two machines maps to the same player_id
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_nous_user
                   ON identities(nous_user_id)
                   WHERE nous_user_id IS NOT NULL"""
            )

    @staticmethod
    def _row_to_identity(row: sqlite3.Row) -> PlayerIdentity:
        return PlayerIdentity(
            id=row["id"],
            profile_name=row["profile_name"],
            normalized_name=row["normalized_name"],
            content_address=row["content_address"],
            short_address=row["short_address"],
            collision_index=int(row["collision_index"]),
            gateway_url=row["gateway_url"] or "",
            public_key_hex=row["public_key_hex"] or "",
            nous_user_id=row["nous_user_id"],
        )

    def register(
        self,
        player_name: str,
        gateway_url: str = "",
        public_key_hex: str = "",
        nous_user_id: str | None = None,
    ) -> str:
        """Register an agent and return its player_id string.

        This is the primary interface used by the MCP server and relay.
        public_key_hex is the Ed25519 public key for signature verification.
        """
        identity = self._upsert(
            profile_name=player_name,
            identity_material=public_key_hex or player_name,
            gateway_url=gateway_url,
            public_key_hex=public_key_hex,
            nous_user_id=nous_user_id,
        )
        return identity.id

    def register_player(
        self,
        profile_name: str,
        identity_material: str | None = None,
        nous_user_id: str | None = None,
    ) -> PlayerIdentity:
        """Legacy interface: register by name + optional material. Returns full identity."""
        return self._upsert(
            profile_name=profile_name,
            identity_material=identity_material or profile_name,
            nous_user_id=nous_user_id,
        )

    def _upsert(
        self,
        profile_name: str,
        identity_material: str,
        gateway_url: str = "",
        public_key_hex: str = "",
        nous_user_id: str | None = None,
    ) -> PlayerIdentity:
        normalized = normalize_profile_name(profile_name)
        material = identity_material.strip() if identity_material else normalized
        address = content_address_for(profile_name, material)
        short_address = address[:8]

        with self._lock, self._connect() as conn:
            # If nous_user_id provided and already registered, update + return
            if nous_user_id:
                existing = conn.execute(
                    "SELECT * FROM identities WHERE nous_user_id = ?", (nous_user_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE identities SET gateway_url=?, public_key_hex=?, "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                        (gateway_url or existing["gateway_url"],
                         public_key_hex or existing["public_key_hex"],
                         existing["id"]),
                    )
                    row = conn.execute(
                        "SELECT * FROM identities WHERE id=?", (existing["id"],)
                    ).fetchone()
                    return self._row_to_identity(row)

            existing = conn.execute(
                "SELECT * FROM identities WHERE normalized_name = ? AND identity_material = ?",
                (normalized, material),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE identities SET gateway_url=?, public_key_hex=?, nous_user_id=COALESCE(nous_user_id, ?), "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                    (gateway_url or existing["gateway_url"],
                     public_key_hex or existing["public_key_hex"],
                     nous_user_id,
                     existing["id"]),
                )
                row = conn.execute(
                    "SELECT * FROM identities WHERE id=?", (existing["id"],)
                ).fetchone()
                return self._row_to_identity(row)

            occupied = {
                int(row["collision_index"])
                for row in conn.execute(
                    "SELECT collision_index FROM identities WHERE normalized_name = ? AND short_address = ?",
                    (normalized, short_address),
                )
            }
            collision_index = 0
            while collision_index in occupied:
                collision_index += 1

            suffix = "" if collision_index == 0 else f"_{collision_index}"
            player_id = f"{normalized}_{short_address}{suffix}"
            conn.execute(
                """
                INSERT INTO identities (
                    id, profile_name, normalized_name, identity_material,
                    content_address, short_address, collision_index,
                    gateway_url, public_key_hex, nous_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (player_id, profile_name, normalized, material,
                 address, short_address, collision_index,
                 gateway_url, public_key_hex, nous_user_id),
            )
            row = conn.execute("SELECT * FROM identities WHERE id = ?", (player_id,)).fetchone()
            if row is None:
                raise RuntimeError("failed to read identity after insert")
            return self._row_to_identity(row)

    def get_player(self, player_id: str) -> PlayerIdentity | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM identities WHERE id = ?", (player_id,)).fetchone()
        return self._row_to_identity(row) if row else None

    def list_players(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM identities ORDER BY created_at, id LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_identity(row).to_dict() for row in rows]
