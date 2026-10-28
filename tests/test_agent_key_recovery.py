"""P246: a recovery key for a lost or stolen identity key. The agent commits, with
its identity key, to the hash of a key it keeps elsewhere; if the identity key is
lost or leaks, that recovery key signs a new one, and says since when the old one
cannot be trusted. Key changes made after that time are undone, and everything
that checks a signature picks the key the agent had when it was made."""
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from emporia import identity, key_recovery, key_rotation, wire
from emporia import relay_server as rs
from emporia.identity import sign_payload, verify_agora_comment
from emporia.relay_server import app

ORIGIN = "https://kc-relay.example"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51460))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _hex(priv) -> str:
    return priv.public_key().public_bytes_raw().hex()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tick() -> None:
    time.sleep(0.01)           # log steps are strictly later than the one before


def _agent(client, tmp_path, name: str):
    priv = ed25519.Ed25519PrivateKey.generate()
    (tmp_path / f"{name}.priv").write_bytes(priv.private_bytes_raw())
    os.chmod(tmp_path / f"{name}.priv", 0o600)
    r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": _hex(priv)})
    assert r.status_code == 200, r.text
    return priv


def _profile(client, agent_id: str) -> dict:
    r = client.get(f"/agents/{agent_id}", headers=_as(agent_id))
    assert r.status_code == 200, r.text
    return r.json()


def _commit(client, agent_id: str, priv, recovery=None, at: str = ""):
    """Commit `agent_id` to a recovery key (made here unless given), signing with `priv`."""
    recovery = recovery or ed25519.Ed25519PrivateKey.generate()
    stable = _profile(client, agent_id)["stable_id"]
    commitment = wire.recovery_commitment_for(_hex(recovery))
    at = at or _now()
    r = client.post(f"/agents/{agent_id}/recovery-commitment", headers=_as(agent_id), json={
        "commitment": commitment, "at": at,
        "signature": sign_payload(priv, wire.signed_recovery_commitment_payload(stable, commitment, at))})
    return recovery, r


def _rotate(client, agent_id: str, old, at: str = ""):
    """Change the key with `old`, as whoever holds it can. Returns the new private key."""
    new = ed25519.Ed25519PrivateKey.generate()
    stable = _profile(client, agent_id)["stable_id"]
    at = at or _now()
    r = client.post(f"/agents/{agent_id}/key-rotations", headers=_as(agent_id), json={
        "new_public_key_hex": _hex(new), "at": at,
        "signature": sign_payload(old, wire.signed_key_rotation_payload(stable, _hex(old), _hex(new), at))})
    assert r.status_code == 200, r.text
    return new


def _recovery_body(stable: str, old: str, new, recovery, since: str | None, at: str = "") -> dict:
    at = at or _now()
    signed_since = since or at
    body = {"old_public_key_hex": old, "new_public_key_hex": _hex(new), "recovery_public_key_hex": _hex(recovery),
            "at": at, "signature": sign_payload(recovery, wire.signed_key_recovery_payload(
                stable, old, _hex(new), _hex(recovery), signed_since, at))}
    if since:
        body["compromised_since"] = since
    return body


def _comment(priv, created_at: str, text: str = "hello") -> dict:
    nonce = wire.new_message_nonce()
    return {"post_id": "p1", "parent_comment_id": None, "content": text, "nonce": nonce, "created_at": created_at,
            "signature": sign_payload(priv, wire.signed_agora_comment_payload("p1", None, text, nonce))}


def _checks(key, created_at: str, priv) -> bool | None:
    return verify_agora_comment(_comment(priv, created_at), key)


# --- the log, offline -------------------------------------------------------------------------

