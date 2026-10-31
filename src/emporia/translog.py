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
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translog_seen (
    key TEXT PRIMARY KEY,
    digest TEXT NOT NULL
) WITHOUT ROWID;
"""

_OLD_TABLES = ("translog_chain_heads", "translog_sources")


def enabled() -> bool:
    """P258: the log is on unless the operator turned it off (EMPORIA_TRANSPARENCY_LOG=off)."""
    import os
    return os.getenv("EMPORIA_TRANSPARENCY_LOG", "on").strip().lower() not in ("0", "off", "false", "no")


def init_schema(conn: Any) -> None:
    """The log's tables. A log in the P256-P257 shape is dropped and rebuilt:
    every source is folded in again from the start, so nothing the relay holds
    is lost — only the old signed states, which nobody outside could check yet."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(translog_leaves)").fetchall()}
    if "record_json" in cols:
        for t in ("translog_leaves", "translog_nodes", "translog_states", *_OLD_TABLES):
            conn.execute(f"DROP TABLE IF EXISTS {t}")  # noqa: S608
    conn.executescript(SCHEMA)


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
    return {"type": STATE_TYPE, "relay_public_key": r["relay_public_key"], "size": r["size"], "root": r["root"],
            "at": r["at"], "signature": r["signature"]}


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


def _log(conn: Any, kind: str, public: bool, ident: str, fields: dict[str, Any]) -> None:
    """Public: by id. Otherwise a commitment, filed under itself."""
    if public:
        append(conn, kind, f"{kind}:{ident}", fields)
    else:
        c = commitment(kind, fields)
        append(conn, kind, c, {"commitment": c})


def content_hash(*parts: Any) -> str:
    return hashlib.sha256(canonical(list(parts))).hexdigest()


# ─── what is folded in when a state is signed ─────────────────────────────────

def _last_head(conn: Any, table: str, column: str, ident: str) -> str | None:
    row = conn.execute(f"SELECT chain_hash FROM {table} WHERE {column} = ? AND chain_hash IS NOT NULL "  # noqa: S608
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (ident,)).fetchone()
    return row[0] if row else None


def _fold_chain(conn: Any, chain: str, ident: str, count: int, public: bool, table: str, column: str) -> None:
    key = f"{chain}:{ident}"
    if _seen(conn, key) == str(count):
        return
    head = _last_head(conn, table, column, ident)
    if not head:
        return
    fields = {"chain": chain, "id": ident, "head": head, "count": count}
    if public:
        append(conn, f"{chain}_chain", key, fields)
    else:
        c = commitment(f"{chain}_chain", fields)
        append(conn, "private_chain", c, {"commitment": c})
    _mark(conn, key, str(count))


def fold_chain_heads(conn: Any) -> None:
    """One leaf for each room or DM chain that grew since it was last logged:
    public rooms by id and head, everything else as a commitment that the
    participants (who hold the id, head and count) can recompute."""
    tables = _tables(conn)
    if "room_messages" in tables and "rooms" in tables:
        for r in conn.execute(
                "SELECT m.room_id, COUNT(*) n, r.room_type, r.encrypted FROM room_messages m "
                "JOIN rooms r ON r.room_id = m.room_id WHERE m.chain_hash IS NOT NULL GROUP BY m.room_id").fetchall():
            _fold_chain(conn, "room", r["room_id"], r["n"], r["room_type"] == "public" and not r["encrypted"],
                        "room_messages", "room_id")
    if "dm_messages" in tables:
        for r in conn.execute("SELECT thread_id, COUNT(*) n FROM dm_messages WHERE chain_hash IS NOT NULL "
                              "GROUP BY thread_id").fetchall():
            _fold_chain(conn, "dm", r["thread_id"], r["n"], False, "dm_messages", "thread_id")


def fold_relay_keys(conn: Any, history: list[dict[str, str]]) -> None:
    """A leaf for each change of the relay's own key not yet logged."""
    for step in history:
        key = f"relay_key:{step.get('at', '')}"
        if _seen(conn, key) is None:
            append(conn, "relay_key", key, {k: step.get(k, "") for k in ("old", "new", "at", "signature")})
            _mark(conn, key, "1")


