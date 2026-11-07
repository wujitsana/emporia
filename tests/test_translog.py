"""P256: the relay's transparency log — RFC 9162 hashing and proofs, every key
change and payment in it in the same transaction, chain heads folded in, signed
states, and public routes to check it all."""
import os

import pytest
from fastapi.testclient import TestClient

from emporia import identity, translog
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _rotate_keyed

# RFC 6962 / 9162 reference tree (the certificate-transparency test data): eight
# leaves and the root of each prefix of them.
_DATA = [b"", b"\x00", b"\x10", b"\x20\x21", b"\x30\x31", b"\x40\x41\x42\x43", bytes(range(0x50, 0x58)),
         bytes(range(0x60, 0x70))]
_ROOTS = [
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]


def test_roots_match_the_rfc_reference_tree():
    leaves = [translog.leaf_hash(d) for d in _DATA]
    assert [translog.root(leaves[:n]).hex() for n in range(1, 9)] == _ROOTS
    assert translog.root([]).hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_every_inclusion_and_consistency_proof_checks_and_a_wrong_one_does_not():
    for n in range(1, 34):
        leaves = [translog.leaf_hash(os.urandom(4)) for _ in range(n)]
        top = translog.root(leaves)
        for i in range(n):
            proof = translog.inclusion_proof(i, leaves)
            assert translog.verify_inclusion(i, n, leaves[i], proof, top)
            assert not translog.verify_inclusion(i, n, translog.leaf_hash(b"other"), proof, top)
        for m in range(1, n + 1):
            proof = translog.consistency_proof(m, leaves)
            assert translog.verify_consistency(m, n, translog.root(leaves[:m]), top, proof)
            if m < n:
                # a log that rewrote an old leaf is not consistent with what it showed before
                rewritten = [translog.leaf_hash(b"rewritten"), *leaves[1:]]
                assert not translog.verify_consistency(m, n, translog.root(leaves[:m]), translog.root(rewritten),
                                                       proof)


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51460))


def _state(client) -> dict:
    rs.sign_log_state()
    r = client.get("/log/state")
    assert r.status_code == 200, r.text
    return r.json()


def _check_in_log(client, index: int, state: dict) -> None:
    proof = client.get("/log/proof/inclusion", params={"index": index, "size": state["size"]}).json()
    assert translog.verify_inclusion(index, state["size"], bytes.fromhex(proof["leaf_hash"]),
                                     [bytes.fromhex(h) for h in proof["proof"]], bytes.fromhex(state["root"]))


def test_a_key_change_is_in_the_log_and_provably_so(client):
    rs.sign_log_state()
    with rs.DB_LOCK, rs.get_db() as conn:
        before = translog.size(conn)          # the suite's log is long by now: read from here
    _register_keyed(client, "tl-alpha")
    _rotate_keyed(client, "tl-alpha")
    stable_id = client.get("/agents/tl-alpha", headers={"x-emporia-agent-id": "tl-alpha"}).json()["stable_id"]
    state = _state(client)
    assert identity.verify_raw(translog.state_bytes(state), state["signature"], state["relay_public_key"])
    found = [e for e in client.get("/log/entries", params={"start": before, "limit": 1000}).json()["entries"]
             if e["record"].get("stable_id") == stable_id and e["record"]["kind"] == "agent_key"]
    assert len(found) == 1
    assert found[0]["record"]["step"]["by"] == "key"
    assert found[0]["leaf_hash"] == translog.record_leaf_hash(found[0]["record"]).hex()
    _check_in_log(client, found[0]["index"], state)


def test_a_later_state_is_consistent_with_an_earlier_one(client):
    _register_keyed(client, "tl-beta")
    _rotate_keyed(client, "tl-beta")
    first = _state(client)
    _rotate_keyed(client, "tl-beta")
    second = _state(client)
    assert second["size"] > first["size"]
    proof = client.get("/log/proof/consistency", params={"first": first["size"], "second": second["size"]}).json()
    assert translog.verify_consistency(first["size"], second["size"], bytes.fromhex(first["root"]),
                                       bytes.fromhex(second["root"]), [bytes.fromhex(h) for h in proof["proof"]])
    # nothing new: no new state
    assert rs.sign_log_state() is None


