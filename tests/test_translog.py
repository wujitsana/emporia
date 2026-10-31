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
    _register_keyed(client, "tl-alpha")
    _rotate_keyed(client, "tl-alpha")
    stable_id = client.get("/agents/tl-alpha", headers={"x-emporia-agent-id": "tl-alpha"}).json()["stable_id"]
    state = _state(client)
    assert identity.verify_raw(translog.state_bytes(state), state["signature"], state["relay_public_key"])
    found = [e for e in client.get("/log/entries", params={"start": 0, "limit": 1000}).json()["entries"]
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
    c = translog.commitment("payment", {k: pay[k] for k in translog.PAYMENT_FIELDS})
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
        translog.fold_chain_heads(conn)
        added = translog.entries(conn, before)
        translog.fold_chain_heads(conn)
        assert translog.size(conn) == before + len(added)            # unchanged chains add nothing
    pub = [e for e in added if e["record"].get("id") == "room_tl_pub"]
    assert pub and pub[0]["record"]["head"] == "ab" * 32
    secret = translog.commitment("room_chain", {"chain": "room", "id": "room_tl_priv", "head": "ab" * 32, "count": 1})
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
        topic_id = conn.execute("SELECT topic_id FROM agora_topics WHERE slug = ?", (slug,)).fetchone()[0]
        conn.execute("INSERT INTO agora_posts (post_id, topic_id, author_id, title, content, created_at) "
                     "VALUES ('post_tl', ?, 'tl-gamma', 'hello', 'world', ?)", (topic_id, now))
        conn.execute("INSERT INTO actions (action_id, session_id, agent_id, action_type, payload_json, created_at) "
                     "VALUES ('act_tl', 'sess_tl', 'tl-gamma', 'move', '{\"bid\": 3}', ?)", (now,))
        conn.execute("INSERT INTO deletions (kind, item, deleted_by, role, deleted_at, signature) "
                     "VALUES ('post', 'post_tl_old', 'tl-gamma', 'author', ?, '')", (now,))
        before = translog.size(conn)
        translog.fold_records(conn)
        added = translog.entries(conn, before, limit=1000)
        translog.fold_records(conn)
        assert translog.size(conn) == before + len(added)
    refs = {e["record"].get("commitment") or e["record"].get("item_hash") or e["record"]["kind"] for e in added}
    assert {"agora_topic", "agora_post"} <= refs
    [post] = [e for e in added if e["record"].get("post_id") == "post_tl"]
    assert post["record"]["content_hash"] == translog.content_hash("hello", "world")
    assert "world" not in str(post)
    move = translog.commitment("session_action", {
        "action_id": "act_tl", "session_id": "sess_tl", "agent_id": "tl-gamma", "action_type": "move",
        "payload_hash": translog.content_hash('{"bid": 3}'), "created_at": now})
    assert move in refs and "sess_tl" not in str(added)
    assert translog.content_hash("post", "post_tl_old") in refs and "post_tl_old" not in str(added)
    state = _state(client)
    _check_in_log(client, post["index"], state)


# --- P258: small storage, things changed in place, and the log turned off -----

def test_entries_are_found_by_ref(client):
    _register_keyed(client, "tl-delta")
    _rotate_keyed(client, "tl-delta")
    with rs.DB_LOCK, rs.get_db() as conn:
        [step] = [e for e in translog.entries(conn, 0, limit=1000)
                  if e["record"]["kind"] == "agent_key" and e["record"]["step"].get("old") ==
                  conn.execute("SELECT old_key FROM agent_key_log ORDER BY rowid DESC LIMIT 1").fetchone()[0]]
        r = step["record"]
        ref = f"agent_key:{r['stable_id']}:{r['step_no']}:{r['step']['at']}"
        assert [e["index"] for e in translog.entries(conn, ref=ref)] == [step["index"]]
        assert translog.leaf_at(conn, step["index"]).hex() == step["leaf_hash"]


def test_a_changed_profile_and_a_withdrawn_advert_are_new_entries():
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO authorized_agents (agent_id, public_key_hex, stable_id, registered_at) "
                     "VALUES ('tl-eps', ?, 'sid_eps', '2026-10-31')", ("cd" * 32,))
        conn.execute("INSERT INTO room_adverts (room_id, stable_id, public_key_hex, signature, advertised_at) "
                     "VALUES ('room_tl_ad', 'sid_eps', ?, 'sig', '2026-10-31')", ("cd" * 32,))
        translog.fold_current(conn)
        before = translog.size(conn)
        translog.fold_current(conn)
        assert translog.size(conn) == before                                  # nothing changed, nothing added
        conn.execute("UPDATE authorized_agents SET x402_pay_to_json = '{\"base\": \"0xthief\"}' "
                     "WHERE agent_id = 'tl-eps'")
        conn.execute("DELETE FROM room_adverts WHERE room_id = 'room_tl_ad'")
        translog.fold_current(conn)
        kinds = [e["record"]["kind"] for e in translog.entries(conn, before)]
    assert sorted(kinds) == ["agent", "room_advert_gone"]


def test_a_relay_used_without_a_log_starts_one_with_everything_it_holds(tmp_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "used.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE agora_topics (topic_id, slug, name, description, visibility, creator_id, created_at);
        CREATE TABLE agora_posts (post_id, topic_id, author_id, title, content, signature, created_at);
        CREATE TABLE payments (payment_id, agent_id, amount_cents, currency, payment_type, session_id, room_id,
                               rail, created_at);""")
    conn.execute("INSERT INTO agora_topics VALUES ('t1', 'old', 'Old', '', 'public', 'a', '2026-01-01')")
    for i in range(3):
        conn.execute("INSERT INTO agora_posts VALUES (?, 't1', 'a', 'hi', 'there', '', '2026-01-02')", (f"p{i}",))
    conn.execute("INSERT INTO payments VALUES ('pay1', 'a', 5, 'usd', 'room_entry', NULL, 'r', 'stripe', '2026-01-03')")
    translog.init_schema(conn)                          # the log arrives on a relay months old
    state = translog.sign_state(conn, lambda b: "sig", "ab" * 32)
    kinds = sorted(e["record"]["kind"] for e in translog.entries(conn, 0, limit=1000))
    assert kinds == ["agora_post"] * 3 + ["agora_topic", "payment"]
    assert state["size"] == 5
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
    assert client.get("/health").json()["transparency_log"] is False
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