def _at(minutes: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


def _key_step(stable, old, new, at):
    return {"old": _hex(old), "new": _hex(new), "at": at, "by": "key",
            "signature": sign_payload(old, wire.signed_key_rotation_payload(stable, _hex(old), _hex(new), at))}


def _commit_step(stable, signer, recovery, at):
    commitment = wire.recovery_commitment_for(_hex(recovery))
    return {"by": "commit", "commitment": commitment, "at": at,
            "signature": sign_payload(signer, wire.signed_recovery_commitment_payload(stable, commitment, at))}


def _recovery_step(stable, old, new, recovery, since, at, signer=None):
    return {"by": "recovery", "old": _hex(old), "new": _hex(new), "recovery_public_key": _hex(recovery),
            "compromised_since": since, "at": at,
            "signature": sign_payload(signer or recovery, wire.signed_key_recovery_payload(
                stable, _hex(old), _hex(new), _hex(recovery), since, at))}


def _keys(n):
    return [ed25519.Ed25519PrivateKey.generate() for _ in range(n)]


def _windows(stable, log, key=""):
    return identity.key_log_windows(stable, log, key)


def test_a_commitment_and_a_recovery_make_a_valid_log_that_ends_the_old_keys_time_early():
    k0, k1, rk = _keys(3)
    stable = wire.stable_id_for(_hex(k0))
    log = [_commit_step(stable, k0, rk, _at(1)), _recovery_step(stable, k0, k1, rk, _at(5), _at(9))]
    windows = _windows(stable, log)
    assert [w["key"] for w in windows] == [_hex(k0), _hex(k1)]
    assert windows[0]["until"] == _at(5) and windows[1]["from"] == _at(9)
    assert identity.key_at(windows, _at(4)) == _hex(k0)
    assert identity.key_at(windows, _at(7)) == ""                   # between the two: nobody's
    assert identity.key_at(windows, _at(10)) == _hex(k1)
    assert wire.live_commitment(log) == "" and wire.live_commitment(log[:1]) == wire.recovery_commitment_for(_hex(rk))


def test_a_commitment_alone_changes_no_key_and_needs_the_key_that_signed_it():
    k0, rk = _keys(2)
    stable = wire.stable_id_for(_hex(k0))
    log = [_commit_step(stable, k0, rk, _at(1))]
    assert [w["key"] for w in _windows(stable, log, _hex(k0))] == [_hex(k0)]
    assert _windows(stable, log) == []                              # no key to say who signed it
    assert _windows(stable, [_commit_step(stable, _keys(1)[0], rk, _at(1))], _hex(k0)) == []
    # after a rotation the commitment is the new key's to make
    k1 = _keys(1)[0]
    after = [_key_step(stable, k0, k1, _at(1)), _commit_step(stable, k1, rk, _at(2))]
    assert [w["key"] for w in _windows(stable, after)] == [_hex(k0), _hex(k1)]
    assert _windows(stable, [_key_step(stable, k0, k1, _at(1)), _commit_step(stable, k0, rk, _at(2))]) == []


def test_only_one_commitment_is_live_and_a_spent_one_is_not_made_again():
    k0, k1, k2, rk, rk2 = _keys(5)
    stable = wire.stable_id_for(_hex(k0))
    one = _commit_step(stable, k0, rk, _at(1))
    assert _windows(stable, [one, _commit_step(stable, k0, rk2, _at(2))], _hex(k0)) == []     # none replaces it
    spent = [one, _recovery_step(stable, k0, k1, rk, _at(2), _at(3))]
    assert _windows(stable, [*spent, _commit_step(stable, k1, rk2, _at(4))])                   # a new one: fine
    assert _windows(stable, [*spent, _commit_step(stable, k1, rk, _at(4))]) == []              # the same one: no


def test_a_recovery_needs_the_committed_key_signing_it_and_a_commitment_from_before_the_loss():
    k0, k1, rk, other = _keys(4)
    stable = wire.stable_id_for(_hex(k0))
    commit = _commit_step(stable, k0, rk, _at(1))
    assert _windows(stable, [commit, _recovery_step(stable, k0, k1, rk, _at(5), _at(9))])
    # signed by something else
    assert _windows(stable, [commit, _recovery_step(stable, k0, k1, rk, _at(5), _at(9), signer=other)]) == []
    assert _windows(stable, [commit, _recovery_step(stable, k0, k1, rk, _at(5), _at(9), signer=k0)]) == []
    # a recovery key the commitment is not for
    assert _windows(stable, [commit, _recovery_step(stable, k0, k1, other, _at(5), _at(9))]) == []
    # no commitment at all
    assert _windows(stable, [_recovery_step(stable, k0, k1, rk, _at(5), _at(9))], _hex(k0)) == []
    # a commitment made after the time the key is said to be lost from is not the owner's
    assert _windows(stable, [commit, _recovery_step(stable, k0, k1, rk, _at(1), _at(9))]) == []
    # compromised_since after at
    assert _windows(stable, [commit, _recovery_step(stable, k0, k1, rk, _at(10), _at(9))]) == []
    # the recovery key is not the new identity key
    assert _windows(stable, [commit, _recovery_step(stable, k0, rk, rk, _at(5), _at(9))]) == []


def test_a_recovery_undoes_key_changes_after_the_time_it_names_whether_or_not_they_are_still_in_the_log():
    k0, k1, thief, k2, rk = _keys(5)
    stable = wire.stable_id_for(_hex(k0))
    commit = _commit_step(stable, k0, rk, _at(1))
    legit = _key_step(stable, k0, k1, _at(2))
    stolen = _key_step(stable, k1, thief, _at(20))
    rec = _recovery_step(stable, k1, k2, rk, _at(15), _at(30))
    rewritten = [commit, legit, rec]
    with_thief = [commit, legit, stolen, rec]
    for log in (rewritten, with_thief):
        windows = _windows(stable, log)
        assert [w["key"] for w in windows] == [_hex(k0), _hex(k1), _hex(k2)], log
        assert windows[1]["until"] == _at(15)
        assert identity.key_at(windows, _at(25)) == ""              # the thief's key was never the agent's again
    # `old` has to be the key at that time: the thief's key is not
    assert _windows(stable, [commit, legit, stolen, _recovery_step(stable, thief, k2, rk, _at(15), _at(30))]) == []
    # a key the thief used cannot come back as the agent's
    assert _windows(stable, [*with_thief, _key_step(stable, k2, thief, _at(40))]) == []
    # a second recovery cannot reach back past the first
    k3, rk2 = _keys(2)
    again = [*rewritten, _commit_step(stable, k2, rk2, _at(31))]
    assert _windows(stable, [*again, _recovery_step(stable, k2, k3, rk2, _at(29), _at(40))]) == []
    assert _windows(stable, [*again, _recovery_step(stable, k2, k3, rk2, _at(35), _at(40))])


# --- the relay ------------------------------------------------------------------------------------

def test_a_commitment_then_a_recovery_with_the_old_key_gone(client, tmp_path):
    old = _agent(client, tmp_path, "kc_end")
    stable = _profile(client, "kc_end")["stable_id"]
    out = tmp_path / "recovery.key"
    key_recovery.make_recovery_key("kc_end", tmp_path, client, out)
    recovery = key_recovery.decode_recovery_key(out.read_text())
    assert wire.live_commitment(_profile(client, "kc_end")["key_log"]) == wire.recovery_commitment_for(_hex(recovery))
    # the encryption key it had published, signed by the key about to be lost
    rs.AGENT_REGISTRY.set_encryption_key("kc_end", os.urandom(32).hex(), "signed-by-the-lost-key")
    assert client.get("/agents/kc_end/encryption-key", headers=_as("kc_end")).json()["encryption_public_key"]
    (tmp_path / "kc_end.priv").unlink()                                        # lost
    _tick()
    done = key_recovery.recover_identity_key("kc_end", tmp_path, client, recovery)
    assert done["installed"] and done["old_public_key"] == _hex(old) and done["kept_old_key_file"] is None
    new = ed25519.Ed25519PrivateKey.from_private_bytes((tmp_path / "kc_end.priv").read_bytes())
    assert _hex(new) == done["new_public_key"] != _hex(old)
    assert stat.S_IMODE((tmp_path / "kc_end.priv").stat().st_mode) == 0o600
    assert not (tmp_path / "kc_end.priv.new").exists()
    now = _profile(client, "kc_end")
    assert now["stable_id"] == stable and now["public_key_hex"] == _hex(new)       # the same agent, a new key
    assert [s["by"] for s in now["key_log"]] == ["commit", "recovery"]
    assert _windows(stable, now["key_log"])[-1]["key"] == _hex(new)
    assert wire.live_commitment(now["key_log"]) == ""                              # spent
    assert now["key_log_superseded"] == []
    # what the lost key had published is gone; the agent publishes it again with the new one
    assert client.get("/agents/kc_end/encryption-key", headers=_as("kc_end")).json()["encryption_public_key"] == ""
    # and it can sign in, and commit to a new recovery key
    key_recovery.make_recovery_key("kc_end", tmp_path, client, tmp_path / "second.key")
    assert wire.live_commitment(_profile(client, "kc_end")["key_log"])
    # the key log is readable by anyone, which is what lets an agent without its key say what it replaces
    served = client.get("/agents/kc_end/key-log").json()
    assert served["stable_id"] == stable and served["key_log"] == _profile(client, "kc_end")["key_log"]


def test_a_recovery_key_that_is_not_the_one_committed_to_is_refused(client, tmp_path):
    old = _agent(client, tmp_path, "kc_wrong")
    recovery, r = _commit(client, "kc_wrong", old)
    assert r.status_code == 200, r.text
    stable = _profile(client, "kc_wrong")["stable_id"]
    wrong, new = _keys(2)
    _tick()
    refused = client.post("/agents/kc_wrong/key-recovery", json=_recovery_body(stable, _hex(old), new, wrong, None))
    assert refused.status_code == 403
    # the old key itself does not do either
    refused = client.post("/agents/kc_wrong/key-recovery", json=_recovery_body(stable, _hex(old), new, old, None))
    assert refused.status_code == 403
    # a right key signing a step it was not asked for
    body = _recovery_body(stable, _hex(old), new, recovery, None)
    assert client.post("/agents/kc_wrong/key-recovery", json={**body, "new_public_key_hex": _hex(_keys(1)[0])}
                       ).status_code == 403
    assert _profile(client, "kc_wrong")["public_key_hex"] == _hex(old)
    with pytest.raises(key_rotation.RotationError, match="not the recovery key"):
        key_recovery.recover_identity_key("kc_wrong", tmp_path, client, wrong)
    assert not (tmp_path / "kc_wrong.priv.new").exists()
    # an agent with no commitment has nothing to recover with
    _agent(client, tmp_path, "kc_none")
    stable_none = _profile(client, "kc_none")["stable_id"]
    assert client.post("/agents/kc_none/key-recovery", json=_recovery_body(
        stable_none, _hex(old), new, recovery, None)).status_code == 409
    assert client.post("/agents/kc_nobody/key-recovery", json=_recovery_body(
        stable_none, _hex(old), new, recovery, None)).status_code == 404


def test_a_second_commitment_while_one_is_live_is_refused(client, tmp_path):
    old = _agent(client, tmp_path, "kc_twice")
    first, r = _commit(client, "kc_twice", old)
    assert r.status_code == 200, r.text
    _tick()
    second, r = _commit(client, "kc_twice", old)
    assert r.status_code == 409 and "already has a recovery commitment" in r.text
    assert wire.live_commitment(_profile(client, "kc_twice")["key_log"]) == wire.recovery_commitment_for(_hex(first))
    # not even by a thief who holds the key: it is what keeps the owner's recovery key the only one
    with pytest.raises(key_rotation.RotationError, match="already has a recovery key"):
        key_recovery.make_recovery_key("kc_twice", tmp_path, client, tmp_path / "again.key")
    assert not (tmp_path / "again.key").exists()
    # the commitment is for the agent's own key to make
    _agent(client, tmp_path, "kc_other")
    _recovery, r = _commit(client, "kc_other", old)               # signed with another agent's key
    assert r.status_code == 403
    r = client.post("/agents/kc_other/recovery-commitment", headers=_as("kc_twice"), json={
        "commitment": "ab" * 32, "at": _now(), "signature": "x"})
    assert r.status_code == 403                                    # nor for another agent to make
    assert wire.live_commitment(_profile(client, "kc_other")["key_log"]) == ""


def test_a_thiefs_rotation_after_the_loss_is_undone_and_only_later_signatures_stop_checking(client, tmp_path):
    old = _agent(client, tmp_path, "kc_thief")
    recovery, r = _commit(client, "kc_thief", old)
    assert r.status_code == 200, r.text
    stable = _profile(client, "kc_thief")["stable_id"]
    before = _now()                                    # signed by the owner, while the key was still safe
    _tick()
    since = _now()                                     # the key is stolen
    _tick()
    thief_key = _rotate(client, "kc_thief", old)       # the thief takes the identity away
    after_theft = _now()
    _tick()
    assert _profile(client, "kc_thief")["public_key_hex"] == _hex(thief_key)
    new = ed25519.Ed25519PrivateKey.generate()
    _tick()
    r = client.post("/agents/kc_thief/key-recovery", json=_recovery_body(stable, _hex(old), new, recovery, since))
    assert r.status_code == 200, r.text
    after_recovery = _now()
    body = _profile(client, "kc_thief")
    assert body["public_key_hex"] == _hex(new)
    assert [s["by"] for s in body["key_log"]] == ["commit", "recovery"]
    assert body["key_log"][-1]["compromised_since"] == since
    assert [(s["old"], s["new"]) for s in body["key_log_superseded"]] == [(_hex(old), _hex(thief_key))]
    assert r.json()["superseded"] == body["key_log_superseded"]
    windows = _windows(stable, body["key_log"])
    key = identity.SigningKey(_hex(new), windows)
    # what the old key signed before the loss still checks; after, it does not — nor does the thief's key
    assert _checks(key, before, old) is True
    assert _checks(key, after_theft, old) is False
    assert _checks(key, after_theft, thief_key) is False
    assert _checks(key, after_recovery, thief_key) is False
    assert _checks(key, after_recovery, new) is True
    # the thief's key cannot come back, and has no say now
    _tick()
    stable_now = body["stable_id"]
    back = client.post("/agents/kc_thief/key-rotations", headers=_as("kc_thief"), json={
        "new_public_key_hex": _hex(thief_key), "at": _now(), "signature": sign_payload(
            new, wire.signed_key_rotation_payload(stable_now, _hex(new), _hex(thief_key), _now()))})
    assert back.status_code in (403, 409)
    again = client.post("/agents/kc_thief/key-recovery", json=_recovery_body(stable, _hex(new), old, recovery, None))
    assert again.status_code == 409                                # the commitment is spent: it cannot be replayed


def test_a_recovery_without_a_time_ends_the_old_keys_time_now_and_a_stale_one_is_refused(client, tmp_path):
    old = _agent(client, tmp_path, "kc_stale")
    recovery, _ = _commit(client, "kc_stale", old)
    stable = _profile(client, "kc_stale")["stable_id"]
    _tick()
    new = ed25519.Ed25519PrivateKey.generate()
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    assert client.post("/agents/kc_stale/key-recovery", json=_recovery_body(
        stable, _hex(old), new, recovery, None, at=stale)).status_code == 400
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    assert client.post("/agents/kc_stale/key-recovery", json=_recovery_body(
        stable, _hex(old), new, recovery, None, at=future)).status_code == 400
    at = _now()
    assert client.post("/agents/kc_stale/key-recovery", json=_recovery_body(
        stable, _hex(old), new, recovery, _at(0), at=at)).status_code == 409      # before its own commitment
    assert client.post("/agents/kc_stale/key-recovery", json=_recovery_body(
        stable, _hex(old), new, recovery, (datetime.now(UTC) + timedelta(hours=1)).isoformat(), at=at)
    ).status_code == 400
    r = client.post("/agents/kc_stale/key-recovery", json=_recovery_body(stable, _hex(old), new, recovery, None))
    assert r.status_code == 200, r.text
    log = r.json()["key_log"]
    assert log[-1]["compromised_since"] == log[-1]["at"]


def test_the_recovery_route_has_no_credential_and_is_throttled_like_sign_in():
    class Request:
        def __init__(self, path):
            self.client = SimpleNamespace(host="203.0.113.77")
            self.url = SimpleNamespace(path=path)
            self.method = "POST"

    rs._RATE_WINDOWS.pop(("203.0.113.77", "auth"), None)
    try:
        answers = [rs._check_rate_limit(Request("/agents/someone/key-recovery")) for _ in range(11)]
        assert all(a is None for a in answers[:10]) and answers[10].status_code == 429
        assert rs._check_rate_limit(Request("/agents/someone/key-rotations")) is None      # an ordinary write
    finally:
        rs._RATE_WINDOWS.pop(("203.0.113.77", "auth"), None)
        rs._RATE_WINDOWS.pop(("203.0.113.77", "write"), None)


def test_tokens_issued_to_the_old_key_stop_working(client, tmp_path):
    old = _agent(client, tmp_path, "kc_token")
    recovery, _ = _commit(client, "kc_token", old)
    stable = _profile(client, "kc_token")["stable_id"]
    stolen = key_rotation._session(client, "kc_token", old)                        # the thief signed in
    assert client.get("/agents/kc_token", headers=stolen).status_code == 200
    _tick()
    new = ed25519.Ed25519PrivateKey.generate()
    r = client.post("/agents/kc_token/key-recovery", json=_recovery_body(stable, _hex(old), new, recovery, None))
    assert r.status_code == 200, r.text
    assert client.get("/agents/kc_token", headers=stolen).status_code in (401, 403)
    with pytest.raises(key_rotation.RotationError):
        key_rotation._session(client, "kc_token", old)                              # the old key cannot sign in
    fresh = key_rotation._session(client, "kc_token", new)                         # the new one can, at once
    assert client.get("/agents/kc_token", headers=fresh).status_code == 200
    # the cut-off is kept across a restart of the relay, and follows the agent's name
    rs._SESSION_FLOOR.clear()
    with rs.DB_LOCK, rs.get_db() as conn:
        rs._load_session_floors(conn)
    assert client.get("/agents/kc_token", headers=stolen).status_code in (401, 403)
    assert client.get("/agents/kc_token", headers=fresh).status_code == 200


# --- clients that pinned the key -------------------------------------------------------------------

def _pin(agent_id: str, body: dict) -> tuple[identity.SigningKey, list[str]]:
    changed: list[str] = []
    key = identity.pinned_signing_key(agent_id, ORIGIN, body["public_key_hex"], body["stable_id"], changed,
                                      body["key_log"])
    return key, changed


def test_a_client_pin_follows_a_recovery_even_past_the_thiefs_rotation(client, tmp_path):
    old = _agent(client, tmp_path, "kc_pin")
    recovery, _ = _commit(client, "kc_pin", old)
    _agent(client, tmp_path, "kc_pin_late")
    stable = _profile(client, "kc_pin")["stable_id"]
    key, changed = _pin("kc_pin", _profile(client, "kc_pin"))                      # pinned on the commitment
    assert key == _hex(old) and not changed
    since = _now()
    _tick()
    thief = _rotate(client, "kc_pin", old)
    _tick()
    key, changed = _pin("kc_pin", _profile(client, "kc_pin"))                      # followed the theft too, as it must
    assert key == _hex(thief) and not changed
    new = ed25519.Ed25519PrivateKey.generate()
    r = client.post("/agents/kc_pin/key-recovery", json=_recovery_body(stable, _hex(old), new, recovery, since))
    assert r.status_code == 200, r.text
    key, changed = _pin("kc_pin", _profile(client, "kc_pin"))
    assert key == _hex(new) and changed == []                                      # it followed the owner back
    assert identity.load_peer_signing_keys()[identity.signing_pin_id("kc_pin", ORIGIN)]["public_key_hex"] == _hex(new)
    assert key.windows[-1]["key"] == _hex(new)
    assert identity.signing_key_for(key, _now()) == _hex(new)


def test_a_client_that_had_not_seen_the_theft_follows_a_recovery_too(client, tmp_path):
    old = _agent(client, tmp_path, "kc_pin2")
    recovery, _ = _commit(client, "kc_pin2", old)
    stable = _profile(client, "kc_pin2")["stable_id"]
    key, changed = _pin("kc_pin2", _profile(client, "kc_pin2"))
    since = _now()
    _tick()
    _rotate(client, "kc_pin2", old)
    new = ed25519.Ed25519PrivateKey.generate()
    _tick()
    assert client.post("/agents/kc_pin2/key-recovery", json=_recovery_body(
        stable, _hex(old), new, recovery, since)).status_code == 200
    key, changed = _pin("kc_pin2", _profile(client, "kc_pin2"))
    assert key == _hex(new) and changed == []


def test_a_recovery_with_no_commitment_in_the_pinned_log_is_still_a_changed_key(client, tmp_path, monkeypatch):
    k0, k1, rk, rk2, k2 = _keys(5)
    stable = wire.stable_id_for(_hex(k0))
    # the client pinned the agent before it ever committed to a recovery key
    key, changed = _pin("kc_forged", {"public_key_hex": _hex(k0), "stable_id": stable, "key_log": []})
    assert key == _hex(k0) and not changed
    # whoever holds the key commits and recovers in one go: valid log, but nothing the client had agreed to
    forged = [_commit_step(stable, k0, rk, _at(1)), _recovery_step(stable, k0, k1, rk, _at(2), _at(3))]
    assert _windows(stable, forged)
    key, changed = _pin("kc_forged", {"public_key_hex": _hex(k1), "stable_id": stable, "key_log": forged})
    assert key == _hex(k0) and changed == ["kc_forged"]
    assert identity.load_peer_signing_keys()[identity.signing_pin_id("kc_forged", ORIGIN)]["public_key_hex"] == _hex(k0)
    # a recovery signed by a key that was never committed to is no log at all
    pinned = [_commit_step(stable, k0, rk, _at(1))]
    key, changed = _pin("kc_forged2", {"public_key_hex": _hex(k0), "stable_id": stable, "key_log": pinned})
    bogus = [*pinned, _recovery_step(stable, k0, k1, rk2, _at(2), _at(3))]
    key, changed = _pin("kc_forged2", {"public_key_hex": _hex(k1), "stable_id": stable, "key_log": bogus})
    assert key == _hex(k0) and changed == ["kc_forged2"]
    # the real one, from the same pin, is followed
    good = [*pinned, _recovery_step(stable, k0, k1, rk, _at(2), _at(3))]
    key, changed = _pin("kc_forged2", {"public_key_hex": _hex(k1), "stable_id": stable, "key_log": good})
    assert key == _hex(k1) and changed == []


def test_a_log_that_drops_steps_without_a_recovery_is_still_a_changed_key(client):
    k0, k1, k2, rk = _keys(4)
    stable = wire.stable_id_for(_hex(k0))
    log = [_key_step(stable, k0, k1, _at(1)), _key_step(stable, k1, k2, _at(2))]
    key, changed = _pin("kc_rollback", {"public_key_hex": _hex(k2), "stable_id": stable, "key_log": log})
    assert key == _hex(k2) and not changed
    key, changed = _pin("kc_rollback", {"public_key_hex": _hex(k1), "stable_id": stable, "key_log": log[:1]})
    assert key == _hex(k2) and changed == ["kc_rollback"]
    # a recovery cannot be used to rewrite steps from before the time it names
    commit = _commit_step(stable, k0, rk, _at(0))
    k3 = _keys(1)[0]
    pinned_log = [commit, *[_key_step(stable, k0, k1, _at(1)), _key_step(stable, k1, k2, _at(2))]]
    _pin("kc_rollback2", {"public_key_hex": _hex(k2), "stable_id": stable, "key_log": pinned_log})
    rewritten = [commit, _recovery_step(stable, k2, k3, rk, _at(5), _at(6))]
    # the log is valid on its own only if `old` is the key at that time; here the steps it drops are before it
    assert _windows(stable, rewritten) == []
    key, changed = _pin("kc_rollback2", {"public_key_hex": _hex(k3), "stable_id": stable, "key_log": rewritten})
    assert key == _hex(k2) and changed == ["kc_rollback2"]


# --- the person's commands -------------------------------------------------------------------------

class _Installer:
    def harvest_env(self, profile):
        return {}

    def print_restart_instructions(self, profiles):
        print("restart it")


def _cli(monkeypatch, tmp_path, name: str, ran: list):
    from emporia import cli
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path, name, {"EMPORIA_AGENT_ID": name}))
    monkeypatch.setattr(cli, "_key_file", lambda profile, agent_id, which: tmp_path / f"{name}.priv")
    monkeypatch.setattr(cli, "installer", lambda: _Installer())
    monkeypatch.setattr(cli, "relay_url", lambda: "http://testserver")
    monkeypatch.setattr(cli, "this_profile", lambda: tmp_path)
    monkeypatch.setattr(cli, "_as_agent", lambda profile, code, **env: ran.append((code, env)) or SimpleNamespace(
        returncode=0, stdout="ok\n", stderr=""))
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    return cli