def test_a_payment_goes_in_as_a_commitment_its_holder_can_find(client):
    pay = rs.record_payment("pi_tl_1", "tl-payer", 250, "room_entry", room_id="room_tl", rail="stripe")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (pay["payment_id"],)).fetchone()
        c = translog.commitment_for(conn, "payment", pay["payment_id"], {k: row[k] for k in translog.PAYMENT_FIELDS})
    # P263: salted — the fields alone, which a guesser could know, do not give it
    assert translog.commitment("payment", {k: row[k] for k in translog.PAYMENT_FIELDS}) != c
    [entry] = client.get("/log/entries", params={"ref": c}).json()["entries"]
    assert entry["record"] == {"commitment": c, "kind": "payment", "logged_at": entry["record"]["logged_at"]}
    assert "tl-payer" not in str(entry)
    state = _state(client)
    _check_in_log(client, entry["index"], state)


def test_chain_heads_are_folded_in_once_per_change_public_by_id_private_by_commitment():
    with rs.DB_LOCK, rs.get_db() as conn:
        now = "2026-10-31T00:00:00+00:00"
        for rid, kind in (("room_tl_pub", "public"), ("room_tl_priv", "private")):
            conn.execute("INSERT INTO rooms (room_id, name, room_type, creator_id, created_at, updated_at) "
                         "VALUES (?, ?, ?, 'x', ?, ?)", (rid, rid, kind, now, now))
            conn.execute("INSERT INTO room_messages (message_id, room_id, sender_id, content, chain_hash, created_at) "
                         "VALUES (?, ?, 'x', 'hi', ?, ?)", (f"m_{rid}", rid, "ab" * 32, now))
        before = translog.size(conn)
        translog.fold_all(conn)
        added = translog.entries(conn, before, limit=1000)
        n = translog.size(conn)
        translog.backfill(conn)                                      # every chain again: unchanged ones add nothing
        translog.fold_all(conn)
        assert translog.size(conn) == n
        secret = translog.commitment_for(conn, "room_chain", "room_tl_priv",
                                         {"chain": "room", "id": "room_tl_priv", "head": "ab" * 32, "count": 1})
    pub = [e for e in added if e["record"].get("id") == "room_tl_pub"]
    assert pub and pub[0]["record"]["head"] == "ab" * 32
    assert [e["record"]["kind"] for e in added if e["record"].get("commitment") == secret] == ["private_chain"]
    assert "room_tl_priv" not in str(added)


def test_proofs_are_only_given_against_a_signed_state(client):
    assert client.get("/log/proof/inclusion", params={"index": 0, "size": 999_999}).status_code == 404
    assert client.get("/log/state", params={"size": 999_999}).status_code == 404


# --- P257: everything else, and proofs from stored nodes ----------------------

def test_stored_nodes_give_the_same_root_and_proofs_as_the_whole_tree(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "log.db")
    conn.row_factory = sqlite3.Row
    translog.init_schema(conn)
    for i in range(37):
        translog.append(conn, "test", f"t:{i}", {"i": i})
    leaves = translog.leaves(conn, 37)
    for n in (1, 2, 5, 8, 16, 33, 37):
        assert translog.stored_root(conn, n) == translog.root(leaves[:n])
        for i in range(n):
            assert translog.stored_inclusion_proof(conn, i, n) == translog.inclusion_proof(i, leaves[:n])
        for m in range(1, n + 1):
            assert translog.stored_consistency_proof(conn, m, n) == translog.consistency_proof(m, leaves[:n])


