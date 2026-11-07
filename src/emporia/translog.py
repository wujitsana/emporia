"""P256: the relay's transparency log — one append-only Merkle log per relay
(docs/proposals/relay-transparency-log.md, step 1).

The relay's hash chains (rooms, DMs, sessions, each agent's key log) prove the
order inside one stream. They do not stop a relay showing one reader one
history and another reader a different one, and proving one entry is in a chain
means handing over the whole chain. This log closes both:

- **Leaves** are records of events — an agent's key change, a payment, the head
  of a room or DM chain, a change of the relay's own key. Never content: a
  private event goes in as a commitment (`commitment()`), which only someone
  holding the record can recompute and look up.
- **Hashing** is RFC 9162's, so the log can be checked with existing tools:
  leaf = SHA-256(0x00 ‖ data), node = SHA-256(0x01 ‖ left ‖ right).
- **Log states** — `{type, relay_public_key, size, root, at}` signed raw with the
  relay key (protocol §4.4) — are what everything else refers to.
- **Proofs**: an inclusion proof shows an entry is in the log of a given size;
  a consistency proof shows a larger log only added to a smaller one.

Each event's leaf is written in the same transaction as the event, so one cannot
exist without the other. Chain heads and relay key changes are folded in when a
state is signed: one leaf per chain that moved, not one per message.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
STATE_TYPE = "emporia:log-state:v1"
STATE_LABEL = b"emporia:log-state:v1\n"
HANDOVER_TYPE = "emporia:log-handover:v1"
HANDOVER_LABEL = b"emporia:log-handover:v1\n"
COMMIT_LABEL = "emporia:log-commit:v2:"


def canonical(obj: Any) -> bytes:
    """Protocol §2's canonical JSON."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def leaf_hash(data: bytes) -> bytes:
    return _sha(LEAF_PREFIX + data)


def node_hash(left: bytes, right: bytes) -> bytes:
    return _sha(NODE_PREFIX + left + right)


def record_leaf_hash(record: dict[str, Any]) -> bytes:
    return leaf_hash(canonical(record))


def commitment(kind: str, fields: dict[str, Any], salt: str = "") -> str:
    """A private event's stand-in: hex SHA-256 over a salt and its fields.

    P263: salted. Before, the hiding rested on each record carrying some id the
    public could not know — and some did not: a public game's participants are
    its (public) session id, an agent id from a short list and a join time a few
    minutes wide, which a GPU guesses in minutes. The salt is per record, made by
    the relay (`record_salt`); whoever is entitled to the record gets it with the
    record (client checks, step 2) and can then recompute the commitment."""
    return hashlib.sha256(COMMIT_LABEL.encode() + kind.encode() + b":" + salt.encode() + b":"
                          + canonical(fields)).hexdigest()


# ─── the tree (RFC 9162 §2.1) ─────────────────────────────────────────────────

def _split(n: int) -> int:
    """The largest power of two smaller than n (n > 1)."""
    return 1 << ((n - 1).bit_length() - 1)