def test_the_commands_never_print_a_key_and_write_the_recovery_key_0600(client, tmp_path, monkeypatch, capsys):
    old = _agent(client, tmp_path, "kc_cli")
    ran: list = []
    cli = _cli(monkeypatch, tmp_path, "kc_cli", ran)
    out = tmp_path / "paper.txt"
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.cmd_agent_recovery_key("kc_cli", str(out), http=client) == 1        # an agent's terminal is not a person's
    assert not out.exists()
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    assert cli.cmd_agent_recovery_key("kc_cli", "", http=client) == 1              # it has to be told where
    assert cli.cmd_agent_recovery_key("kc_cli", str(out), http=client) == 0
    text = capsys.readouterr().out
    line = out.read_text().strip()
    assert line.startswith(key_recovery.RECOVERY_KEY_PREFIX) and len(out.read_text().splitlines()) == 1
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    secret = line[len(key_recovery.RECOVERY_KEY_PREFIX):]
    assert secret not in text and line not in text
    assert "paper" in text and "password manager" in text and "delete the file" in text
    assert cli.cmd_agent_recovery_key("kc_cli", str(out), http=client) == 1        # not over a file that is there
    assert out.read_text().strip() == line
    assert cli.cmd_agent_recovery_key("kc_cli", str(tmp_path / "again.txt"), http=client) == 1   # one live at a time
    assert not (tmp_path / "again.txt").exists()
    capsys.readouterr()

    # the key is lost; the person recovers from the file
    (tmp_path / "kc_cli.priv").unlink()
    _tick()
    assert cli.cmd_agent_recover("kc_cli", key_file=str(out), yes=True, http=client) == 0
    text = capsys.readouterr().out
    new_file = tmp_path / "kc_cli.priv"
    new_secret = new_file.read_bytes()
    assert secret not in text and line not in text and new_secret.hex() not in text
    assert stat.S_IMODE(new_file.stat().st_mode) == 0o600
    assert "make a new one" in text.lower() or "Make a new one" in text
    assert "signed again" in text and "restart it" in text
    assert len(ran) == 1 and "register_agent" in ran[0][0]
    assert _hex(ed25519.Ed25519PrivateKey.from_private_bytes(new_secret)) == _profile(client, "kc_cli")["public_key_hex"] != _hex(old)
    # the spent key does it no more
    (tmp_path / "kc_cli.priv.new").unlink(missing_ok=True)
    assert cli.cmd_agent_recover("kc_cli", key_file=str(out), yes=True, http=client) == 1
    assert "nothing to recover" in capsys.readouterr().out


