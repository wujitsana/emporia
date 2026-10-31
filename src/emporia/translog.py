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
COMMIT_LABEL = "emporia:log-commit:v1:"


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


def commitment(kind: str, fields: dict[str, Any]) -> str:
    """A private event's stand-in: hex SHA-256 over its fields. Every field set
    committed to carries a random id (payment_id, settlement_id, a thread or room
    id), so the public cannot recompute it by guessing amounts and names."""
    return hashlib.sha256(COMMIT_LABEL.encode() + kind.encode() + b":" + canonical(fields)).hexdigest()


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


# ─── storage (the relay database) ─────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS translog_leaves (
    idx INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    record_json TEXT NOT NULL,
    leaf_hash TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translog_ref ON translog_leaves(ref);
CREATE TABLE IF NOT EXISTS translog_states (
    size INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    at TEXT NOT NULL,
    relay_public_key TEXT NOT NULL,
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translog_chain_heads (
    chain TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS translog_nodes (
    level INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (level, idx)
);
CREATE TABLE IF NOT EXISTS translog_sources (
    source TEXT PRIMARY KEY,
    last_rowid INTEGER NOT NULL
);
"""


def init_schema(conn: Any) -> None:
    conn.executescript(SCHEMA)
    # P257: a log written before stored nodes gets them now, once
    n = size(conn)
    if n and not conn.execute("SELECT 1 FROM translog_nodes LIMIT 1").fetchone():
        for i, (h,) in enumerate(conn.execute("SELECT leaf_hash FROM translog_leaves ORDER BY idx").fetchall()):
            _store_nodes(conn, i, bytes.fromhex(h))


def size(conn: Any) -> int:
    return conn.execute("SELECT COALESCE(MAX(idx) + 1, 0) FROM translog_leaves").fetchone()[0]   # not COUNT(*): a scan


def append(conn: Any, kind: str, ref: str, record: dict[str, Any]) -> int:
    """Add one leaf, in the caller's transaction, and return its index. The
    record gains `kind` and `logged_at`, this relay's time of recording."""
    at = datetime.now(UTC).isoformat()
    full = {**record, "kind": kind, "logged_at": at}
    idx = size(conn)
    leaf = record_leaf_hash(full)
    conn.execute("INSERT INTO translog_leaves (idx, kind, ref, record_json, leaf_hash, at) VALUES (?,?,?,?,?,?)",
                 (idx, kind, ref, canonical(full).decode(), leaf.hex(), at))
    _store_nodes(conn, idx, leaf)
    return idx


def _store_nodes(conn: Any, idx: int, leaf: bytes) -> None:
    """P257: each complete subtree the new leaf finishes, stored once — at most
    log₂ n hashes per leaf, and never changed after."""
    level, i, h = 0, idx, leaf
    while i & 1:
        h = node_hash(_node(conn, level, i - 1), h)
        level, i = level + 1, i >> 1
        conn.execute("INSERT OR REPLACE INTO translog_nodes (level, idx, hash) VALUES (?,?,?)", (level, i, h.hex()))


def _node(conn: Any, level: int, i: int) -> bytes:
    if level == 0:
        row = conn.execute("SELECT leaf_hash FROM translog_leaves WHERE idx = ?", (i,)).fetchone()
    else:
        row = conn.execute("SELECT hash FROM translog_nodes WHERE level = ? AND idx = ?", (level, i)).fetchone()
    if row is None:
        raise LookupError(f"log node {level}/{i} is missing")
    return bytes.fromhex(row[0])


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
    return [bytes.fromhex(r[0]) for r in conn.execute(
        "SELECT leaf_hash FROM translog_leaves WHERE idx < ? ORDER BY idx", (upto,)).fetchall()]


def entries(conn: Any, start: int = 0, end: int | None = None, ref: str = "", limit: int = 100) -> list[dict]:
    q, args = "SELECT idx, ref, record_json, leaf_hash FROM translog_leaves WHERE idx >= ?", [start]
    if end is not None:
        q += " AND idx < ?"
        args.append(end)
    if ref:
        q += " AND ref = ?"
        args.append(ref)
    q += " ORDER BY idx LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    return [{"index": r["idx"], "ref": r["ref"], "record": json.loads(r["record_json"]), "leaf_hash": r["leaf_hash"]}
            for r in conn.execute(q, args).fetchall()]


def _state_row(r: Any) -> dict[str, Any]:
    return {"type": STATE_TYPE, "relay_public_key": r["relay_public_key"], "size": r["size"], "root": r["root"],
            "at": r["at"], "signature": r["signature"]}


def latest_state(conn: Any) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM translog_states ORDER BY size DESC LIMIT 1").fetchone()
    return _state_row(r) if r else None


def state_at(conn: Any, at_size: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM translog_states WHERE size = ?", (at_size,)).fetchone()
    return _state_row(r) if r else None


# ─── what is folded in when a state is signed ─────────────────────────────────

def _fold_chain(conn: Any, chain: str, ident: str, head: str | None, count: int, public: bool) -> None:
    key = f"{chain}:{ident}"
    seen = conn.execute("SELECT count FROM translog_chain_heads WHERE chain = ?", (key,)).fetchone()
    if not head or (seen and seen[0] == count):
        return
    fields = {"chain": chain, "id": ident, "head": head, "count": count}
    if public:
        append(conn, f"{chain}_chain", key, fields)
    else:
        c = commitment(f"{chain}_chain", fields)
        append(conn, "private_chain", c, {"commitment": c})
    conn.execute("INSERT INTO translog_chain_heads (chain, count) VALUES (?, ?) "
                 "ON CONFLICT(chain) DO UPDATE SET count = excluded.count", (key, count))


def _last_head(conn: Any, table: str, column: str, ident: str) -> str | None:
    row = conn.execute(f"SELECT chain_hash FROM {table} WHERE {column} = ? AND chain_hash IS NOT NULL "  # noqa: S608
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (ident,)).fetchone()
    return row[0] if row else None


def fold_chain_heads(conn: Any) -> None:
    """One leaf for each room or DM chain that grew since it was last logged:
    public rooms by id and head, everything else as a commitment that the
    participants (who hold the id, head and count) can recompute."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if "room_messages" in tables and "rooms" in tables:
        for r in conn.execute(
                "SELECT m.room_id, COUNT(*) n, r.room_type, r.encrypted FROM room_messages m "
                "JOIN rooms r ON r.room_id = m.room_id WHERE m.chain_hash IS NOT NULL GROUP BY m.room_id").fetchall():
            public = r["room_type"] == "public" and not r["encrypted"]
            _fold_chain(conn, "room", r["room_id"], _last_head(conn, "room_messages", "room_id", r["room_id"]),
                        r["n"], public)
    if "dm_messages" in tables:
        for r in conn.execute("SELECT thread_id, COUNT(*) n FROM dm_messages WHERE chain_hash IS NOT NULL "
                              "GROUP BY thread_id").fetchall():
            _fold_chain(conn, "dm", r["thread_id"], _last_head(conn, "dm_messages", "thread_id", r["thread_id"]),
                        r["n"], False)


def fold_relay_keys(conn: Any, history: list[dict[str, str]]) -> None:
    """A leaf for each change of the relay's own key not yet logged."""
    for step in history:
        ref = f"relay_key:{step.get('at', '')}"
        if not conn.execute("SELECT 1 FROM translog_leaves WHERE ref = ?", (ref,)).fetchone():
            append(conn, "relay_key", ref, {k: step.get(k, "") for k in ("old", "new", "at", "signature")})


def sign_state(conn: Any, sign: Callable[[bytes], str], relay_public_key: str,
               relay_key_history: list[dict[str, str]] = ()) -> dict[str, Any] | None:
    """Fold in chain heads and relay key changes, then sign the log's state —
    or return None when the log has not grown since the last one."""
    fold_relay_keys(conn, list(relay_key_history))
    fold_chain_heads(conn)
    fold_records(conn)
    n = size(conn)
    last = latest_state(conn)
    if n == 0 or (last and last["size"] == n):
        return None
    state = {"type": STATE_TYPE, "relay_public_key": relay_public_key, "size": n,
             "root": stored_root(conn, n).hex(), "at": datetime.now(UTC).isoformat()}
    state["signature"] = sign(state_bytes(state))
    conn.execute("INSERT INTO translog_states (size, root, at, relay_public_key, signature) VALUES (?,?,?,?,?)",
                 (n, state["root"], state["at"], relay_public_key, state["signature"]))
    return state


# ─── P257: records folded in by table ─────────────────────────────────────────
# Each source is read from where the last fold stopped (its rowid). A row
# written again with INSERT OR REPLACE gets a new rowid, so it is logged again —
# a changed advert or offer is a new entry. Public items go in by id with a hash
# of their content and the author's signature; anything not public goes in as a
# commitment over the fields listed here, which those who hold the record can
# recompute (`commitment(kind, fields)`) and find with `GET /log/entries?ref=`.

def content_hash(*parts: Any) -> str:
    return hashlib.sha256(canonical(list(parts))).hexdigest()


def _topic_public(conn: Any, topic_id: str) -> bool:
    r = conn.execute("SELECT visibility FROM agora_topics WHERE topic_id = ?", (topic_id,)).fetchone()
    return bool(r) and r[0] == "public"


def _room_public(conn: Any, room_id: str) -> bool:
    r = conn.execute("SELECT room_type, encrypted FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    return bool(r) and r[0] == "public" and not r[1]


def _agora_topic(conn, r):
    return r["visibility"] == "public", r["topic_id"], {
        "topic_id": r["topic_id"], "slug": r["slug"], "creator_id": r["creator_id"],
        "content_hash": content_hash(r["name"], r["description"]), "created_at": r["created_at"]}


def _agora_post(conn, r):
    return _topic_public(conn, r["topic_id"]), r["post_id"], {
        "post_id": r["post_id"], "topic_id": r["topic_id"], "author_id": r["author_id"],
        "content_hash": content_hash(r["title"], r["content"]), "signature": r["signature"] or "",
        "created_at": r["created_at"]}


def _agora_comment(conn, r):
    post = conn.execute("SELECT topic_id FROM agora_posts WHERE post_id = ?", (r["post_id"],)).fetchone()
    return bool(post) and _topic_public(conn, post[0]), r["comment_id"], {
        "comment_id": r["comment_id"], "post_id": r["post_id"], "author_id": r["author_id"],
        "content_hash": content_hash(r["content"]), "signature": r["signature"] or "", "created_at": r["created_at"]}


def _listing(conn, r):
    return True, r["listing_id"], {
        "listing_id": r["listing_id"], "agent_id": r["agent_id"], "agent_stable_id": r["agent_stable_id"] or "",
        "listing_type": r["listing_type"], "content_hash": content_hash(r["title"], r["description"]),
        "signature": r["signature"] or "", "created_at": r["created_at"]}


def _room_act(conn, r):
    return _room_public(conn, r["room_id"]), r["act_id"], {
        "act_id": r["act_id"], "room_id": r["room_id"], "act": r["act"], "by_agent": r["by_agent"],
        "payload_hash": content_hash(r["payload_json"]), "signature": r["signature"] or "", "at": r["at"]}


def _advert(id_col: str, at_col: str):
    def build(conn, r):
        return True, r[id_col], {id_col: r[id_col], "stable_id": r["stable_id"], "signature": r["signature"],
                                 at_col: r[at_col]}
    return build


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


#: kind -> (table, extra WHERE, builder)
SOURCES: dict[str, tuple[str, str, Callable]] = {
    "agora_topic": ("agora_topics", "", _agora_topic),
    "agora_post": ("agora_posts", "", _agora_post),
    "agora_comment": ("agora_comments", "", _agora_comment),
    "listing": ("listings", "AND federated_from IS NULL", _listing),   # a peer's listings are in its own log
    "room_act": ("room_acts", "", _room_act),
    "room_advert": ("room_adverts", "", _advert("room_id", "advertised_at")),
    "topic_advert": ("topic_adverts", "", _advert("topic_id", "advertised_at")),
    "session_offer": ("session_offers", "", _advert("session_id", "offered_at")),
    "deletion": ("deletions", "", _deletion),
    "session_action": ("actions", "", _action),
    "game_chat": ("game_chat", "", _game_chat),
}

_FOLD_BATCH = 5000


def fold_records(conn: Any) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    for kind, (table, where, build) in SOURCES.items():
        if table not in tables:
            continue
        seen = conn.execute("SELECT last_rowid FROM translog_sources WHERE source = ?", (kind,)).fetchone()
        last = seen[0] if seen else 0
        while True:
            rows = conn.execute(f"SELECT rowid AS _r, * FROM {table} WHERE rowid > ? {where} "  # noqa: S608
                                "ORDER BY rowid LIMIT ?", (last, _FOLD_BATCH)).fetchall()
            for r in rows:
                public, ident, fields = build(conn, r)
                if public:
                    append(conn, kind, f"{kind}:{ident}", fields)
                else:
                    c = commitment(kind, fields)
                    append(conn, kind, c, {"commitment": c})
                last = r["_r"]
            if len(rows) < _FOLD_BATCH:
                break
        conn.execute("INSERT INTO translog_sources (source, last_rowid) VALUES (?, ?) "
                     "ON CONFLICT(source) DO UPDATE SET last_rowid = excluded.last_rowid", (kind, last))


def withdrawn(conn: Any, kind: str, ident: str) -> None:
    """A withdrawn advert or offer is a DELETE, which no fold sees: logged here, in its transaction."""
    append(conn, f"{kind}_withdrawn", f"{kind}_withdrawn:{ident}", {"id": ident})
