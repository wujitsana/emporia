"""Signer phase 2a (P293): the identity key in the signer — it signs by kind, under its own rules."""
import json
import os
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from emporia import identity, signer, signer_client, signing, wire
from tests.test_cli import profiles  # noqa: F401  (fixture)

AGENT = "p293_agent"


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A signer holding AGENT's identity key and rules; the agent's process holds only the public half."""
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw().hex()
    signer.identity_path(AGENT).parent.mkdir(parents=True)
    identity._write_secret_atomically(signer.identity_path(AGENT), priv.private_bytes_raw())
    signer.save_policy(AGENT, {"EMPORIA_RELAY_URL": "https://home.example"})
    token = signer.new_agent_token(AGENT)
    app = TestClient(signer.app, base_url="http://signer")
    monkeypatch.setattr(signer_client.Client, "_http", lambda self: app)
    keys = tmp_path / "agent-keys"
    keys.mkdir()
    (keys / f"{AGENT}.signer.pub").write_text(pub + "\n")
    sent = []
    monkeypatch.setattr("emporia.signer_alerts.notify", lambda a, e, d: sent.append((a, e, d)))
    agent_side = {"EMPORIA_SIGNER_URL": "unix:///x/signer.sock", "EMPORIA_SIGNER_TOKEN": token,
                  "EMPORIA_AGENT_ID": AGENT, "EMPORIA_RELAY_URL": "https://home.example"}
    with patch.object(identity, "KEY_DIR", keys), patch.dict(os.environ, agent_side):
        yield pub, app, sent


def test_the_agent_signs_through_the_signer_with_no_key_of_its_own(held):
    pub, _, _ = held
    args = ("room-1", "hello", "chat", "n" * 32)
    p, sig = signing.signed(AGENT, "room-message", *args)
    assert p == wire.signed_room_message_payload(*args)
    assert identity.verify(p, sig, pub)
    assert not list(identity.KEY_DIR.glob("*.priv"))                       # nothing to steal here
    assert identity.get_public_key_hex(AGENT) == identity.read_public_key_hex(AGENT) == pub
    with pytest.raises(identity.IdentityInSigner):
        identity.generate_or_load_keypair(AGENT)                         # and never a new key in its place
    nonce = "cd" * 16
    assert ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(
        bytes.fromhex(signing.sign_in(AGENT, nonce)), identity.challenge_bytes(nonce)) is None


def test_the_signers_rules_hold_whatever_the_agent_says_about_itself(held, monkeypatch):
    _, _, sent = held
    monkeypatch.setenv("EMPORIA_PAYOUT_ADDRESSES", "0x" + "ee" * 20)      # the agent widening its own list
    with pytest.raises(signing.Refused, match="the signer: not signed"):
        signing.sign(AGENT, "payout-wallet", "sid", "evm", "0x" + "ee" * 20, "2026-11-04T00:00:00+00:00")
    assert sent and sent[-1][1] == "refused"
    signer.save_policy(AGENT, {**signer.policy(AGENT)["env"], "EMPORIA_PAYOUT_ADDRESSES": "0x" + "ee" * 20})
    signing.sign(AGENT, "payout-wallet", "sid", "evm", "0x" + "ee" * 20, "2026-11-04T00:00:00+00:00")
    assert sent[-1][1] == "payout-wallet"                                  # its person hears of the change


def test_limits_are_counted_by_the_signer_and_outlive_a_restart(held):
    signer.save_policy(AGENT, {**signer.policy(AGENT)["env"], "EMPORIA_SIGN_LIMITS": "removal=2/day"})
    for i in range(2):
        signing.sign(AGENT, "removal", "post", f"p{i}", AGENT, "2026-11-04T00:00:00+00:00")
    signer._LOCK.acquire(), signer._LOCK.release()                         # a new process would read the file
    with pytest.raises(signing.Limited) as e:
        signing.sign(AGENT, "removal", "post", "p3", AGENT, "2026-11-04T00:00:00+00:00")
    assert e.value.retry_after > 80_000
    assert json.loads((signer.agent_dir(AGENT) / "sign_history.json").read_text())["removal"]


def test_a_new_home_or_backup_is_the_persons_and_a_change_is_told(held):
    _, _, sent = held
    signing.sign(AGENT, "relay-list", AGENT, "https://home.example", [], "", "2026-11-04T00:00:00+00:00", [])
    with pytest.raises(signing.Refused):
        signing.sign(AGENT, "relay-list", AGENT, "https://evil.example", [], "", "2026-11-04T00:00:01+00:00", [])
    signer.save_policy(AGENT, {**signer.policy(AGENT)["env"], "EMPORIA_BACKUP_RELAYS": "https://b.example"})
    os.environ["EMPORIA_BACKUP_RELAYS"] = "https://b.example"            # both copies, as the person's change makes
    signing.sign(AGENT, "relay-list", AGENT, "https://home.example", [], "", "2026-11-04T00:00:02+00:00",
                 ["https://b.example"])
    assert sent[-1][1] == "home-or-backups" and "https://b.example" in sent[-1][2]


def test_the_log_says_what_was_signed_and_refused_never_what_it_said(held):
    _, app, _ = held
    signing.sign(AGENT, "room-message", "room-1", "a secret plan", "chat", "n" * 32)
    with pytest.raises(signing.Refused):                                  # asked of the signer directly,
        signer_client.Client.from_env().sign("key-rotation", ["sid", "a", "b", "t"], {})   # past the agent's copy
    entries = app.get("/admin/log", params={"agent": AGENT},
                      headers={"authorization": f"Bearer {signer.admin_token()}"}).json()["entries"]
    assert [(e["kind"], e["result"]) for e in entries if e.get("op") == "sign"] == [
        ("room-message", "signed"), ("key-rotation", "refused")]
    assert "a secret plan" not in json.dumps(entries)
    assert app.get("/admin/log").status_code == 401


def test_a_wrong_token_or_unknown_kind_is_refused(held):
    _, app, _ = held
    r = app.post("/v1/sign", json={"kind": "room-message", "args": []},
                 headers={"authorization": "Bearer nope", "x-emporia-agent": AGENT})
    assert r.status_code == 401
    with pytest.raises(signing.Refused, match="no rule"):
        signing.sign(AGENT, "anything-new")


def test_a_person_moves_the_identity_key_in_and_back(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    from emporia import cli
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "ident", "--keys", "files", "--wallet"]) == 0
    prof = root / "ident"
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    keys = prof / "home" / ".hermes" / "keys"
    priv = keys / "ident.priv"
    if not priv.exists():
        identity._write_secret_atomically(priv, ed25519.Ed25519PrivateKey.generate().private_bytes_raw())
    raw = priv.read_bytes()
    assert cli.main(["signer", "add", "ident", "--url", "unix:///run/s.sock", "--identity"]) == 0
    assert not priv.exists() and signer.identity_path("ident").read_bytes() == raw
    pub = ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
    assert (keys / "ident.signer.pub").read_text().strip() == pub
    assert "held by the signer" in capsys.readouterr().out
    # its person moving its home changes the signer's copy of the rule too
    cli.merge_env_file(prof / ".env", {"EMPORIA_BACKUP_RELAYS": "https://b.example"})
    assert signer.policy("ident")["env"]["EMPORIA_BACKUP_RELAYS"] == "https://b.example"
    assert cli.main(["signer", "remove", "ident"]) == 0
    assert priv.read_bytes() == raw and not (keys / "ident.signer.pub").exists()