def test_the_old_key_left_after_a_recovery_is_said_to_be_compromised_evidence(client, tmp_path, monkeypatch, capsys):
    _agent(client, tmp_path, "kc_stolen")
    cli = _cli(monkeypatch, tmp_path, "kc_stolen", [])
    assert cli.cmd_agent_recovery_key("kc_stolen", str(tmp_path / "r.txt"), http=client) == 0
    capsys.readouterr()
    _tick()
    assert cli.cmd_agent_recover("kc_stolen", key_file=str(tmp_path / "r.txt"), yes=True, http=client) == 0
    text = capsys.readouterr().out
    kept = [f for f in tmp_path.iterdir() if ".recovered-" in f.name]
    assert len(kept) == 1 and kept[0].name in text
    assert "compromised" in text and "evidence" in text and "delete the file" in text
    assert "not compromised" not in text


def test_a_recovery_key_can_be_read_from_a_password_manager_command_and_a_bad_one_is_said_so(
        client, tmp_path, monkeypatch, capsys):
    _agent(client, tmp_path, "kc_src")
    cli = _cli(monkeypatch, tmp_path, "kc_src", [])
    out = tmp_path / "pm.txt"
    assert cli.cmd_agent_recovery_key("kc_src", str(out), http=client) == 0
    line = out.read_text().strip()
    capsys.readouterr()
    assert cli.cmd_agent_recover("kc_src", key_source="cmd:cat /nonexistent/nothing", yes=True, http=client) == 1
    assert "could not read the recovery key" in capsys.readouterr().out
    out.write_text("emporia-recovery-v1:not-a-key\n")
    assert cli.cmd_agent_recover("kc_src", key_file=str(out), yes=True, http=client) == 1
    assert "damaged" in capsys.readouterr().out
    out.write_text("a password\n")
    assert cli.cmd_agent_recover("kc_src", key_file=str(out), yes=True, http=client) == 1
    assert "not a recovery key" in capsys.readouterr().out
    out.write_text(line + "\n")
    assert cli.cmd_agent_recover("kc_src", key_source=f"cmd:cat {out}", yes=True, http=client) == 0


