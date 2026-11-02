"""P271: an agent's encryption key can be replaced, and its peers follow it
forward only; the relay's sign-in secret can be replaced."""
import os
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from emporia import crypto_box, identity, wire
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    for var in (identity.ENCRYPTION_KEY_SOURCE_VAR, identity.RETIRED_ENC_KEYS_VAR):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _sealed_to(pub_hex: str, text: str = "hello") -> dict:
    return crypto_box.seal(text, {"bob": pub_hex}, b"aad")


def test_a_replaced_key_still_opens_what_was_sealed_to_it(keys):
    old_pub = identity.get_encryption_public_key_hex("bob")
    before = _sealed_to(old_pub, "before")
    old, new = identity.rotate_encryption_keypair("bob")
    assert old == old_pub and new != old and identity.get_encryption_public_key_hex("bob") == new
    after = _sealed_to(new, "after")
    keyring = identity.encryption_private_keys("bob")
    assert len(keyring) == 2
    assert crypto_box.open_sealed(before, "bob", keyring, b"aad") == "before"
    assert crypto_box.open_sealed(after, "bob", keyring, b"aad") == "after"
    with pytest.raises(crypto_box.DecryptionError):                    # the current key alone cannot
        crypto_box.open_sealed(before, "bob", keyring[0], b"aad")
    retired = keys / "bob.x25519.retired"
    assert retired.exists() and oct(retired.stat().st_mode)[-3:] == "600"
    assert not list(keys.glob("*.retired.priv"))                       # never read as an agent's key
    identity.rotate_encryption_keypair("bob")                          # again: both old keys kept
    assert len(identity.encryption_private_keys("bob")) == 3


def test_a_key_kept_in_the_env_is_replaced_there(keys, tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    dotenv = tmp_path / "profile.env"
    first = X25519PrivateKey.generate().private_bytes_raw().hex()
    dotenv.write_text(f"EMPORIA_ENCRYPTION_PRIVATE_KEY={first}\n")
    monkeypatch.setenv("EMPORIA_DOTENV", str(dotenv))
    monkeypatch.setenv("EMPORIA_ENCRYPTION_PRIVATE_KEY", first)
    monkeypatch.setenv(identity.ENCRYPTION_KEY_SOURCE_VAR, "env:EMPORIA_ENCRYPTION_PRIVATE_KEY")
    sealed = _sealed_to(identity.get_encryption_public_key_hex("bob"))
    identity.rotate_encryption_keypair("bob")
    text = dotenv.read_text()
    assert first not in text.split("EMPORIA_ENCRYPTION_PRIVATE_KEY=")[1].split("\n")[0]
    assert f"{identity.RETIRED_ENC_KEYS_VAR}={first}" in text
    assert not list(keys.glob("*.retired"))                            # nothing written beside the files
    assert crypto_box.open_sealed(sealed, "bob", identity.encryption_private_keys("bob"), b"aad") == "hello"


def test_a_key_from_a_password_manager_is_replaced_there(keys, monkeypatch):
    monkeypatch.setenv(identity.ENCRYPTION_KEY_SOURCE_VAR, "cmd:op read op://v/i/k")
    with pytest.raises(ValueError, match="password manager"):
        identity.rotate_encryption_keypair("bob")


def _ed():
    k = ed25519.Ed25519PrivateKey.generate()
    return k, k.public_key().public_bytes_raw().hex()


def _bound(priv, name, at=""):
    enc = os.urandom(32).hex()
    return enc, identity.sign_payload(priv, wire.signed_encryption_key_payload(name, enc, at))


def test_a_peer_follows_a_newer_binding_and_refuses_an_older_one(keys):
    url, name = "https://enc271.example", "enc271_peer"
    me, me_hex = _ed()
    t1 = datetime.now(UTC).isoformat()
    t2 = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    enc1, sig1 = _bound(me, name, t1)
    assert identity.trust_peer_encryption_key(name, enc1, sig1, me_hex, url, at=t1) == enc1
    enc2, sig2 = _bound(me, name, t2)                                  # they replaced it
    assert identity.trust_peer_encryption_key(name, enc2, sig2, me_hex, url, at=t2) == enc2
    assert identity.load_peer_encryption_keys()[name] == enc2
    with pytest.raises(identity.PeerKeyMismatch, match="not newer"):   # a relay replays the old one
        identity.trust_peer_encryption_key(name, enc1, sig1, me_hex, url, at=t1)
    enc3, sig3 = _bound(me, name)                                      # an unordered (v1) binding
    with pytest.raises(identity.PeerKeyMismatch):
        identity.trust_peer_encryption_key(name, enc3, sig3, me_hex, url)
    rogue, _ = _ed()
    enc4, sig4 = _bound(rogue, name, (datetime.now(UTC) + timedelta(seconds=9)).isoformat())
    with pytest.raises(identity.PeerKeyMismatch, match="not signed"):  # newer, but not theirs
        identity.trust_peer_encryption_key(name, enc4, sig4, me_hex, url,
                                           at=(datetime.now(UTC) + timedelta(seconds=9)).isoformat())
    assert identity.load_peer_encryption_keys()[name] == enc2


def test_the_relay_takes_a_new_key_only_with_a_newer_binding():
    client = TestClient(app, client=("127.0.0.1", 51271))
    aid = "enc271_relay"
    _register_keyed(client, aid)
    h = {"x-emporia-agent-id": aid}

    def publish(at):
        enc = os.urandom(32).hex()
        while True:                                                   # a usable X25519 point
            try:
                from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
                X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(enc)))
                break
            except ValueError:
                enc = os.urandom(32).hex()
        sig = identity.sign(wire.signed_encryption_key_payload(aid, enc, at), aid)
        return enc, client.post(f"/agents/{aid}/encryption-key", headers=h,
                                json={"encryption_public_key_hex": enc, "signature": sig, "at": at})
    now = datetime.now(UTC)
    first, r = publish(now.isoformat())
    assert r.status_code == 200, r.text
    _, older = publish((now - timedelta(seconds=30)).isoformat())
    assert older.status_code == 409
    _, unordered = publish("")
    assert unordered.status_code == 409
    second, newer = publish((now + timedelta(seconds=1)).isoformat())
    assert newer.status_code == 200
    served = client.get(f"/agents/{aid}/encryption-key", headers=h).json()
    assert served["encryption_public_key"] == second and served["at"] == (now + timedelta(seconds=1)).isoformat()