def test_posts_listings_moves_and_removals_are_folded_in_once(client):
    _register_keyed(client, "tl-gamma")
    slug = client.post("/agoras/topics", json={"name": "tl gamma topic", "visibility": "public"},
                       headers={"x-emporia-agent-id": "tl-gamma"}).json()["slug"]
    now = "2026-10-31T00:00:00+00:00"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO posts (post_id, author_id, author_stable_id, author_public_key_hex, origin_relay, "
                     "nonce, kind, label, text, tags_json, topic, at, signature, recorded_at) "
                     "VALUES ('post_tl', 'tl-gamma', 'sid', '', 'o', 'n', 'note', 'hello', 'world', '[]', ?, ?, 'sig', ?)",
                     (slug, now, now))
        conn.execute("INSERT INTO actions (action_id, session_id, agent_id, action_type, payload_json, created_at) "
                     "VALUES ('act_tl', 'sess_tl', 'tl-gamma', 'move', '{\"bid\": 3}', ?)", (now,))
        conn.execute("INSERT INTO deletions (kind, item, deleted_by, role, deleted_at, signature) "
                     "VALUES ('post', 'post_tl_old', 'tl-gamma', 'author', ?, '')", (now,))
        before = translog.size(conn)
        translog.fold_all(conn)
        added = translog.entries(conn, before, limit=1000)
        n = translog.size(conn)
        translog.backfill(conn)
        translog.fold_all(conn)
        assert translog.size(conn) == n                              # folded in once
        move = translog.commitment_for(conn, "session_action", "act_tl", {
            "action_id": "act_tl", "session_id": "sess_tl", "agent_id": "tl-gamma", "action_type": "move",
            "payload_hash": translog.content_hash('{"bid": 3}'), "created_at": now})
    refs = {e["record"].get("commitment") or e["record"].get("item_hash") or e["record"]["kind"] for e in added}
    assert {"agora_topic", "post"} <= refs
    [post] = [e for e in added if e["record"].get("post_id") == "post_tl"]
    assert post["record"]["content_hash"] == translog.content_hash("hello", "world", "[]")
    assert "world" not in str(post)
    assert move in refs and "sess_tl" not in str(added)
    assert translog.content_hash("post", "post_tl_old") in refs and "post_tl_old" not in str(added)
    state = _state(client)
    _check_in_log(client, post["index"], state)


# --- P258: small storage, things changed in place, and the log turned off -----

def test_entries_are_found_by_ref(client):
    _register_keyed(client, "tl-delta")
    _rotate_keyed(client, "tl-delta")
    stable_id = client.get("/agents/tl-delta", headers={"x-emporia-agent-id": "tl-delta"}).json()["stable_id"]
    with rs.DB_LOCK, rs.get_db() as conn:
        at = conn.execute("SELECT at FROM agent_key_log WHERE stable_id = ? AND step_no = 0", (stable_id,)).fetchone()[0]
        [step] = translog.entries(conn, ref=f"agent_key:{stable_id}:0:{at}")
        assert step["record"]["stable_id"] == stable_id and step["record"]["step"]["by"] == "key"
        assert translog.leaf_at(conn, step["index"]).hex() == step["leaf_hash"]


def test_a_changed_profile_and_a_withdrawn_offer_are_new_entries():
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO authorized_agents (agent_id, public_key_hex, stable_id, registered_at) "
                     "VALUES ('tl-eps', ?, 'sid_eps', '2026-10-31')", ("cd" * 32,))
        conn.execute("INSERT INTO session_offers (session_id, stable_id, public_key_hex, signature, offered_at) "
                     "VALUES ('sess_tl_offer', 'sid_eps', ?, 'sig', '2026-10-31')", ("cd" * 32,))
        translog.fold_all(conn)
        before = translog.size(conn)
        translog.backfill(conn)
        translog.fold_all(conn)
        assert translog.size(conn) == before                                  # nothing changed, nothing added
        conn.execute("UPDATE authorized_agents SET x402_pay_to_json = '{\"base\": \"0xthief\"}' "
                     "WHERE agent_id = 'tl-eps'")
        conn.execute("DELETE FROM session_offers WHERE session_id = 'sess_tl_offer'")
        translog.fold_all(conn)
        kinds = [e["record"]["kind"] for e in translog.entries(conn, before)]
    assert sorted(kinds) == ["agent", "session_offer_gone"]