def test_a_key_kept_elsewhere_is_left_alone_by_a_recovery_and_the_new_key_goes_to_a_file(client, tmp_path):
    old = _agent(client, tmp_path, "kc_linked")
    (tmp_path / "kc_linked.priv").unlink()
    env = {"KC_LINKED_KEY": old.private_bytes_raw().hex()}
    key_recovery.make_recovery_key("kc_linked", tmp_path, client, tmp_path / "r.key", source="env:KC_LINKED_KEY",
                                   source_env=env)
    recovery = key_recovery.decode_recovery_key((tmp_path / "r.key").read_text())
    _tick()
    done = key_recovery.recover_identity_key("kc_linked", tmp_path, client, recovery, source="env:KC_LINKED_KEY")
    assert done["installed"] is False and not (tmp_path / "kc_linked.priv").exists()
    assert stat.S_IMODE(done["new_key_file"].stat().st_mode) == 0o600
    assert _profile(client, "kc_linked")["public_key_hex"] == done["new_public_key"]


def test_a_run_that_stopped_after_the_relay_took_the_recovery_finishes_with_the_key_on_disk(client, tmp_path):
    _agent(client, tmp_path, "kc_resume")
    key_recovery.make_recovery_key("kc_resume", tmp_path, client, tmp_path / "r.key")
    recovery = key_recovery.decode_recovery_key((tmp_path / "r.key").read_text())
    (tmp_path / "kc_resume.priv").unlink()

    class DropsTheAnswer:
        def __init__(self, inner):
            self.inner = inner

        def get(self, *a, **k):
            return self.inner.get(*a, **k)

        def post(self, url, **k):
            r = self.inner.post(url, **k)
            if "key-recovery" in url:
                raise ConnectionError("connection reset")
            return r

    _tick()
    with pytest.raises(ConnectionError):
        key_recovery.recover_identity_key("kc_resume", tmp_path, DropsTheAnswer(client), recovery)
    assert (tmp_path / "kc_resume.priv.new").exists()                              # kept before the relay was asked
    out = key_recovery.recover_identity_key("kc_resume", tmp_path, client, recovery)
    assert out["installed"]
    assert _hex(ed25519.Ed25519PrivateKey.from_private_bytes((tmp_path / "kc_resume.priv").read_bytes())) == \
        _profile(client, "kc_resume")["public_key_hex"] == out["new_public_key"]


