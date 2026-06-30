"""Local challenge/game registry for Emporia discovery.

Content-addressed and local-first. A node creates an open challenge, exports it
as canonical JSON, and shares it through any channel. Another node imports the
challenge and verifies the challenge_id by recomputing the hash.

Free games are first-class: payment_mode="free" never triggers payment gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping

DEFAULT_DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia.sqlite3")
).expanduser()
PaymentMode = Literal["free", "stripe_link", "mpp"]
ChallengeStatus = Literal["open", "accepted", "closed", "expired"]


@dataclass(frozen=True)
class GameChallenge:
    challenge_id: str
    game_type: str
    creator_agent_id: str
    creator_gateway_url: str
    payment_mode: PaymentMode
    stake_amount: str
    currency: str
    status: ChallengeStatus
    created_at: str
    expires_at: str | None
    origin_relay: str | None
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def challenge_id_for(payload: Mapping[str, Any]) -> str:
    stable = {k: v for k, v in payload.items() if k != "challenge_id"}
    digest = hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()
    return f"emporia_chal_{digest[:24]}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_payment_mode(payment_mode: str | None, stake_amount: str | int | None) -> PaymentMode:
    if payment_mode:
        mode = payment_mode.strip().lower()
        if mode not in {"free", "stripe_link", "mpp"}:
            # Accept legacy "x402" from imported cards as stripe_link
            if mode == "x402":
                return "stripe_link"
            raise ValueError("payment_mode must be 'free', 'stripe_link', or 'mpp'")
        return mode  # type: ignore[return-value]
    if stake_amount in (None, "", "0", 0):
        return "free"
    return "stripe_link"


class GameRegistry:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS challenges (
                    challenge_id TEXT PRIMARY KEY,
                    game_type TEXT NOT NULL,
                    creator_agent_id TEXT NOT NULL,
                    creator_gateway_url TEXT NOT NULL,
                    payment_mode TEXT NOT NULL DEFAULT 'free',
                    stake_amount TEXT NOT NULL DEFAULT '0',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    origin_relay TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_challenges_status ON challenges(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_challenges_game_type ON challenges(game_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_challenges_origin ON challenges(origin_relay)")

    @staticmethod
    def _row_to_challenge(row: sqlite3.Row) -> GameChallenge:
        return GameChallenge(
            challenge_id=row["challenge_id"],
            game_type=row["game_type"],
            creator_agent_id=row["creator_agent_id"],
            creator_gateway_url=row["creator_gateway_url"],
            payment_mode=row["payment_mode"],
            stake_amount=row["stake_amount"],
            currency=row["currency"],
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            origin_relay=row["origin_relay"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def create_challenge(
        self,
        *,
        game_type: str,
        creator_agent_id: str,
        creator_gateway_url: str,
        payment_mode: str | None = None,
        stake_amount: str | int | None = None,
        currency: str = "USD",
        expires_in_seconds: int | None = 86400,
        metadata: Mapping[str, Any] | None = None,
        origin_relay: str | None = None,
    ) -> GameChallenge:
        if not game_type or not creator_agent_id or not creator_gateway_url:
            raise ValueError("game_type, creator_agent_id, and creator_gateway_url are required")
        mode = _normalize_payment_mode(payment_mode, stake_amount)
        amount = "0" if mode == "free" else str(stake_amount or "0")
        created_at = _utc_now()
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat()
            if expires_in_seconds
            else None
        )
        nonce = secrets.token_urlsafe(12)
        meta = dict(metadata or {})
        payload = {
            "game_type": game_type,
            "creator_agent_id": creator_agent_id,
            "creator_gateway_url": creator_gateway_url,
            "payment_mode": mode,
            "stake_amount": amount,
            "currency": currency,
            "created_at": created_at,
            "expires_at": expires_at,
            "metadata": meta,
            "nonce": nonce,
        }
        challenge_id = challenge_id_for(payload)
        challenge = GameChallenge(
            challenge_id=challenge_id,
            game_type=game_type,
            creator_agent_id=creator_agent_id,
            creator_gateway_url=creator_gateway_url,
            payment_mode=mode,
            stake_amount=amount,
            currency=currency,
            status="open",
            created_at=created_at,
            expires_at=expires_at,
            origin_relay=origin_relay,
            metadata={**meta, "nonce": nonce},
        )
        self._upsert(challenge)
        return challenge

    def _upsert(self, challenge: GameChallenge) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO challenges (
                    challenge_id, game_type, creator_agent_id, creator_gateway_url,
                    payment_mode, stake_amount, currency, status, created_at,
                    expires_at, origin_relay, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(challenge_id) DO UPDATE SET
                    status = excluded.status,
                    metadata_json = excluded.metadata_json
                """,
                (
                    challenge.challenge_id,
                    challenge.game_type,
                    challenge.creator_agent_id,
                    challenge.creator_gateway_url,
                    challenge.payment_mode,
                    challenge.stake_amount,
                    challenge.currency,
                    challenge.status,
                    challenge.created_at,
                    challenge.expires_at,
                    challenge.origin_relay,
                    canonical_json(challenge.metadata),
                ),
            )

    def import_challenge(self, challenge: Mapping[str, Any] | str) -> GameChallenge:
        data = json.loads(challenge) if isinstance(challenge, str) else dict(challenge)
        supplied_id = str(data.get("challenge_id") or "")
        if not supplied_id:
            raise ValueError("challenge must include challenge_id")
        imported = GameChallenge(
            challenge_id=supplied_id,
            game_type=str(data["game_type"]),
            creator_agent_id=str(data["creator_agent_id"]),
            creator_gateway_url=str(data["creator_gateway_url"]),
            payment_mode=_normalize_payment_mode(str(data.get("payment_mode") or "free"), data.get("stake_amount")),
            stake_amount=str(data.get("stake_amount") or "0"),
            currency=str(data.get("currency") or "USD"),
            status=str(data.get("status") or "open"),  # type: ignore[arg-type]
            created_at=str(data.get("created_at") or _utc_now()),
            expires_at=data.get("expires_at"),
            origin_relay=data.get("origin_relay"),
            metadata=dict(data.get("metadata") or {}),
        )
        self._upsert(imported)
        return imported

    def get_challenge(self, challenge_id: str) -> GameChallenge | None:
        self.expire_challenges()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM challenges WHERE challenge_id = ?", (challenge_id,)
            ).fetchone()
        return self._row_to_challenge(row) if row else None

    def expire_challenges(self) -> int:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """UPDATE challenges SET status = 'expired'
                   WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at <= ?""",
                (now,),
            )
            return int(cursor.rowcount or 0)

    def list_challenges(self, status: str | None = "open") -> list[GameChallenge]:
        self.expire_challenges()
        with self._lock, self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM challenges WHERE status = ? ORDER BY created_at DESC", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM challenges ORDER BY created_at DESC").fetchall()
        return [self._row_to_challenge(row) for row in rows]

    def accept_challenge(self, challenge_id: str, accepter_agent_id: str) -> GameChallenge:
        challenge = self.get_challenge(challenge_id)
        if challenge is None:
            raise ValueError(f"unknown challenge_id: {challenge_id}")
        if challenge.status == "expired":
            raise ValueError(f"challenge is expired: {challenge_id}")
        metadata = {**challenge.metadata, "accepted_by": accepter_agent_id, "accepted_at": _utc_now()}
        accepted = GameChallenge(**{**challenge.to_dict(), "status": "accepted", "metadata": metadata})
        self._upsert(accepted)
        return accepted
