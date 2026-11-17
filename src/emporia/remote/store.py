"""The connector's state: one SQLite file in `<key dir>/remote/`, beside the keys.

The folder is 0700 and the file 0600. Tokens, codes and refresh tokens are kept as the SHA-256 of a 256-bit random
value (a fast hash is right for a high-entropy secret); the plaintext exists only in the response that carried it.
No request or response body is stored. The server and the person's CLI are two processes on this one file, so every
call opens its own short connection; a revoke by the CLI is seen by the server on its next request.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

DB_NAME = "connectors.db"
#: An approval the browser has not collected within this long after the request would have expired is void.
APPROVED_GRACE = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(
  client_id TEXT PRIMARY KEY, name TEXT, redirect_uris TEXT, scope TEXT, created_at REAL, last_used REAL);
CREATE TABLE IF NOT EXISTS pending(
  page_hash TEXT PRIMARY KEY, code TEXT UNIQUE, client_id TEXT, redirect_uri TEXT, explicit INTEGER,
  scopes TEXT, state TEXT, challenge TEXT, resource TEXT, created_at REAL, expires_at REAL,
  status TEXT, grant_id TEXT, approved_scopes TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS grants(
  grant_id TEXT PRIMARY KEY, client_id TEXT, name TEXT, scopes TEXT, created_at REAL, expires_at REAL,
  last_used REAL, calls INTEGER DEFAULT 0, revoked_at REAL, revoke_reason TEXT, locked_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS codes(
  hash TEXT PRIMARY KEY, grant_id TEXT, client_id TEXT, redirect_uri TEXT, challenge TEXT, resource TEXT,
  scopes TEXT, expires_at REAL, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS access(
  hash TEXT PRIMARY KEY, grant_id TEXT, client_id TEXT, resource TEXT, scopes TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS refresh(
  hash TEXT PRIMARY KEY, grant_id TEXT, client_id TEXT, resource TEXT, scopes TEXT, expires_at REAL,
  used INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS redirects(url TEXT PRIMARY KEY, added_at REAL);
CREATE TABLE IF NOT EXISTS events(kind TEXT, at REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL);
"""

#: the alphabet of a short approval code: no 0/O, 1/I/L, so it can be read aloud
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"       # 256 bits


def short_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def norm_code(code: str) -> str:
    raw = "".join(c for c in (code or "").upper() if c.isalnum())
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else (code or "").upper()


class Store:
    def __init__(self, folder: str | os.PathLike, clock: Callable[[], float] = time.time) -> None:
        self.folder = Path(folder)
        self.clock = clock
        self.folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        self.path = self.folder / DB_NAME
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(self.path, 0o600)
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    # ── small helpers ─────────────────────────────────────────────────────────────
    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(sql, tuple(args)).fetchone()

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(sql, tuple(args)).fetchall()

    def run(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self.conn() as c:
            return c.execute(sql, tuple(args)).rowcount

    def event(self, kind: str) -> None:
        self.run("INSERT INTO events VALUES(?,?)", (kind, self.clock()))

    def events_since(self, kind: str, seconds: float) -> int:
        row = self.one("SELECT COUNT(*) n FROM events WHERE kind=? AND at>?", (kind, self.clock() - seconds))
        return int(row["n"]) if row else 0

    # ── upkeep ────────────────────────────────────────────────────────────────────
    def purge(self) -> None:
        """Drop what has lapsed: expired requests, codes and access tokens, refresh tokens a week past their end,
        and clients nobody approved within a day."""
        from emporia.remote import CLIENT_UNUSED_TTL
        now = self.clock()
        with self.conn() as c:
            c.execute("DELETE FROM pending WHERE expires_at<? AND status IN ('pending','done','denied','expired')",
                      (now - 3600,))
            c.execute("DELETE FROM pending WHERE expires_at<? AND status='approved'", (now - APPROVED_GRACE,))
            c.execute("UPDATE pending SET status='expired' WHERE expires_at<? AND status='pending'", (now,))
            c.execute("DELETE FROM codes WHERE expires_at<?", (now - 3600,))
            c.execute("DELETE FROM access WHERE expires_at<?", (now - 3600,))
            c.execute("DELETE FROM refresh WHERE expires_at<?", (now - 7 * 24 * 3600,))
            c.execute("DELETE FROM events WHERE at<?", (now - 24 * 3600,))
            c.execute("DELETE FROM clients WHERE created_at<? AND client_id NOT IN (SELECT client_id FROM grants) "
                      "AND client_id NOT IN (SELECT client_id FROM pending WHERE status='pending')",
                      (now - CLIENT_UNUSED_TTL,))
            # a client whose grants are all revoked or expired is done: it no longer counts toward the cap
            c.execute("DELETE FROM clients WHERE client_id IN (SELECT client_id FROM grants GROUP BY client_id "
                      "HAVING SUM(CASE WHEN revoked_at IS NULL AND expires_at>? THEN 1 ELSE 0 END)=0) "
                      "AND client_id NOT IN (SELECT client_id FROM pending WHERE status IN ('pending','approved'))",
                      (now,))

    # ── the pairing window ────────────────────────────────────────────────────────
    def pairing_until(self) -> float:
        row = self.one("SELECT value FROM meta WHERE key='pairing_until'")
        return float(row["value"]) if row else 0.0

    def open_pairing(self, seconds: float) -> float:
        until = self.clock() + seconds
        self.run("INSERT OR REPLACE INTO meta VALUES('pairing_until', ?)", (until,))
        return until

    # ── redirects the person allowed ──────────────────────────────────────────────
    def allowed_redirects(self) -> list[str]:
        return [r["url"] for r in self.all("SELECT url FROM redirects ORDER BY added_at")]

    def allow_redirect(self, url: str) -> None:
        self.run("INSERT OR IGNORE INTO redirects VALUES(?,?)", (url, self.clock()))


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