def test_a_refused_recovery_leaves_no_new_key_behind(client, tmp_path):
    old = _agent(client, tmp_path, "kc_refused")
    key_recovery.make_recovery_key("kc_refused", tmp_path, client, tmp_path / "r.key")
    recovery = key_recovery.decode_recovery_key((tmp_path / "r.key").read_text())
    other = _agent(client, tmp_path, "kc_refused_other")

    class Refuses:
        def __init__(self, inner):
            self.inner = inner

        def get(self, *a, **k):
            return self.inner.get(*a, **k)

        def post(self, url, **k):
            if "key-recovery" in url:
                k["json"] = {**k["json"], "new_public_key_hex": _hex(other)}
            return self.inner.post(url, **k)

    _tick()
    with pytest.raises(key_rotation.RotationError):
        key_recovery.recover_identity_key("kc_refused", tmp_path, Refuses(client), recovery)
    assert (tmp_path / "kc_refused.priv").read_bytes() == old.private_bytes_raw()
    assert not (tmp_path / "kc_refused.priv.new").exists()
    with pytest.raises(key_rotation.RotationError, match="future"):
        key_recovery.recover_identity_key("kc_refused", tmp_path, client, recovery,
                                          compromised_since=(datetime.now(UTC) + timedelta(days=1)).isoformat())