def test_a_new_sign_in_secret_signs_everyone_out(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(identity, "_RELAY_JWT_SECRET_CACHE", None)
    token = rs._jwt_encode({"agent_id": "someone", "relay_id": rs.RELAY_ID, "iat": 0, "issued": 0,
                            "exp": int(datetime.now(UTC).timestamp()) + 600})
    assert rs._jwt_decode(token)
    identity.rotate_relay_jwt_secret()
    assert rs._jwt_decode(token) is None
    monkeypatch.setattr(identity, "_RELAY_JWT_SECRET_CACHE", None)


def test_a_second_relay_process_on_the_same_database_is_refused(tmp_path, monkeypatch):
    """P271: limits, sign-in floors and sockets live in one process's memory."""
    import subprocess
    import sys
    db = tmp_path / "relay.sqlite3"
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    monkeypatch.setattr(rs, "_PROCESS_LOCK", None)
    rs._hold_the_database()
    try:
        held = rs._PROCESS_LOCK
        rs._hold_the_database()                                         # this process again: fine
        assert rs._PROCESS_LOCK is held
        other = subprocess.run([sys.executable, "-c",
                                "import fcntl,sys\nf=open(sys.argv[1],'a')\n"
                                "try:\n    fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:\n"
                                "    print('refused')\nelse:\n    print('took it')", str(db) + ".lock"],
                               capture_output=True, text=True, timeout=30)
        assert other.stdout.strip() == "refused"
    finally:
        rs._PROCESS_LOCK.close()
        monkeypatch.setattr(rs, "_PROCESS_LOCK", None)


def test_a_relay_follows_a_remote_agents_new_encryption_key_forward_only():
    from fastapi import HTTPException

    from emporia import federation_dm as fd
    host, peer, name = "enc271-remote.example", "https://enc271-remote.example", "enc271_far"
    priv, pub = _ed()
    stable = wire.stable_id_for(pub)

    def record(at):
        enc, sig = _bound(priv, name, at)
        return enc, {"address": f"{name}@{host}", "public_key_hex": pub, "stable_id": stable, "key_log": [],
                     "encryption_public_key": enc, "encryption_key_signature": sig, "encryption_key_at": at}
    t1 = datetime.now(UTC).isoformat()
    enc1, first = record(t1)
    fd.pin_remote(peer, first)
    enc2, second = record((datetime.now(UTC) + timedelta(seconds=3)).isoformat())
    fd.pin_remote(peer, second)
    assert fd.remote(f"{name}@{host}")["encryption_public_key"] == enc2
    with pytest.raises(HTTPException) as e:
        fd.pin_remote(peer, first)                                      # the old one, replayed
    assert e.value.status_code == 409
    assert fd.remote(f"{name}@{host}")["encryption_public_key"] == enc2


def test_a_join_is_signed_by_the_joining_agent():
    """P272: a relay cannot add a player who never asked to join."""
    from conftest import header_shim_off, signed_join_body
    from tests.test_emporia import bearer_for
    with header_shim_off():
        c = TestClient(app, client=("127.0.0.1", 51272))
        _register_keyed(c, "join272_host")
        _register_keyed(c, "join272_guest")
        sid = c.post("/sessions", json={"module_type": "emporia:chess:v1", "config": {"opponent": "join272_guest"},
                                        "payment_rules": {"mode": "free"}},
                     headers=bearer_for(c, "join272_host")).json()["session_id"]
        h = bearer_for(c, "join272_guest")
        assert c.post(f"/sessions/{sid}/join", json={"agent_id": "join272_guest"}, headers=h).status_code == 401
        forged = signed_join_body(sid, "join272_host") | {"agent_id": "join272_guest"}   # another's signature
        assert c.post(f"/sessions/{sid}/join", json=forged, headers=h).status_code == 403
        other = signed_join_body("sess_elsewhere", "join272_guest")                       # another session's
        assert c.post(f"/sessions/{sid}/join", json=other, headers=h).status_code == 403
        body = signed_join_body(sid, "join272_guest")
        assert c.post(f"/sessions/{sid}/join", json=body, headers=h).status_code == 200
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT join_at, join_signature FROM participants WHERE session_id = ? AND agent_id = ?",
                           (sid, "join272_guest")).fetchone()
    assert row["join_signature"] == body["signature"] and row["join_at"] == body["at"]
