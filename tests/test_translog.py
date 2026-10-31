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
             if e["record"].get("stable_id") == stable_id]
    assert [e["record"]["kind"] for e in found] == ["agent_key"]
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
    c = translog.commitment("payment", rs.payment_log_fields(
        pay["payment_id"], "tl-payer", 250, "usd", "room_entry", None, "room_tl", "stripe", pay["created_at"]))
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
    pub = [e for e in added if e["ref"] == "room:room_tl_pub"]
    assert pub and pub[0]["record"]["head"] == "ab" * 32
    secret = translog.commitment("room_chain", {"chain": "room", "id": "room_tl_priv", "head": "ab" * 32, "count": 1})
    assert [e["record"]["kind"] for e in added if e["ref"] == secret] == ["private_chain"]
    assert "room_tl_priv" not in str(added)


def test_proofs_are_only_given_against_a_signed_state(client):
    assert client.get("/log/proof/inclusion", params={"index": 0, "size": 999_999}).status_code == 404
    assert client.get("/log/state", params={"size": 999_999}).status_code == 404