def sign_state(conn: Any, sign: Callable[[bytes], str], relay_public_key: str,
               relay_key_history: list[dict[str, str]] = ()) -> dict[str, Any] | None:
    """Fold everything in, then sign the log's state — or return None when the
    log is off or has not grown since the last one."""
    if not enabled():
        return None
    fold_relay_keys(conn, list(relay_key_history))
    fold_chain_heads(conn)
    fold_records(conn)
    fold_current(conn)
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


# ─── P257-P258: records, by table ─────────────────────────────────────────────
# Two kinds of source. **Appended** tables only gain rows: each is read from the
# rowid where the last fold stopped. **Current** tables are changed in place (an
# agent's wallet, an advert re-signed or withdrawn, a game finishing): each row's
# digest is compared with the one last logged, and a change — or the row going —
# is a new entry. Public items go in by id with a hash of their content and the
# signer's signature; anything else as a commitment over the fields listed here,
# which those holding the record can recompute (`commitment(kind, fields)`) and
# find with `GET /log/entries?ref=`.

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


PAYMENT_FIELDS = ("payment_id", "agent_id", "amount_cents", "currency", "payment_type", "session_id", "room_id",
                  "rail", "created_at")
SETTLEMENT_FIELDS = ("settlement_id", "session_id", "winner_id", "payer_id", "total_stake_cents",
                     "winner_payout_cents", "rail", "created_at")


def _fields(names: tuple[str, ...], id_col: str):
    def build(conn, r):
        # who paid whom is not public: always a commitment
        return False, r[id_col], {k: r[k] for k in names}
    return build


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


#: appended tables — kind -> (table, extra WHERE, builder)
SOURCES: dict[str, tuple[str, str, Callable]] = {
    "agent_key": ("agent_key_log", "", _agent_key),
    "payment": ("payments", "", _fields(PAYMENT_FIELDS, "payment_id")),
    "settlement": ("settlements", "", _fields(SETTLEMENT_FIELDS, "settlement_id")),
    "agora_topic": ("agora_topics", "", _agora_topic),
    "agora_post": ("agora_posts", "", _agora_post),
    "agora_comment": ("agora_comments", "", _agora_comment),
    "listing": ("listings", "AND federated_from IS NULL", _listing),   # a peer's listings are in its own log
    "room_act": ("room_acts", "", _room_act),
    "deletion": ("deletions", "", _deletion),
    "session_action": ("actions", "", _action),
    "game_chat": ("game_chat", "", _game_chat),
    "session_message": ("messages", "", _session_message),
    "access_grant": ("access_entitlements", "", _whole_row("entitlement_id")),
    "access_removal": ("access_removals", "", _whole_row("removal_id")),
    "bounty_entry": ("bounty_entries", "", _whole_row("listing_id")),
    "participant": ("participants", "", _whole_row("session_id")),
    "agora_member": ("agora_members", "", _whole_row("topic_id")),
    "agora_invite": ("agora_invites", "", _whole_row("topic_id")),
    "room_invite": ("room_invites", "", _whole_row("room_id")),
}


def _agent(conn, r):
    # the profile others act on: keys, payout wallets, relays, name — hashed, so a
    # verifier compares it with /agents/{id} and a quiet change shows
    return True, r["stable_id"] or r["agent_id"], {
        "agent_id": r["agent_id"], "stable_id": r["stable_id"] or "", "public_key_hex": r["public_key_hex"],
        "profile_hash": content_hash(*(r[k] for k in AGENT_PROFILE_FIELDS))}


AGENT_PROFILE_FIELDS = ("agent_id", "public_key_hex", "display_name", "is_active", "x402_pay_to_json",
                        "stripe_account_id", "encryption_public_key", "encryption_key_signature", "stable_id",
                        "accepts_remote_dms", "relay_list_json")


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


def _event(conn, r):
    return True, r["event_id"], {"event_id": r["event_id"], "organizer_id": r["organizer_id"], "status": r["status"],
                                 "content_hash": content_hash(r["title"], r["description"], r["bracket_json"])}


