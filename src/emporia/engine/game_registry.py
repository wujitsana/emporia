"""Local challenge/game registry for Emporia discovery.

Content-addressed and local-first. A node creates an open challenge, exports it
as canonical JSON, and shares it through any channel. Another node imports the
challenge and verifies the challenge_id by recomputing the hash — over the record
that will actually be stored, so a re-exported stored row always re-verifies (N14-1;
see challenge_address()).

Free games are first-class: payment_mode="free" never triggers payment gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping

DEFAULT_DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia.sqlite3")
).expanduser()
PaymentMode = Literal["free", "stripe_link", "mpp"]
ChallengeStatus = Literal["open", "accepted", "closed", "expired"]

_UNSET: Any = object()  # distinguishes "no filter" from an explicit origin_relay=None filter


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


def challenge_address(challenge: GameChallenge) -> str:
    """The content address of a challenge *record* — the single definition of what the
    address covers (N14-1).

    Derived from create_challenge()'s own hashed payload, which is the only authority:
    every GameChallenge field except `challenge_id` (excluded by challenge_id_for),
    `status`, and `origin_relay` — the three that are deliberately mutable so a
    legitimate status change or a peer-origin stamp keeps the same address. `metadata`
    is hashed *without* the nonce and stored *with* it merged in, so the nonce is split
    back out here.

    Both create_challenge() and import_challenge() address records through this one
    function, so "the id we mint", "the id we verify against", and "the content we
    store" cannot diverge. Before N14-1 they could: import_challenge() hashed the raw
    incoming payload but stored a normalized one, so a row whose payment_mode/currency/
    created_at got defaulted or rewritten on the way in verified exactly once — at
    import — and never again.
    """
    metadata = dict(challenge.metadata or {})
    nonce = metadata.pop("nonce", None)
    return challenge_id_for(
        {
            "game_type": challenge.game_type,
            "creator_agent_id": challenge.creator_agent_id,
            "creator_gateway_url": challenge.creator_gateway_url,
            "payment_mode": challenge.payment_mode,
            "stake_amount": challenge.stake_amount,
            "currency": challenge.currency,
            "created_at": challenge.created_at,
            "expires_at": challenge.expires_at,
            "metadata": metadata,
            "nonce": nonce,
        }
    )


def _challenge_record_from_payload(
    data: Mapping[str, Any], *, challenge_id: str, origin_relay: str | None
) -> GameChallenge:
    """Build the exact record that would be *stored* for `data`, normalization and
    defaults applied (N14-1).

    Shared by import_challenge() and _recompute_challenge_id() so verification always
    hashes the record that will actually be written, never the raw payload it came from.
    `created_at` is required rather than defaulted to now(): it is part of the address,
    so defaulting it would make the resulting id nondeterministic and turn a missing
    field into a baffling content-mismatch instead of a clear one.
    """
    if not data.get("created_at"):
        raise ValueError("challenge must include created_at — it is part of the content address")
    mode = _normalize_payment_mode(str(data.get("payment_mode") or "free"), data.get("stake_amount"))
    return GameChallenge(
        challenge_id=challenge_id,
        game_type=str(data["game_type"]),
        creator_agent_id=str(data["creator_agent_id"]),
        creator_gateway_url=str(data["creator_gateway_url"]),
        payment_mode=mode,
        stake_amount=_normalize_stake_amount(mode, data.get("stake_amount")),
        currency=str(data.get("currency") or "USD"),
        status=str(data.get("status") or "open"),  # type: ignore[arg-type]
        created_at=str(data["created_at"]),
        expires_at=data.get("expires_at"),
        origin_relay=origin_relay,
        metadata=dict(data.get("metadata") or {}),
    )


def _recompute_challenge_id(data: Mapping[str, Any]) -> str:
    """Content address of the record `data` would be stored as. Thin wrapper over
    challenge_address() — kept because callers (and tests) hold raw dicts rather than
    GameChallenge records."""
    return challenge_address(
        _challenge_record_from_payload(data, challenge_id="", origin_relay=None)
    )


class ChallengeOwnershipError(ValueError):
    """Raised by import_challenge() when a challenge_id collides with an existing row
    it isn't entitled to overwrite (N12-3, R3) — distinct from ValueError so callers that
    care about the difference (POST /gaming/lobby maps this to 403, not the generic
    422 an id-verification or malformed-payload failure gets) can catch it specifically.
    """


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_payment_mode(payment_mode: str | None, stake_amount: str | int | None) -> PaymentMode:
    if payment_mode:
        mode = payment_mode.strip().lower()
        if mode not in {"free", "stripe_link", "mpp"}:
            # P1a-4 (R4): "x402" no longer aliases to stripe_link. The x402 protocol
            # (HTTP-402-native, stablecoin-settled — https://x402.org) is not the same
            # thing this relay's "stripe_link" mode settles, and the alias predates
            # that distinction being drawn. No backwards-compat kept for it, per the
            # user: a legacy card claiming "x402" was already unimportable as of
            # N14-1 (payment_mode is part of the content address, so a card whose own
            # id was computed over "x402" fails verification against the "stripe_link"
            # this alias used to normalize it to).
            #
            # P1b: x402 IS a real, settleable rail on this relay now — this rejection
            # is not "not implemented yet" anymore. It's scoped: `payment_mode` governs
            # *session escrow* (capture_method="manual", hold-then-conditionally-
            # release-minus-fee), and x402's request-scoped settlement model has no
            # analogue for that flow — the payment *is* the settlement, there's no
            # "hold" to later capture or release. x402 settles the two pay-to-access
            # routes instead (`POST /rooms/{id}/join`, `POST /agoras/topics/{slug}/
            # subscribe`), reached via the `PAYMENT-SIGNATURE` header, never through
            # this `payment_mode` vocabulary at all.
            if mode == "x402":
                raise ValueError(
                    "payment_mode 'x402' is not valid here — x402 settles room/Agora "
                    "pay-to-access joins (see PAYMENT-SIGNATURE), not session escrow; "
                    "payment_mode must be 'free', 'stripe_link', or 'mpp'"
                )
            raise ValueError("payment_mode must be 'free', 'stripe_link', or 'mpp'")
        return mode  # type: ignore[return-value]
    if stake_amount in (None, "", "0", 0):
        return "free"
    return "stripe_link"


def _normalize_stake_amount(mode: PaymentMode, stake_amount: str | int | None) -> str:
    """A free challenge always stores stake_amount="0" (P1a-5, R5) — this was
    create_challenge()'s own rule, but _challenge_record_from_payload() didn't apply
    it, so a hand-built payload could mint payment_mode="free" with a nonzero
    stake_amount, importable and re-verifiable, but not mintable through
    create_challenge() itself: the two record constructors could disagree on a field
    N14-1 unified everything else around. Inert today (free mode never triggers a
    payment gate regardless of stake_amount), but it's exactly the class of mint/
    import divergence N14-1 exists to eliminate, one field further in — sharing this
    with create_challenge() means it can't reopen the same way twice.
    """
    return "0" if mode == "free" else str(stake_amount or "0")


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
        amount = _normalize_stake_amount(mode, stake_amount)
        created_at = _utc_now()
        expires_at = (
            (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat()
            if expires_in_seconds
            else None
        )
        nonce = secrets.token_urlsafe(12)
        meta = dict(metadata or {})
        # N14-1: address the fully-built record via challenge_address() rather than
        # hashing a separately-assembled payload here. The old shape kept two field
        # lists — one hashed, one stored — that had to be maintained in agreement by
        # hand; routing both through one function is what makes "a stored row always
        # re-verifies" structural instead of a convention.
        draft = GameChallenge(
            challenge_id="",
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
        challenge = replace(draft, challenge_id=challenge_address(draft))
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

    def import_challenge(
        self,
        challenge: Mapping[str, Any] | str,
        *,
        verify_id: bool = True,
        trusted_origin: str | None = None,
    ) -> GameChallenge:
        """N12-3 (R3): the ownership rule that used to live only in POST /gaming/lobby's
        handler now lives here, so all four importers (that route, _pull_federated_lobby,
        MCP import_challenge, MCP sync_lobby_from_peer) share one definition instead of
        one enforcing it and three not.

        `trusted_origin`, set only by the two peer-pull call sites (to the signature-
        verified peer_url they just pulled from), closes a gap content-addressing alone
        can't: `status` and `origin_relay` are deliberately excluded from the hash (so a
        legitimate status update keeps its id), which means a peer can replay a locally-
        or foreign-owned challenge's exact content with only `status` flipped and still
        pass id verification *and* the creator_agent_id check below, since it's copying
        the real creator's name verbatim, not claiming a different one. Rejecting any
        update where the existing row's origin_relay doesn't match the peer we're
        currently pulling from closes that, while still letting a peer refresh the
        status of challenges it legitimately owns.

        Ownership is checked *before* id verification, not after: content-addressing
        means a payload claiming an existing id under a genuinely different
        creator_agent_id can never verify anyway (creator_agent_id is itself hashed),
        so an ownership violation on a forged id would otherwise surface as a generic
        "content does not match" instead of the more specific "wrong owner" — checking
        ownership first keeps that distinction, on the raw claimed fields, without
        needing the id to be valid first.

        N14-1 (R1): the record is built *before* verification, and verification hashes
        that record — not the raw payload it came from. The old order (hash the payload,
        then build a normalized record from it) meant any field normalization or
        defaulting applied on the way in produced a stored row whose own content no
        longer hashed to its stored id: it verified exactly once, at import, and never
        again, which silently broke the export→import chain this module exists for.
        Verifying what will actually be stored makes "a stored row always re-verifies"
        an invariant rather than an aspiration.

        N14-2 (R2): returns the row re-read from storage after _upsert, not the
        `imported` record built above. `_upsert`'s `ON CONFLICT DO UPDATE` only touches
        `status` and `metadata_json` — deliberately: `origin_relay` stays whatever it was
        set to on the row's first insert. A conflicting import can legitimately change
        `status` (e.g. a peer refreshing one of its own challenges), but content-
        addressing plus the ownership checks above mean it can never legitimately need
        to change who a challenge is attributed to (creator_agent_id, hashed) or where it
        came from (origin_relay, checked against trusted_origin above whenever that's
        known) — so nothing depends on origin_relay being conflict-updatable, and
        leaving it out keeps a locally-owned row (origin_relay=None, no trusted_origin to
        check it against — e.g. a same-creator repost through POST /gaming/lobby) from
        having its origin silently rewritten by whatever the payload happens to claim.
        Before this fix the method returned the object it *constructed*, which on a
        conflicting import could claim an origin_relay the DB never actually wrote.
        """
        data = json.loads(challenge) if isinstance(challenge, str) else dict(challenge)
        supplied_id = str(data.get("challenge_id") or "")
        if not supplied_id:
            raise ValueError("challenge must include challenge_id")
        with self._lock:
            existing = self.get_challenge(supplied_id)
            if existing is not None:
                new_creator = str(data.get("creator_agent_id") or "")
                if existing.creator_agent_id != new_creator:
                    raise ChallengeOwnershipError(
                        f"challenge_id '{supplied_id}' belongs to a different creator"
                    )
                if trusted_origin is not None and existing.origin_relay != trusted_origin:
                    raise ChallengeOwnershipError(
                        f"challenge_id '{supplied_id}' is owned by a different origin "
                        f"relay ({existing.origin_relay!r}) — a peer batch may not "
                        "overwrite content it doesn't own"
                    )
            # N12-4 (R2a): when we independently know who we're pulling from
            # (trusted_origin, set by the two peer-pull call sites to the
            # signature-verified peer_url), that's authoritative over whatever
            # origin_relay the payload itself claims — including claiming none at
            # all. Trusting a self-reported origin_relay here is how an imported
            # challenge could end up with origin_relay=None (indistinguishable from
            # a genuinely local one) and then get re-served, signed, by
            # federated_lobby_out as if authored on this relay. origin_relay is not
            # part of the address, so overriding it here can't affect verification.
            imported = _challenge_record_from_payload(
                data,
                challenge_id=supplied_id,
                origin_relay=(
                    trusted_origin if trusted_origin is not None else data.get("origin_relay")
                ),
            )
            if verify_id:
                expected_id = challenge_address(imported)
                if expected_id != supplied_id:
                    raise ValueError(
                        f"challenge_id does not match its content — expected {expected_id}, "
                        f"got {supplied_id} (the batch may be tampered or corrupt)"
                    )
            self._upsert(imported)
            stored = self.get_challenge(imported.challenge_id)
            if stored is None:
                raise RuntimeError(
                    f"import_challenge: '{imported.challenge_id}' vanished immediately "
                    "after being upserted — this should be unreachable"
                )
        return stored

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

    def list_challenges(
        self,
        status: str | None = "open",
        *,
        origin_relay: str | None = _UNSET,  # type: ignore[assignment]
        limit: int | None = None,
    ) -> list[GameChallenge]:
        """`origin_relay` is unset by default (no filter — every caller before N12-4 gets
        every challenge regardless of origin, e.g. the human/dashboard-facing
        GET /gaming/lobby and the MCP list_challenges tool). Pass it explicitly —
        including `None` — to filter: `origin_relay=None` means "genuinely local, never
        imported," which is the invariant this schema actually uses (unlike listings,
        nothing ever stamps a challenge's origin_relay to this relay's own
        RELAY_BASE_URL at creation — see import_challenge()'s trusted_origin handling),
        so it needs SQL's NULL-safe `IS`, not `=`, to match correctly.
        """
        self.expire_challenges()
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if origin_relay is not _UNSET:
            clauses.append("origin_relay IS ?")
            params.append(origin_relay)
        query = "SELECT * FROM challenges"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
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