def test_a_relay_used_without_a_log_starts_one_with_everything_it_holds(tmp_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "used.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE agora_topics (topic_id, slug, name, description, visibility, creator_id, created_at);
        CREATE TABLE payments (payment_id, agent_id, amount_cents, currency, payment_type, session_id, room_id,
                               rail, created_at);""")
    conn.execute("INSERT INTO agora_topics VALUES ('t1', 'old', 'Old', '', 'public', 'a', '2026-01-01')")
    conn.execute("INSERT INTO payments VALUES ('pay1', 'a', 5, 'usd', 'room_entry', NULL, 'r', 'stripe', '2026-01-03')")
    translog.init_schema(conn)                          # the log arrives on a relay months old
    state = translog.sign_state(conn, lambda b: "sig", "ab" * 32)
    kinds = sorted(e["record"]["kind"] for e in translog.entries(conn, 0, limit=1000))
    assert kinds == ["agora_topic", "payment"]
    assert state["size"] == 2
    assert translog.sign_state(conn, lambda b: "sig", "ab" * 32) is None      # and only once


def test_turned_off_the_relay_logs_nothing_and_says_so(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    with rs.DB_LOCK, rs.get_db() as conn:
        before = translog.size(conn)
    rs.record_payment("pi_tl_off", "tl-payer", 100, "room_entry", room_id="room_tl", rail="stripe")
    assert rs.sign_log_state() is None
    with rs.DB_LOCK, rs.get_db() as conn:
        assert translog.size(conn) == before
    assert client.get("/log/state").status_code == 404
    assert "log" not in client.get("/health").json()["relay_modules"]
    # turned on again, what it missed is folded in from where it stopped
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert rs.sign_log_state() is not None
    with rs.DB_LOCK, rs.get_db() as conn:
        assert any(e["record"]["kind"] == "payment" for e in translog.entries(conn, before))


def test_setup_log_turns_it_off_and_on_in_the_relay_profile(tmp_path, monkeypatch, capsys):
    from emporia import cli
    (tmp_path / ".env").write_text("EMPORIA_AGENT_ID=relay\n")
    monkeypatch.setattr(cli, "this_profile", lambda: tmp_path)
    assert cli.main(["setup", "log", "off"]) == 0
    assert "EMPORIA_TRANSPARENCY_LOG=off" in (tmp_path / ".env").read_text()
    assert cli.main(["setup", "log", "on"]) == 0
    assert "EMPORIA_TRANSPARENCY_LOG=on" in (tmp_path / ".env").read_text()
    assert "emporia relay restart" in capsys.readouterr().out


def test_every_table_is_logged_or_says_why_not():
    with rs.DB_LOCK, rs.get_db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    covered = ({t for t, _, _, _ in translog.SOURCES.values()} | {t for t, _ in translog.CHAINS.values()}
               | set(translog.NOT_LOGGED))
    missing = {t for t in tables if not t.startswith(("translog_", "sqlite_"))} - covered
    assert not missing, f"tables neither in the transparency log nor in translog.NOT_LOGGED: {sorted(missing)}"


# --- P263: one ingest for rows added, changed and removed ---------------------

def _fresh_log(tmp_path, ddl: str):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "p263.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(ddl)
    translog.init_schema(conn)
    return conn


_POSTS = ("CREATE TABLE posts (post_id TEXT PRIMARY KEY, author_id, author_stable_id, kind, label, text, tags_json, "
          "topic, reply_to, quote_of, root, nonce, signature, at, is_deleted, federated_from);")
_SESSIONS = "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, module_type, status, participants_json, created_at);"


def _add_post(conn, post_id, text="t"):
    """A public post: the simplest row the log follows."""
    conn.execute("INSERT OR REPLACE INTO posts VALUES (?, 'a', 's', 'note', 'l', ?, '[]', NULL, NULL, NULL, NULL, 'n', "
                 "'sig', 'now', 0, NULL)", (post_id, text))


def _kinds(conn, start=0):
    return [e["record"]["kind"] for e in translog.entries(conn, start, limit=1000)]


def test_a_row_taking_a_removed_rows_rowid_is_still_logged(tmp_path):
    """The audit's case: delete the newest post, add one, fold. Before P263 the
    new one took the old rowid and was never logged."""
    conn = _fresh_log(tmp_path, _POSTS)
    _add_post(conn, "L1"); _add_post(conn, "L2")
    translog.fold_all(conn)
    conn.execute("DELETE FROM posts WHERE post_id = 'L2'")
    _add_post(conn, "L3")
    translog.fold_all(conn)
    logged = [e["record"].get("post_id") for e in translog.entries(conn, 0) if e["record"]["kind"] == "post"]
    assert logged == ["L1", "L2", "L3"] and _kinds(conn).count("post_gone") == 1


def test_a_refund_is_a_new_entry_not_a_silent_edit(tmp_path):
    conn = _fresh_log(tmp_path, "CREATE TABLE payments (payment_id, agent_id, amount_cents, currency, payment_type, "
                                "session_id, room_id, rail, status, created_at);")
    conn.execute("INSERT INTO payments VALUES ('pay1', 'a', 500, 'usd', 'room_entry', NULL, 'r', 'stripe', "
                 "'confirmed', 'now')")
    translog.fold_all(conn)
    first = translog.size(conn)
    conn.execute("UPDATE payments SET status = 'refunded' WHERE payment_id = 'pay1'")
    translog.fold_all(conn)
    assert translog.size(conn) == first + 1 and _kinds(conn, first) == ["payment"]
    conn.execute("UPDATE payments SET status = 'refunded' WHERE payment_id = 'pay1'")    # no change: nothing
    translog.fold_all(conn)
    assert translog.size(conn) == first + 1


def test_a_chain_rewritten_to_the_same_length_is_logged_again(tmp_path):
    """P258 compared a chain's count, so replacing the last message kept it out."""
    conn = _fresh_log(tmp_path, "CREATE TABLE dm_messages (message_id, thread_id, chain_hash, created_at);")
    conn.execute("INSERT INTO dm_messages VALUES ('m1', 'dm_x', 'h1', '2026-11-01')")
    translog.fold_all(conn)
    n = translog.size(conn)
    conn.execute("UPDATE dm_messages SET chain_hash = 'h1-edited' WHERE message_id = 'm1'")
    translog.fold_all(conn)
    assert translog.size(conn) == n + 1 and _kinds(conn, n) == ["private_chain"]


def test_a_large_backlog_is_folded_in_batches(tmp_path):
    conn = _fresh_log(tmp_path, "CREATE TABLE deletions (kind, item, role, deleted_at, signature);")
    conn.executemany("INSERT INTO deletions VALUES ('post', ?, 'author', 'now', '')", [(f"p{i}",) for i in range(4500)])
    assert translog.waiting(conn) == 4500
    assert translog.drain(conn) == translog.DRAIN_BATCH and translog.waiting(conn) == 4500 - translog.DRAIN_BATCH
    translog.fold_all(conn)
    assert translog.waiting(conn) == 0 and translog.size(conn) == 4500


def test_a_log_from_the_scanning_ingest_is_rebuilt_once(tmp_path):
    conn = _fresh_log(tmp_path, "CREATE TABLE deletions (kind, item, role, deleted_at, signature);")
    conn.execute("INSERT INTO deletions VALUES ('post', 'p1', 'author', 'now', '')")
    translog.fold_all(conn)
    conn.execute("UPDATE translog_seen SET digest = 'scan-v0' WHERE key = 'ingest'")   # as P258-P262 left it
    translog.init_schema(conn)
    translog.fold_all(conn)
    assert _kinds(conn) == ["deletion"]                       # rebuilt from what the relay holds, once
    translog.init_schema(conn)
    translog.fold_all(conn)
    assert _kinds(conn) == ["deletion"]


def test_a_backfilled_entry_says_so(tmp_path):
    """P267: a row the relay held before its log began is logged with `backfilled`,
    so its `logged_at` (now) does not pass for when it happened."""
    conn = _fresh_log(tmp_path, "CREATE TABLE deletions (kind, item, role, deleted_at, signature);")
    conn.execute("INSERT INTO deletions VALUES ('post', 'p_new', 'author', 'now', '')")
    translog.fold_all(conn)
    [entry] = translog.entries(conn, 0)
    assert "backfilled" not in entry["record"]
    import sqlite3
    old = sqlite3.connect(tmp_path / "old.db")
    old.row_factory = sqlite3.Row
    old.executescript("CREATE TABLE deletions (kind, item, role, deleted_at, signature);")
    old.execute("INSERT INTO deletions VALUES ('post', 'p_old', 'author', '2025-01-01', '')")
    translog.init_schema(old)
    translog.fold_all(old)
    [entry] = translog.entries(old, 0)
    assert entry["record"]["backfilled"] is True


def test_a_row_saved_again_by_replace_is_one_record_that_changed_or_did_not(tmp_path):
    """P268: a session is saved with INSERT OR REPLACE on every move, which gives
    the row a new rowid each time. Followed by its key, an unchanged record adds
    nothing and a changed one is one new entry — never a "gone" and a "new"."""
    conn = _fresh_log(tmp_path, _POSTS)

    def save(text):
        _add_post(conn, "p_replaced", text)
        translog.fold_all(conn)
    save("First")
    n = translog.size(conn)
    for _ in range(5):
        save("First")                                   # a move that changed nothing logged
    assert translog.size(conn) == n
    save("Second")
    assert [e["record"]["kind"] for e in translog.entries(conn, n)] == ["post"]
    conn.execute("DELETE FROM posts WHERE post_id = 'p_replaced'")
    translog.fold_all(conn)
    assert [e["record"]["kind"] for e in translog.entries(conn, n)] == ["post", "post_gone"]


def test_a_renamed_agent_is_the_old_record_gone_and_a_new_one(tmp_path):
    conn = _fresh_log(tmp_path, "CREATE TABLE authorized_agents (agent_id TEXT PRIMARY KEY, public_key_hex, stable_id, "
                                "display_name, is_active, x402_pay_to_json, stripe_account_id, encryption_public_key, "
                                "encryption_key_signature, accepts_remote_dms, relay_list_json, x402_pay_to_proofs_json);")
    conn.execute("INSERT INTO authorized_agents (agent_id, public_key_hex, stable_id) VALUES ('old_name', 'k', 's')")
    translog.fold_all(conn)
    n = translog.size(conn)
    conn.execute("UPDATE authorized_agents SET agent_id = 'new_name' WHERE agent_id = 'old_name'")
    translog.fold_all(conn)
    assert sorted(e["record"]["kind"] for e in translog.entries(conn, n)) == ["agent", "agent_gone"]


def test_a_rebuild_is_a_new_epoch_handed_over_and_signed_with_the_same_secret(tmp_path):
    """P269: a rebuild (a new ingest) used to wipe the log, its signed states and the
    commit secret. Now the secret is kept (a private record's salt never changes),
    the old log's last state is kept, and the new log's first state signs a
    hand-over naming it."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    sign = lambda b: key.sign(b).hex()                                   # noqa: E731
    conn = _fresh_log(tmp_path, _POSTS + _SESSIONS)
    _add_post(conn, "l1")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'chess', 'active', '[]', 'now')")
    first = translog.sign_state(conn, sign, pub)
    assert first["epoch"] == 1 and identity.verify_raw(translog.state_bytes(first), first["signature"], pub)
    salt = translog.record_salt(conn, "session", "s1")
    conn.execute("UPDATE translog_seen SET digest = 'outbox-v1' WHERE key = 'ingest'")    # as P268b found it
    translog.init_schema(conn)
    assert translog.record_salt(conn, "session", "s1") == salt
    second = translog.sign_state(conn, sign, pub)
    assert second["epoch"] == 2 and translog.latest_state(conn) == second
    [one, two] = translog.epochs(conn)
    assert one["epoch"] == 1 and one["handover"] is None
    assert two["previous"] == first and "outbox-v1 -> " in two["reason"]
    h = two["handover"]
    assert h["previous"]["root"] == first["root"] and h["first"]["root"] == second["root"]
    assert identity.verify_raw(translog.handover_bytes(h), h["signature"], pub)
    translog.init_schema(conn)                                           # not rebuilt again: same epoch
    assert translog.epoch(conn) == 2 and len(translog.epochs(conn)) == 2


def test_a_log_kept_from_before_epochs_keeps_its_states_as_signed(tmp_path):
    conn = _fresh_log(tmp_path, _POSTS)
    _add_post(conn, "l1")
    state = translog.sign_state(conn, lambda b: "sig", "ab" * 32)
    conn.execute("UPDATE translog_states SET epoch = NULL")             # signed before P269
    conn.execute("DELETE FROM translog_epochs")
    translog.init_schema(conn)
    assert "epoch" not in translog.latest_state(conn)
    assert {k: v for k, v in state.items() if k != "epoch"} == translog.latest_state(conn)
    assert translog.epochs(conn)[0]["reason"] == "kept from before epochs were recorded"


def test_a_row_removed_while_the_log_was_off_is_logged_gone_when_it_is_on_again(tmp_path, monkeypatch):
    conn = _fresh_log(tmp_path, _POSTS)
    _add_post(conn, "l1")
    _add_post(conn, "l2")
    translog.fold_all(conn)
    n = translog.size(conn)
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    translog.init_schema(conn)
    conn.execute("DELETE FROM posts WHERE post_id = 'l1'")         # no trigger saw this
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    translog.init_schema(conn)
    translog.fold_all(conn)
    assert _kinds(conn, n) == ["post_gone"]                         # l2, unchanged, adds nothing


def test_a_row_with_no_key_is_refused_not_silently_left_out(tmp_path):
    """P269: SQLite lets a TEXT PRIMARY KEY hold NULL, and NULL || anything is NULL,
    so such a row would never reach the log. The log's trigger refuses it."""
    import sqlite3
    conn = _fresh_log(tmp_path, _POSTS)
    with pytest.raises(sqlite3.IntegrityError, match="no key"):
        _add_post(conn, None)


def test_every_logged_key_column_is_guarded_on_the_relays_own_schema():
    """Every SOURCES key column is NOT NULL in the relay's schema or caught by the trigger."""
    import sqlite3
    with rs.DB_LOCK, rs.get_db() as conn:
        for kind, (table, _b, _c, cols) in translog.SOURCES.items():
            info = {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
            assert all(c in info for c in cols), (kind, cols)
            if any(not info[c][3] for c in cols):                       # nullable: the trigger must refuse it
                sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?",
                                   (f"translog_{kind}_insert",)).fetchone()[0]
                assert "RAISE(ABORT" in sql and all(f"NEW.{c} IS NULL" in sql for c in cols), kind
    assert sqlite3   # imported for the type above


def test_a_private_records_removal_is_salted_a_public_ones_is_not(tmp_path):
    conn = _fresh_log(tmp_path, _POSTS + _SESSIONS)
    _add_post(conn, "l1")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'chess', 'active', '[]', 'now')")
    translog.fold_all(conn)
    n = translog.size(conn)
    conn.execute("DELETE FROM posts")
    conn.execute("DELETE FROM sessions")
    translog.fold_all(conn)
    gone = {e["record"]["kind"]: e["record"]["id_hash"] for e in translog.entries(conn, n)}
    assert gone["post_gone"] == translog.content_hash("post", "l1")        # public: anyone can check
    assert gone["session_gone"] != translog.content_hash("session", "s1")        # private: not from a guess
    assert gone["session_gone"] == translog.content_hash("session", "s1", translog.record_salt(conn, "session", "s1"))


def test_the_epochs_route(client):
    r = client.get("/log/epochs")
    assert r.status_code == 200 and r.json()["epoch"] >= 1 and r.json()["epochs"]