#: current tables — kind -> (table, id column, builder). The digest is the
#: built record's hash, so only what is logged counts as a change.
CURRENT: dict[str, tuple[str, str, Callable]] = {
    "agent": ("authorized_agents", "agent_id", _agent),
    "room_advert": ("room_adverts", "room_id", _signed_by_owner("room_id", "advertised_at")),
    "topic_advert": ("topic_adverts", "topic_id", _signed_by_owner("topic_id", "advertised_at")),
    "session_offer": ("session_offers", "session_id", _signed_by_owner("session_id", "offered_at")),
    "escrow_hold": ("escrow_holds", "hold_hash", _escrow),
    "session": ("sessions", "session_id", _session),
    "event": ("events", "event_id", _event),
}

#: tables deliberately not in the log, and why
NOT_LOGGED: dict[str, str] = {
    "room_messages": "content; each room's chain head is logged instead (fold_chain_heads)",
    "dm_messages": "content; each conversation's chain head is logged instead, as a commitment",
    "dm_threads": "who talks to whom; the chain heads cover the conversations",
    "rooms": "a room's creation, settings, invites and kicks are its signed room_acts",
    "agora_votes": "tallies the relay keeps, not anyone's signed statement",
    "agent_inbox": "delivery bookkeeping",
    "reg_challenges": "short-lived sign-in nonces",
    "remote_agents": "agents of other relays, pinned here; in their own relay's log",
    "elsewhere_adverts": "adverts pulled from linked relays; in their own relay's log",
    "x402_settle_intents": "short-lived payment bookkeeping; the payment itself is logged",
    "agent_key_log_superseded": "copies of key steps a recovery undid; the steps were logged when made",
    "schema_migrations": "the database's own bookkeeping",
}

_FOLD_BATCH = 5000


def log_row(conn: Any, kind: str, rowid: int | None) -> None:
    """Log one row of an appended source now, in the caller's transaction — for
    key changes and money, which should never wait for the next fold."""
    if not enabled() or rowid is None:
        return
    table, _where, build = SOURCES[kind]
    r = conn.execute(f"SELECT rowid AS _r, * FROM {table} WHERE rowid = ?", (rowid,)).fetchone()  # noqa: S608
    if r is None:
        return
    _log(conn, kind, *build(conn, r))
    key = f"source:{kind}"
    if int(_seen(conn, key) or 0) < rowid:
        _mark(conn, key, str(rowid))


def fold_records(conn: Any) -> None:
    tables = _tables(conn)
    for kind, (table, where, build) in SOURCES.items():
        if table not in tables:
            continue
        key = f"source:{kind}"
        last = int(_seen(conn, key) or 0)
        while True:
            rows = conn.execute(f"SELECT rowid AS _r, * FROM {table} WHERE rowid > ? {where} "  # noqa: S608
                                "ORDER BY rowid LIMIT ?", (last, _FOLD_BATCH)).fetchall()
            for r in rows:
                _log(conn, kind, *build(conn, r))
                last = r["_r"]
            if len(rows) < _FOLD_BATCH:
                break
        _mark(conn, key, str(last))


def fold_current(conn: Any) -> None:
    tables = _tables(conn)
    for kind, (table, id_col, build) in CURRENT.items():
        if table not in tables:
            continue
        prefix = f"{kind}:"
        held = {r[0][len(prefix):]: r[1] for r in conn.execute(
            "SELECT key, digest FROM translog_seen WHERE key >= ? AND key < ?", (prefix, prefix + "￿")).fetchall()}
        for r in conn.execute(f"SELECT * FROM {table}").fetchall():  # noqa: S608
            ident = str(r[id_col])
            public, _, fields = build(conn, r)
            digest = content_hash(fields)
            if held.pop(ident, None) != digest:
                _log(conn, kind, public, ident, fields)
                _mark(conn, prefix + ident, digest)
        for ident in held:           # gone since it was last logged: withdrawn or removed
            gone = content_hash(kind, ident)
            append(conn, f"{kind}_gone", f"{kind}_gone:{gone}", {"id_hash": gone})
            conn.execute("DELETE FROM translog_seen WHERE key = ?", (prefix + ident,))