def test_the_recovery_key_line_round_trips_and_is_one_printable_line():
    priv = ed25519.Ed25519PrivateKey.generate()
    line = key_recovery.encode_recovery_key(priv)
    assert line.isprintable() and "\n" not in line and line.startswith("emporia-recovery-v1:")
    assert key_recovery.decode_recovery_key(f"  {line}\n").private_bytes_raw() == priv.private_bytes_raw()
    with pytest.raises(key_rotation.RotationError):
        key_recovery.decode_recovery_key(line[:-8])


def test_a_renamed_agent_keeps_the_cut_off_for_tokens_of_its_lost_key(client, tmp_path):
    old = _agent(client, tmp_path, "kc_rename")
    recovery, _ = _commit(client, "kc_rename", old)
    stable = _profile(client, "kc_rename")["stable_id"]
    _tick()
    new = ed25519.Ed25519PrivateKey.generate()
    assert client.post("/agents/kc_rename/key-recovery", json=_recovery_body(
        stable, _hex(old), new, recovery, None)).status_code == 200
    floor = rs._SESSION_FLOOR["kc_rename"]
    rs.AGENT_REGISTRY.rename("kc_rename", "kc_renamed")
    assert "kc_rename" not in rs._SESSION_FLOOR and rs._SESSION_FLOOR["kc_renamed"] == floor