class _Tree:
    """Merkle tree hashes, memoised per subtree. `node(level, i)`, when given,
    answers a complete subtree — leaves [i·2^level, (i+1)·2^level) — from storage,
    so a root or proof reads O(log² n) stored hashes instead of every leaf."""

    def __init__(self, leaves: list[bytes] | None = None,
                 node: Callable[[int, int], bytes] | None = None):
        self.leaves = leaves
        self.node = node
        self._memo: dict[tuple[int, int], bytes] = {}

    def mth(self, lo: int, hi: int) -> bytes:
        n = hi - lo
        if n == 0:
            return _sha(b"")
        if self.node is not None and n & (n - 1) == 0 and lo % n == 0:
            return self.node(n.bit_length() - 1, lo // n)
        if n == 1:
            return self.leaves[lo]
        got = self._memo.get((lo, hi))
        if got is None:
            k = _split(n)
            got = self._memo[(lo, hi)] = node_hash(self.mth(lo, lo + k), self.mth(lo + k, hi))
        return got

    def path(self, m: int, lo: int, hi: int) -> list[bytes]:
        n = hi - lo
        if n <= 1:
            return []
        k = _split(n)
        if m < k:
            return [*self.path(m, lo, lo + k), self.mth(lo + k, hi)]
        return [*self.path(m - k, lo + k, hi), self.mth(lo, lo + k)]

    def subproof(self, m: int, lo: int, hi: int, whole: bool) -> list[bytes]:
        n = hi - lo
        if m == n:
            return [] if whole else [self.mth(lo, hi)]
        k = _split(n)
        if m <= k:
            return [*self.subproof(m, lo, lo + k, whole), self.mth(lo + k, hi)]
        return [*self.subproof(m - k, lo + k, hi, False), self.mth(lo, lo + k)]


def root(leaves: list[bytes]) -> bytes:
    return _Tree(leaves).mth(0, len(leaves))


def inclusion_proof(index: int, leaves: list[bytes]) -> list[bytes]:
    if not 0 <= index < len(leaves):
        raise ValueError("index is not in the log")
    return _Tree(leaves).path(index, 0, len(leaves))


def consistency_proof(old_size: int, leaves: list[bytes]) -> list[bytes]:
    if not 0 < old_size <= len(leaves):
        raise ValueError("old size must be between 1 and the log's size")
    return _Tree(leaves).subproof(old_size, 0, len(leaves), True)


def verify_inclusion(index: int, size: int, leaf: bytes, proof: list[bytes], root_hash: bytes) -> bool:
    """RFC 9162 §2.1.3.2."""
    if not 0 <= index < size:
        return False
    fn, sn, r = index, size - 1, leaf
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            while not fn & 1 and fn != 0:
                fn >>= 1
                sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root_hash


def verify_consistency(old_size: int, new_size: int, old_root: bytes, new_root: bytes,
                       proof: list[bytes]) -> bool:
    """RFC 9162 §2.1.4.2."""
    if not 0 < old_size <= new_size:
        return False
    if old_size == new_size:
        return not proof and old_root == new_root
    if old_size & (old_size - 1) == 0:          # a power of two: the old root is a node of the new tree
        proof = [old_root, *proof]
    if not proof:
        return False
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    fr = sr = proof[0]
    for c in proof[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            while not fn & 1 and fn != 0:
                fn >>= 1
                sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == old_root and sr == new_root and sn == 0


def state_bytes(state: dict[str, Any]) -> bytes:
    """What a log state's signature covers: its fields minus the signature."""
    return STATE_LABEL + canonical({k: v for k, v in state.items() if k != "signature"})


def handover_bytes(handover: dict[str, Any]) -> bytes:
    """P269: what a hand-over's signature covers: its fields minus the signature."""
    return HANDOVER_LABEL + canonical({k: v for k, v in handover.items() if k != "signature"})


# ─── storage (the relay database) ─────────────────────────────────────────────
# P258: kept small. A leaf is its index, its record (canonical JSON) and an
# 8-byte key of its ref; its hash is recomputed from the record when a proof
# needs it (a microsecond). Subtree hashes are 32 raw bytes, in a table without
# rowids. About 0.25 KB an entry, most of it the record itself.

SCHEMA = """
CREATE TABLE IF NOT EXISTS translog_leaves (
    idx INTEGER PRIMARY KEY,
    ref_key INTEGER NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translog_ref ON translog_leaves(ref_key);
CREATE TABLE IF NOT EXISTS translog_nodes (
    level INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    hash BLOB NOT NULL,
    PRIMARY KEY (level, idx)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS translog_states (
    size INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    at TEXT NOT NULL,
    relay_public_key TEXT NOT NULL,
    signature TEXT NOT NULL,
    epoch INTEGER
);
-- P269: one row per log this relay has kept; never dropped by a rebuild
CREATE TABLE IF NOT EXISTS translog_epochs (
    epoch INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous TEXT,
    handover TEXT
);
CREATE TABLE IF NOT EXISTS translog_seen (
    key TEXT PRIMARY KEY,
    digest TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS translog_outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref TEXT,
    backfill INTEGER NOT NULL DEFAULT 0
);
"""

#: P263: what translog_seen says about the ingest it was written by. A log
#: written by another (P256-P262 folded by scans) is rebuilt once.
INGEST = "outbox-v2"     # P268: rows by their key, not their rowid

_OLD_TABLES = ("translog_chain_heads", "translog_sources")


def enabled() -> bool:
    """P281: the log is off unless the operator turned it on (EMPORIA_TRANSPARENCY_LOG=on)."""
    import os
    return os.getenv("EMPORIA_TRANSPARENCY_LOG", "off").strip().lower() not in ("0", "off", "false", "no")


def init_schema(conn: Any) -> None:
    """The log's tables, and (log on) the triggers that feed it.

    A log written by an older ingest (the P256-P257 shape, or P258-P262's scans)
    is dropped and rebuilt: every source is folded in again from the start, so
    nothing the relay holds is lost — only the old signed states, which no client
    checked yet. With the log off the triggers are taken out, and turning it on
    again later folds in everything from the start (unchanged rows add nothing)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(translog_leaves)").fetchall()}
    old_shape = "record_json" in cols
    has_seen = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='translog_seen'").fetchone()
    ingest = (conn.execute("SELECT digest FROM translog_seen WHERE key = 'ingest'").fetchone()
              if has_seen else None)
    rebuild = old_shape or (cols and (ingest is None or ingest[0] != INGEST))
    secret, last = None, None
    if rebuild:
        # P269: a rebuild keeps the commit secret (so a private record's salt never
        # changes) and the old log's last signed state, which the new log's first
        # state hands over from — loud, signed, and never passed off as the same log.
        if has_seen:
            secret = conn.execute("SELECT digest FROM translog_seen WHERE key = 'commit_secret'").fetchone()
        if not old_shape and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='translog_states'").fetchone():
            last = conn.execute("SELECT * FROM translog_states ORDER BY size DESC LIMIT 1").fetchone()
        for t in ("translog_leaves", "translog_nodes", "translog_states", "translog_seen", "translog_outbox",
                  *_OLD_TABLES):
            conn.execute(f"DROP TABLE IF EXISTS {t}")  # noqa: S608
    conn.executescript(SCHEMA)
    if "epoch" not in {r[1] for r in conn.execute("PRAGMA table_info(translog_states)").fetchall()}:
        conn.execute("ALTER TABLE translog_states ADD COLUMN epoch INTEGER")
    if secret:
        _mark(conn, "commit_secret", secret[0])
    held = conn.execute("SELECT MAX(epoch) FROM translog_epochs").fetchone()[0]
    if rebuild or held is None:
        reason = ("a new log" if not cols else
                  f"rebuilt: ingest {ingest[0] if ingest else 'unmarked'} -> {INGEST}" if rebuild else
                  "kept from before epochs were recorded")
        conn.execute("INSERT INTO translog_epochs (epoch, started_at, reason, previous) VALUES (?,?,?,?)",
                     ((held or 0) + 1, datetime.now(UTC).isoformat(), reason,
                      json.dumps(_state_row(last)) if last else None))
        # a log kept as it is keeps its states as signed: without an epoch field
    _mark(conn, "ingest", INGEST)
    if not conn.execute("SELECT 1 FROM translog_seen WHERE key = 'commit_secret'").fetchone():
        import secrets
        _mark(conn, "commit_secret", secrets.token_hex(32))
    if enabled():
        drop_triggers(conn)                            # P269: always the current triggers (the NULL-key guard)
        install_triggers(conn)
        if _seen(conn, "backfilled") is None:
            backfill(conn)
            _mark(conn, "backfilled", "1")
    else:
        drop_triggers(conn)
        conn.execute("DELETE FROM translog_seen WHERE key = 'backfilled'")
    conn.commit()


def ref_key(ref: str) -> int:
    """The 8-byte key a ref is filed under (signed, as SQLite stores integers)."""
    return int.from_bytes(hashlib.sha256(ref.encode()).digest()[:8], "big", signed=True)


def size(conn: Any) -> int:
    return conn.execute("SELECT COALESCE(MAX(idx) + 1, 0) FROM translog_leaves").fetchone()[0]   # not COUNT(*): a scan


def append(conn: Any, kind: str, ref: str, record: dict[str, Any]) -> int | None:
    """Add one leaf, in the caller's transaction, and return its index (None
    when the log is off). The record gains `kind` and `logged_at`, this relay's
    time of recording."""
    if not enabled():
        return None
    full = {**record, "kind": kind, "logged_at": datetime.now(UTC).isoformat()}
    idx = size(conn)
    data = canonical(full)
    conn.execute("INSERT INTO translog_leaves (idx, ref_key, record) VALUES (?,?,?)", (idx, ref_key(ref), data.decode()))
    _store_nodes(conn, idx, leaf_hash(data))
    return idx


def _store_nodes(conn: Any, idx: int, leaf: bytes) -> None:
    """P257: each complete subtree the new leaf finishes, stored once — at most
    log₂ n hashes per leaf, and never changed after."""
    level, i, h = 0, idx, leaf
    while i & 1:
        h = node_hash(_node(conn, level, i - 1), h)
        level, i = level + 1, i >> 1
        conn.execute("INSERT OR REPLACE INTO translog_nodes (level, idx, hash) VALUES (?,?,?)", (level, i, h))


def _node(conn: Any, level: int, i: int) -> bytes:
    if level == 0:
        row = conn.execute("SELECT record FROM translog_leaves WHERE idx = ?", (i,)).fetchone()
        if row is not None:
            return leaf_hash(row[0].encode())
    else:
        row = conn.execute("SELECT hash FROM translog_nodes WHERE level = ? AND idx = ?", (level, i)).fetchone()
        if row is not None:
            return bytes(row[0])
    raise LookupError(f"log node {level}/{i} is missing")


def stored_tree(conn: Any) -> _Tree:
    return _Tree(node=lambda level, i: _node(conn, level, i))


def stored_root(conn: Any, upto: int) -> bytes:
    return stored_tree(conn).mth(0, upto)


def stored_inclusion_proof(conn: Any, index: int, upto: int) -> list[bytes]:
    if not 0 <= index < upto:
        raise ValueError("index is not in the log")
    return stored_tree(conn).path(index, 0, upto)


def stored_consistency_proof(conn: Any, old_size: int, upto: int) -> list[bytes]:
    if not 0 < old_size <= upto:
        raise ValueError("old size must be between 1 and the log's size")
    return stored_tree(conn).subproof(old_size, 0, upto, True)


def leaf_at(conn: Any, index: int) -> bytes:
    return _node(conn, 0, index)


def leaves(conn: Any, upto: int) -> list[bytes]:
    return [leaf_hash(r[0].encode()) for r in conn.execute(
        "SELECT record FROM translog_leaves WHERE idx < ? ORDER BY idx", (upto,)).fetchall()]


def entries(conn: Any, start: int = 0, end: int | None = None, ref: str = "", limit: int = 100) -> list[dict]:
    q, args = "SELECT idx, record FROM translog_leaves WHERE idx >= ?", [start]
    if end is not None:
        q += " AND idx < ?"
        args.append(end)
    if ref:
        q += " AND ref_key = ?"
        args.append(ref_key(ref))
    q += " ORDER BY idx LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    return [{"index": r["idx"], "record": json.loads(r["record"]), "leaf_hash": leaf_hash(r["record"].encode()).hex()}
            for r in conn.execute(q, args).fetchall()]


def _state_row(r: Any) -> dict[str, Any]:
    out = {"type": STATE_TYPE, "relay_public_key": r["relay_public_key"], "size": r["size"], "root": r["root"],
           "at": r["at"], "signature": r["signature"]}
    if "epoch" in r.keys() and r["epoch"] is not None:
        out["epoch"] = r["epoch"]
    return out


def epoch(conn: Any) -> int:
    """P269: the number of the log this relay keeps now (1 for its first)."""
    return conn.execute("SELECT COALESCE(MAX(epoch), 1) FROM translog_epochs").fetchone()[0]


def epochs(conn: Any) -> list[dict[str, Any]]:
    """Every log this relay has kept, oldest first: when it started, why, the last
    signed state of the one before it, and the signed hand-over between them."""
    return [{"epoch": r["epoch"], "started_at": r["started_at"], "reason": r["reason"],
             "previous": json.loads(r["previous"]) if r["previous"] else None,
             "handover": json.loads(r["handover"]) if r["handover"] else None}
            for r in conn.execute("SELECT * FROM translog_epochs ORDER BY epoch").fetchall()]


def latest_state(conn: Any) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM translog_states ORDER BY size DESC LIMIT 1").fetchone()
    return _state_row(r) if r else None


def state_at(conn: Any, at_size: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM translog_states WHERE size = ?", (at_size,)).fetchone()
    return _state_row(r) if r else None


def _seen(conn: Any, key: str) -> str | None:
    r = conn.execute("SELECT digest FROM translog_seen WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _mark(conn: Any, key: str, digest: str) -> None:
    conn.execute("INSERT INTO translog_seen (key, digest) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET digest = excluded.digest", (key, digest))


def record_salt(conn: Any, kind: str, ident: str) -> str:
    """P263: the salt of one private record's commitment — derived from this
    relay's log secret, so it is stored nowhere and the same each time."""
    import hmac
    secret = _seen(conn, "commit_secret") or ""
    return hmac.new(bytes.fromhex(secret) if secret else b"", f"{kind}:{ident}".encode(), hashlib.sha256).hexdigest()[:32]


def commitment_for(conn: Any, kind: str, ident: str, fields: dict[str, Any]) -> str:
    """The commitment this relay logs for a private record."""
    return commitment(kind, fields, record_salt(conn, kind, ident))


def _log(conn: Any, kind: str, public: bool, ident: str, fields: dict[str, Any],
         extra: dict[str, Any] | None = None) -> None:
    """Public: by id. Otherwise a salted commitment, filed under itself. `extra`
    is about the entry, not the record (P267: `backfilled`)."""
    if public:
        append(conn, kind, f"{kind}:{ident}", {**fields, **(extra or {})})
    else:
        c = commitment_for(conn, kind, ident, fields)
        append(conn, kind, c, {"commitment": c, **(extra or {})})


def content_hash(*parts: Any) -> str:
    return hashlib.sha256(canonical(list(parts))).hexdigest()


# ─── P263: what goes in, and how ─────────────────────────────────────────────
# Every logged table has three SQLite triggers (insert, update, delete) that
# put the changed row's rowid in translog_outbox, numbered by AUTOINCREMENT so a
# number is never reused. A fold drains the outbox in order: each row is read
# again and built into its record; a record that differs from the one last
# logged for that row is a new entry, and a row that has gone is a `_gone`
# entry. One mechanism for rows added and rows changed in place (P258 folded
# them two ways, by scanning, and missed both a reused rowid and a status
# changed after the row was logged). Room and DM chains are fed the same way,
# by the room or thread that grew. Key steps and money are also logged in their
# own transaction (`log_row`); the outbox then finds them already logged.

def _tables(conn: Any) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _topic_public(conn: Any, topic_id: str) -> bool:
    r = conn.execute("SELECT visibility FROM agora_topics WHERE topic_id = ?", (topic_id,)).fetchone()
    return bool(r) and r[0] == "public"


def _room_public(conn: Any, room_id: str) -> bool:
    r = conn.execute("SELECT room_type, encrypted FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    return bool(r) and r[0] == "public" and not r[1]


def _row(r: Any) -> dict[str, Any]:
    return {k: r[k] for k in r.keys() if k != "_r"}


def _whole_row(id_col: str):
    """A commitment over the whole row: what the relay can later show it held, and when."""
    def build(conn, r):
        return False, str(r[id_col]), _row(r)
    return build


def _agent_key(conn, r):
    by = r["signed_by"]
    if by == "commit":
        step = {"by": "commit", "commitment": r["commitment"], "at": r["at"], "signature": r["signature"]}
    elif by == "recovery":
        step = {"by": "recovery", "old": r["old_key"], "new": r["new_key"], "recovery_public_key": r["recovery_public_key"],
                "compromised_since": r["compromised_since"], "at": r["at"], "signature": r["signature"]}
    else:
        step = {"old": r["old_key"], "new": r["new_key"], "at": r["at"], "by": by, "signature": r["signature"]}
    return True, f"{r['stable_id']}:{r['step_no']}:{r['at']}", {
        "stable_id": r["stable_id"], "step_no": r["step_no"], "step": step}


#: P263: with the status, so a release or a refund is a new entry, not a silent edit.
PAYMENT_FIELDS = ("payment_id", "agent_id", "amount_cents", "currency", "payment_type", "session_id", "room_id",
                  "rail", "status", "created_at")
SETTLEMENT_FIELDS = ("settlement_id", "session_id", "winner_id", "payer_id", "total_stake_cents",
                     "winner_payout_cents", "rail", "status", "transfer_status", "created_at")


def _fields(names: tuple[str, ...], id_col: str):
    def build(conn, r):
        # who paid whom is not public: always a commitment
        cols = set(r.keys())
        return False, r[id_col], {k: r[k] if k in cols else None for k in names}
    return build


def _agora_topic(conn, r):
    return r["visibility"] == "public", r["topic_id"], {
        "topic_id": r["topic_id"], "slug": r["slug"], "creator_id": r["creator_id"],
        "content_hash": content_hash(r["name"], r["description"]), "created_at": r["created_at"]}


def _post(conn, r):
    """A post:v2, as an Agora post is logged: the text and tags only as a hash, so the log shows that a post
    was made, by whom and when, never what it said. One in a topic that is not public is a commitment only
    (the topic and its author are salted in), as a private topic's Agora post is."""
    topic = r["topic"]
    public = True
    if topic:
        t = conn.execute("SELECT visibility, gate_type FROM agora_topics WHERE slug = ?", (topic,)).fetchone()
        public = bool(t) and t[0] == "public" and t[1] == "open"
    return public, r["post_id"], {
        "post_id": r["post_id"], "author_id": r["author_id"], "author_stable_id": r["author_stable_id"],
        "kind": r["kind"], "content_hash": content_hash(r["label"], r["text"], r["tags_json"]),
        "topic": topic, "reply_to": r["reply_to"], "quote_of": r["quote_of"], "root": r["root"],
        "nonce": r["nonce"], "signature": r["signature"], "at": r["at"], "is_deleted": r["is_deleted"]}


def _room_message(conn, r):
    """A room message (rooms v2), as a post is logged: by id, the content only as a hash, so the log shows
    that a message was sent, by whom and when, never what it said. One in a room that is not public, or
    that is encrypted, is a commitment only (the room and its author are salted in) -- and an encrypted
    room's content is ciphertext already, so its hash says nothing either way. The kind, tags and refs of
    an encrypted room's message are inside the seal and in no entry at all."""
    return _room_public(conn, r["room_id"]), r["message_id"], {
        "message_id": r["message_id"], "room_id": r["room_id"], "sender_id": r["sender_id"],
        "msg_type": r["msg_type"], "content_hash": content_hash(r["content"]),
        "reply_to": r["reply_to"] if "reply_to" in r.keys() else None,
        "root": r["root"] if "root" in r.keys() else None, "nonce": r["nonce"] or "",
        "signature": r["signature"] or "", "created_at": r["created_at"], "removed": bool(r["deleted_at"])}


def _listing(conn, r):
    return True, r["listing_id"], {
        "listing_id": r["listing_id"], "agent_id": r["agent_id"], "agent_stable_id": r["agent_stable_id"] or "",
        "listing_type": r["listing_type"], "content_hash": content_hash(r["title"], r["description"]),
        "signature": r["signature"] or "", "created_at": r["created_at"]}


def _room_act(conn, r):
    return _room_public(conn, r["room_id"]), r["act_id"], {
        "act_id": r["act_id"], "room_id": r["room_id"], "act": r["act"], "by_agent": r["by_agent"],
        "payload_hash": content_hash(r["payload_json"]), "signature": r["signature"] or "", "at": r["at"]}


def _topic_act(conn, r):
    return _topic_public(conn, r["topic_id"]), r["act_id"], {
        "act_id": r["act_id"], "topic_id": r["topic_id"], "act": r["act"], "by_agent": r["by_agent"],
        "payload_hash": content_hash(r["payload_json"]), "signature": r["signature"] or "", "at": r["at"]}


def _deletion(conn, r):
    # the removed item's id is hashed: a removal is on record, not what was removed
    return True, content_hash(r["kind"], r["item"]), {
        "kind": r["kind"], "item_hash": content_hash(r["kind"], r["item"]), "role": r["role"],
        "deleted_at": r["deleted_at"], "signature": r["signature"] or ""}


def _action(conn, r):
    # a move can be a sealed bid or a low-entropy choice: always a commitment
    return False, r["action_id"], {
        "action_id": r["action_id"], "session_id": r["session_id"], "agent_id": r["agent_id"],
        "action_type": r["action_type"], "payload_hash": content_hash(r["payload_json"]),
        "created_at": r["created_at"]}


def _game_chat(conn, r):
    return False, r["message_id"], {
        "message_id": r["message_id"], "session_id": r["session_id"], "agent_id": r["agent_id"],
        "content_hash": content_hash(r["content"]), "created_at": r["created_at"]}


def _session_message(conn, r):
    return False, r["message_id"], {
        "message_id": r["message_id"], "from_agent": r["from_agent"], "to_agent": r["to_agent"],
        "session_id": r["session_id"], "payload_hash": content_hash(r["payload_json"]),
        "signature": r["signature"] or "", "created_at": r["created_at"]}


def _agent(conn, r):
    # the profile others act on: keys, payout wallets, relays, name — hashed, so a
    # verifier compares it with /agents/{id} and a quiet change shows
    return True, r["stable_id"] or r["agent_id"], {
        "agent_id": r["agent_id"], "stable_id": r["stable_id"] or "", "public_key_hex": r["public_key_hex"],
        "profile_hash": content_hash(*(r[k] if k in r.keys() else "" for k in AGENT_PROFILE_FIELDS))}


AGENT_PROFILE_FIELDS = ("agent_id", "public_key_hex", "display_name", "is_active", "x402_pay_to_json",
                        "stripe_account_id", "encryption_public_key", "encryption_key_signature", "stable_id",
                        "accepts_remote_dms", "relay_list_json", "x402_pay_to_proofs_json",
                        "encryption_key_at")       # P271: a replaced encryption key is a changed profile


def _signed_by_owner(id_col: str, at_col: str):
    def build(conn, r):
        return True, r[id_col], {id_col: r[id_col], "stable_id": r["stable_id"], "signature": r["signature"],
                                 at_col: r[at_col]}
    return build


def _escrow(conn, r):
    return False, r["hold_hash"], {k: r[k] for k in ("hold_hash", "session_id", "payer_agent_id", "receiver_agent_id",
                                                     "network", "amount_atomic", "state", "authorize_tx",
                                                     "capture_tx", "void_tx", "updated_at")}


def _session(conn, r):
    return False, r["session_id"], {k: r[k] for k in ("session_id", "module_type", "status", "participants_json",
                                                      "created_at")}


def _pledge(conn, r):
    # Pledges: a signed promise, as public as its subject was when it was made (a listing, a
    # public game); a private deal's pledge is logged as a commitment only
    return bool(r["public"]), r["pledge_id"], {k: r[k] for k in (
        "pledge_id", "maker_id", "to_agent", "kind", "subject_type", "subject_id", "terms_json", "note", "nonce",
        "made_at", "signature")}


def _pledge_act(conn, r):
    p = conn.execute("SELECT public FROM pledges WHERE pledge_id = ?", (r["pledge_id"],)).fetchone()
    return bool(p and p[0]), f"{r['pledge_id']}:{r['act']}", {k: r[k] for k in (
        "pledge_id", "act", "agent_id", "verdict", "at", "signature")}


def _lab(conn, r):
    # Labs v2: a lab is public: its subject, terms and its creator's signature (its room may be private)
    return True, r["slug"], {k: r[k] for k in (
        "slug", "name", "room_id", "subject_kind", "subject_ref", "repo_url", "branch", "proof_commit",
        "creator_id", "creator_stable", "maintainers_json", "licence", "verified", "checked_at", "signed_at",
        "signature")}


def _lab_report(conn, r):
    # Labs v2: a maintainer's signed report, public in every lab: refs, agents and the signed claims
    return True, r["report_id"], {k: r[k] for k in (
        "report_id", "lab_slug", "ref_kind", "ref_id", "event", "commit_sha", "maintainer_id", "author_claim",
        "credited_json", "ideas_json", "note", "signed_at", "signature")}


#: logged tables — kind -> (table, builder, trigger condition on the row or "", key columns).
#: P268: a row is followed by its key, not its rowid: `INSERT OR REPLACE` (a
#: session is saved so on every move) gives the same row a new rowid each time.
SOURCES: dict[str, tuple[str, Callable, str, tuple[str, ...]]] = {
    "agent_key": ("agent_key_log", _agent_key, "", ("stable_id", "step_no")),
    "payment": ("payments", _fields(PAYMENT_FIELDS, "payment_id"), "", ("payment_id",)),
    "settlement": ("settlements", _fields(SETTLEMENT_FIELDS, "settlement_id"), "", ("settlement_id",)),
    "agora_topic": ("agora_topics", _agora_topic, "", ("topic_id",)),
    "listing": ("listings", _listing, "", ("listing_id",)),
    "room_act": ("room_acts", _room_act, "", ("act_id",)),
    "topic_act": ("topic_acts", _topic_act, "", ("act_id",)),   # P278
    "deletion": ("deletions", _deletion, "", ("kind", "item")),
    "session_action": ("actions", _action, "", ("action_id",)),
    "session_message": ("messages", _session_message, "", ("message_id",)),
    "access_grant": ("access_entitlements", _whole_row("entitlement_id"), "", ("entitlement_id",)),
    "access_removal": ("access_removals", _whole_row("removal_id"), "", ("removal_id",)),
    "participant": ("participants", _whole_row("session_id"), "", ("session_id", "agent_id")),
    "session_leave": ("session_leaves", _whole_row("session_id"), "", ("session_id", "agent_id", "at")),   # P273
    "agent": ("authorized_agents", _agent, "", ("agent_id",)),
    "session_offer": ("session_offers", _signed_by_owner("session_id", "offered_at"), "", ("session_id",)),
    "escrow_hold": ("escrow_holds", _escrow, "", ("hold_hash",)),
    "session": ("sessions", _session, "", ("session_id",)),
    "reaction": ("reactions", _whole_row("target_id"), "", ("agent_id", "target", "value")),   # replaces P297 likes
    "post": ("posts", _post, "{row}.federated_from IS NULL", ("post_id",)),   # Feed v2
    "room_message": ("room_messages", _room_message, "", ("message_id",)),   # rooms v2
    "follow": ("follows", _whole_row("target"), "", ("agent_id", "target")),
    "pledge": ("pledges", _pledge, "", ("pledge_id",)),                   # pledges
    "pledge_act": ("pledge_acts", _pledge_act, "", ("pledge_id", "act")),
    # emporia:rand:v1 (games v2): each player's signed commitment, and how the game ended (the beacon round
    # used, forfeits, collisions and voids included). Reveals and voids are session_actions, as every move is.
    "rand_commit": ("rand_commits", _whole_row("session_id"), "", ("session_id", "agent_id")),
    "rand_result": ("rand_results", _whole_row("session_id"), "", ("session_id",)),
    "lab": ("labs", _lab, "", ("slug",)),                                  # labs v2
    "lab_report": ("lab_reports", _lab_report, "", ("report_id",)),
}

#: chains — name -> (messages table, the column naming the chain)
CHAINS: dict[str, tuple[str, str]] = {"room": ("room_messages", "room_id"), "dm": ("dm_messages", "thread_id")}

#: tables deliberately not in the log, and why
NOT_LOGGED: dict[str, str] = {
    "dm_messages": "content; each conversation's chain head is logged instead, as a commitment",
    "dm_threads": "who talks to whom; the chain heads cover the conversations",
    "rooms": "a room's creation, settings, invites, kicks, joins and leaves are its signed room_acts",
    "room_invites": "an invite, or taking one back, is a signed room_act, logged as one",
    "dm_blocks": "an agent's private choice of whom it takes no DMs from",
    "agora_members": "who follows a topic: bookkeeping, and nobody's signed statement",
    "agora_invites": "an invite, or taking one back, is a signed topic_act, logged as one",
    "game_chat": "table talk; the moves are logged",
    "lab_report_ideas": "an index of the ideas each lab report names; the signed report carries the list and is logged",
    "beacon_rounds": "a cache of drand's public rounds; anyone refetches and verifies them",
    "season_finals": "a season's final standing, signed by the relay and recomputable from the records it hashes",
    "agent_inbox": "delivery bookkeeping",
    "reg_challenges": "short-lived sign-in nonces",
    "remote_agents": "agents of other relays, pinned here; in their own relay's log",
    "opened_from": "which object a room or topic was opened from: derived from the logged reply post that names it",
    "x402_settle_intents": "short-lived payment bookkeeping; the payment itself is logged",
    "agent_key_log_superseded": "copies of key steps a recovery undid; the steps were logged when made",
    "operator_log": "the relay's own record of a changed operator label (shown on the agent's card as a change date); the label is declared, not verified",
    "model_commits": "the commitment each move carries is inside its signed move, which is logged",
    "model_reveals": "an end-of-game reveal, kept with its signature and checked against the commits; declared, never verified, and no outcome rests on it",
    "token_revocations": "short-lived session bookkeeping: a signed-out token's id until it would have expired",
    "token_floors": "when an agent last revoked all its session tokens: session bookkeeping",
}


_SEP = "\x1f"     # between the columns of a key


def _key_sql(row: str, cols: tuple[str, ...]) -> str:
    return " || char(31) || ".join(f"{row}.{c}" for c in cols)


def _trigger_sql(name: str, table: str, kind: str, cond: str, cols: tuple[str, ...]) -> list[str]:
    """Insert: the new key. Delete: the old. Update: both — a changed key (a
    rename) is the old record gone and a new one."""
    out = []
    # P269: SQLite lets a TEXT PRIMARY KEY hold NULL, and NULL || anything is NULL:
    # such a row would never reach the log. Refused here instead, loudly.
    guard = (f"SELECT RAISE(ABORT, 'translog: a {table} row with no key ({', '.join(cols)})') WHERE "
             + " OR ".join(f"NEW.{c} IS NULL" for c in cols) + ";")
    for when, rows in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
        body = (guard + " " if when != "DELETE" else "") + " ".join(
            f"INSERT INTO translog_outbox (kind, ref) SELECT '{kind}', {_key_sql(row, cols)}"
            + (f" WHERE {cond.format(row=row)};" if cond else ";") for row in rows)
        out.append(f"CREATE TRIGGER IF NOT EXISTS translog_{name}_{when.lower()} AFTER {when} ON {table} "
                   f"BEGIN {body} END")
    return out


def install_triggers(conn: Any) -> None:
    tables = _tables(conn)
    for kind, (table, _build, cond, cols) in SOURCES.items():
        if table in tables:
            for sql in _trigger_sql(kind, table, kind, cond, cols):
                conn.execute(sql)
    for chain, (table, column) in CHAINS.items():
        if table in tables:
            for sql in _trigger_sql(f"{chain}_chain", table, f"{chain}_chain", "", (column,)):
                conn.execute(sql)


def drop_triggers(conn: Any) -> None:
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'translog_%'"
                                ).fetchall():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")  # noqa: S608


def backfill(conn: Any) -> None:
    """Every row the relay already holds, into the outbox — for a log that
    starts on a relay already in use, or is turned on again. P267: marked, so an
    entry for a row held before the log began does not read as recorded when it
    happened (its `logged_at` is now)."""
    tables = _tables(conn)
    for kind, (table, _build, cond, cols) in SOURCES.items():
        if table in tables:
            where = f" WHERE {cond.format(row=table)}" if cond else ""
            conn.execute(f"INSERT INTO translog_outbox (kind, ref, backfill) SELECT ?, {_key_sql(table, cols)}, 1 "  # noqa: S608
                         f"FROM {table}{where} ORDER BY rowid", (kind,))
    for chain, (table, column) in CHAINS.items():
        if table in tables:
            conn.execute(f"INSERT INTO translog_outbox (kind, ref) SELECT DISTINCT ?, {column} FROM {table}",  # noqa: S608
                         (f"{chain}_chain",))
    # P269: and every row the log already holds, so one removed while the log was
    # off is logged as gone (one still there is found unchanged and adds nothing)
    for (key,) in conn.execute("SELECT key FROM translog_seen WHERE key LIKE '%#%'").fetchall():
        kind, _, ref = key.partition("#")
        if kind in SOURCES and SOURCES[kind][0] in tables:
            conn.execute("INSERT INTO translog_outbox (kind, ref, backfill) VALUES (?, ?, 1)", (kind, ref))


def _held(conn: Any, key: str) -> dict[str, str] | None:
    raw = _seen(conn, key)
    return json.loads(raw) if raw else None


def _ingest_row(conn: Any, kind: str, ref: str, backfilled: bool = False) -> None:
    table, build, _cond, cols = SOURCES[kind]
    key = f"{kind}#{ref}"
    held = _held(conn, key)
    values = ref.split(_SEP)
    r = (conn.execute(f"SELECT * FROM {table} WHERE " + " AND ".join(f"{c} = ?" for c in cols),  # noqa: S608
                      values).fetchone() if len(values) == len(cols) else None)
    if r is None:
        if held:
            # P269: a private record's removal is salted like the record was, so an
            # id someone can guess does not tell them it was removed. Public: by id.
            gone = (content_hash(kind, held["id"]) if held.get("p") else
                    content_hash(kind, held["id"], record_salt(conn, kind, held["id"])))
            append(conn, f"{kind}_gone", f"{kind}_gone:{gone}", {"id_hash": gone})
            conn.execute("DELETE FROM translog_seen WHERE key = ?", (key,))
        return
    public, ident, fields = build(conn, r)
    ident, digest = str(ident), content_hash(fields)
    if held and held["d"] == digest:
        return
    _log(conn, kind, public, ident, fields, {"backfilled": True} if backfilled else None)
    _mark(conn, key, json.dumps({"id": ident, "d": digest, "p": bool(public)}))


def _ingest_chain(conn: Any, chain: str, ident: str) -> None:
    table, column = CHAINS[chain]
    row = conn.execute(f"SELECT chain_hash, (SELECT COUNT(*) FROM {table} WHERE {column} = ? "  # noqa: S608
                       f"AND chain_hash IS NOT NULL) n FROM {table} WHERE {column} = ? AND chain_hash IS NOT NULL "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (ident, ident)).fetchone()
    if not row:
        return
    key, head, count = f"{chain}:{ident}", row[0], row[1]
    if _seen(conn, key) == head:                   # P263: by head, not count — an edit keeps the count
        return
    fields = {"chain": chain, "id": ident, "head": head, "count": count}
    if chain == "room" and _room_public(conn, ident):
        append(conn, "room_chain", key, fields)
    else:
        c = commitment_for(conn, f"{chain}_chain", ident, fields)
        append(conn, "private_chain", c, {"commitment": c})
    _mark(conn, key, head)


#: P266: entries per drain transaction — about 45 ms of DB_LOCK on a small server.
DRAIN_BATCH = 500


def drain(conn: Any, limit: int = DRAIN_BATCH) -> int:
    """Fold up to `limit` outbox entries into the log, oldest first, and return
    how many were taken — fewer than `limit` means the outbox is empty. Called
    in batches so a large backlog never holds the database for long."""
    rows = conn.execute("SELECT seq, kind, ref, backfill FROM translog_outbox ORDER BY seq LIMIT ?",
                        (limit,)).fetchall()
    if not rows:
        return 0
    done: set[tuple[str, Any]] = set()
    tables = _tables(conn)
    for _seq, kind, ref, backfilled in rows:
        if ref is None or (kind, ref) in done:
            continue
        done.add((kind, ref))
        chain = kind[:-6] if kind.endswith("_chain") and kind[:-6] in CHAINS else ""
        if chain:
            if CHAINS[chain][0] in tables:
                _ingest_chain(conn, chain, str(ref))
        elif kind in SOURCES and SOURCES[kind][0] in tables:
            _ingest_row(conn, kind, str(ref), backfilled=bool(backfilled))
    conn.execute("DELETE FROM translog_outbox WHERE seq <= ?", (rows[-1][0],))
    return len(rows)


def waiting(conn: Any) -> int:
    """Outbox entries not yet folded in."""
    return conn.execute("SELECT COUNT(*) FROM translog_outbox").fetchone()[0]


def fold_all(conn: Any) -> None:
    """Drain the whole outbox in one go (tests, and a log signed by hand)."""
    while drain(conn) == DRAIN_BATCH:
        pass


def fold_relay_keys(conn: Any, history: list[dict[str, str]]) -> None:
    """A leaf for each change of the relay's own key not yet logged."""
    for step in history:
        key = f"relay_key:{step.get('at', '')}"
        if _seen(conn, key) is None:
            # P290: a guardian step, or a rotation the guardians signed, goes in whole
            fields = ("type", "key", "guardians", "threshold", "old", "new", "at", "signature", "guardian_signatures")
            append(conn, "relay_key", key, {k: step[k] for k in fields if k in step})
            _mark(conn, key, "1")


def log_row(conn: Any, kind: str, rowid: int | None) -> None:
    """Log one row now, in the caller's transaction — for key changes and money,
    which should never wait for the next fold. Its outbox entry then finds it
    already logged."""
    if not enabled() or rowid is None:
        return
    table, _build, _cond, cols = SOURCES[kind]
    r = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE rowid = ?", (rowid,)).fetchone()  # noqa: S608
    if r is not None and all(v is not None for v in r):
        _ingest_row(conn, kind, _SEP.join(str(v) for v in r))


def sign_state(conn: Any, sign: Callable[[bytes], str], relay_public_key: str,
               relay_key_history: list[dict[str, str]] = ()) -> dict[str, Any] | None:
    """Fold everything in, then sign the log's state — or return None when the
    log is off or has not grown since the last one. The relay drains the outbox
    in batches first (`drain`), so what is left here is small."""
    if not enabled():
        return None
    fold_relay_keys(conn, list(relay_key_history))
    fold_all(conn)
    n = size(conn)
    last = latest_state(conn)
    if n == 0 or (last and last["size"] == n):
        return None
    ep = epoch(conn)
    state = {"type": STATE_TYPE, "relay_public_key": relay_public_key, "size": n,
             "root": stored_root(conn, n).hex(), "at": datetime.now(UTC).isoformat(), "epoch": ep}
    state["signature"] = sign(state_bytes(state))
    conn.execute("INSERT INTO translog_states (size, root, at, relay_public_key, signature, epoch) "
                 "VALUES (?,?,?,?,?,?)", (n, state["root"], state["at"], relay_public_key, state["signature"], ep))
    row = conn.execute("SELECT previous, handover, reason FROM translog_epochs WHERE epoch = ?", (ep,)).fetchone()
    if row and row["previous"] and not row["handover"]:
        prev = json.loads(row["previous"])
        handover = {"type": HANDOVER_TYPE, "epoch": ep, "reason": row["reason"],
                    "previous": {k: prev.get(k) for k in ("epoch", "size", "root", "at", "relay_public_key")},
                    "first": {k: state[k] for k in ("size", "root", "at")},
                    "relay_public_key": relay_public_key, "at": state["at"]}
        handover["signature"] = sign(handover_bytes(handover))
        conn.execute("UPDATE translog_epochs SET handover = ? WHERE epoch = ?", (json.dumps(handover), ep))
    return state
