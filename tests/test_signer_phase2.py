"""Signer phase 2b and 2c (P293): what the signer opens, keys made inside it, and
its person's commands — rotation, recovery, backup — with the key never leaving it."""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from fastapi.testclient import TestClient

from emporia import crypto_box, identity, key_recovery, key_rotation, signer, signer_client, signing, wire

AGENT = "p2_agent"


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A signer that made AGENT's keys itself; the agent's side has only the markers."""
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    app = TestClient(signer.app, base_url="http://signer")
    monkeypatch.setattr(signer_client.Client, "_http", lambda self: app)
    monkeypatch.setattr(signer, "_OPENS", {})
    sent = []
    monkeypatch.setattr("emporia.signer_alerts.notify", lambda a, e, d: sent.append((a, e, d)))
    made = signer.make_agent_keys(AGENT, {"evm": "local", "svm": "local"})
    signer.save_policy(AGENT, {"EMPORIA_RELAY_URL": "https://home.example"})
    token = signer.new_agent_token(AGENT)
    keys = tmp_path / "agent-keys"
    keys.mkdir()
    (keys / f"{AGENT}.signer.pub").write_text(made["identity"] + "\n")
    (keys / f"{AGENT}.x25519.signer.pub").write_text(made["encryption"] + "\n")
    side = {"EMPORIA_SIGNER_URL": "unix:///x/signer.sock", "EMPORIA_SIGNER_TOKEN": token,
            "EMPORIA_AGENT_ID": AGENT, "EMPORIA_RELAY_URL": "https://home.example"}
    with patch.object(identity, "KEY_DIR", keys), patch.dict(os.environ, side):
        yield SimpleNamespace(made=made, app=app, keys=keys, sent=sent, token=token)


def _sealed(text, pub, label=AGENT, aad=None):
    aad = aad or crypto_box.message_aad("dm", "dm_1", "bob", "chat", "n1")
    return crypto_box.seal(text, {label: pub, "bob": x25519.X25519PrivateKey.generate().public_key()
                                  .public_bytes_raw().hex()}, aad), aad


def _open(h, envelope, label, aad):
    import base64
    return h.app.post("/v1/open", json={"envelope": envelope, "label": label, "aad": base64.b64encode(aad).decode()},
                      headers={"authorization": f"Bearer {h.token}", "x-emporia-agent": AGENT})


# ─── 2b: the signer opens only what is sealed to its agent ───────────────────

def test_the_signer_opens_what_is_sealed_to_its_agent_by_id_or_address(held):
    env, aad = _sealed("for me", held.made["encryption"])
    assert crypto_box.open_sealed(env, AGENT, identity.encryption_private_keys(AGENT), aad) == "for me"
    addr = f"{AGENT}@relay.example:8088"                                  # its name in a thread with another relay
    env2, aad2 = _sealed("across relays", held.made["encryption"], label=addr)
    assert _open(held, env2, addr, aad2).json()["plaintext"] == "across relays"


def test_the_signer_refuses_to_open_what_is_not_addressed_to_its_agent(held):
    env, aad = _sealed("for bob", held.made["encryption"])
    r = _open(held, env, "bob", aad)                                       # asking as someone else
    assert r.status_code == 403 and "opens only what is sealed to its own agent" in r.text
    assert "for bob" not in r.text
    env_bob = crypto_box.seal("only bob's", {"bob": held.made["encryption"]}, aad)   # no key wrapped for it
    r = _open(held, env_bob, AGENT, aad)
    assert r.status_code == 403 and "not addressed to" in r.text
    r = _open(held, env, f"{AGENT}x@relay", aad)
    assert r.status_code == 403
    bad = json.dumps({"v": 2, "kind": "transfer", "scope_id": "x", "sender_id": "b", "msg_type": "m",
                      "nonce": "n"}, sort_keys=True, separators=(",", ":")).encode()
    r = _open(held, env, AGENT, bad)                                        # not a DM, room or session
    assert r.status_code == 403 and "context" in r.text
    assert _open(held, env, AGENT, b"raw bytes").status_code == 403
    entries = signer.log(AGENT)
    assert [e["result"] for e in entries if e.get("op") == "open"].count("refused") == 5
    assert "for bob" not in json.dumps(entries)
    assert all(e[1] == "refused" for e in held.sent)                       # its person hears of each


def test_without_a_signer_the_agent_and_console_open_with_their_own_keys(tmp_path):
    from emporia.console import agents as console_agents
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, {"EMPORIA_SIGNER_URL": ""}):
        _, pub = identity.generate_or_load_encryption_keypair("solo")
        keys = identity.encryption_private_keys("solo")
        assert hasattr(keys[0], "exchange")                                 # a key, not an opener
        env, aad = _sealed("local", pub.hex(), label="solo")
        assert crypto_box.open_sealed(env, "solo", keys, aad) == "local"
        found = console_agents.discover([tmp_path]) if (tmp_path / "solo.priv").exists() else {}
        agent = found.get("solo") or console_agents.LocalAgent("solo", tmp_path)
        assert not agent.in_signer
        assert crypto_box.open_sealed(env, "solo", console_agents.load_encryption_keys(agent, []), aad) == "local"


async def test_the_sdk_reads_dms_through_the_signer_and_without_one(held, tmp_path):
    from emporia.agent_sdk import EmporiaAgent
    env, aad = _sealed("sdk via signer", held.made["encryption"],
                       aad=crypto_box.message_aad("dm", "dm_9", "bob", "chat", "n9"))

    def thread_of(envelope):
        return {"messages": [{"sender_id": "bob", "content": json.dumps(envelope), "msg_type": "chat",
                              "nonce": "n9"}], "my_label": None}

    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    agent = EmporiaAgent(relay_url="http://fake.invalid", agent_id=AGENT, public_key_hex=held.made["identity"])

    async def fake_request(*a, **k):
        return Resp(thread_of(env))

    async def no_key(*a, **k):
        return ""
    agent._bearer_request, agent._sender_public_key = fake_request, no_key
    out = await agent.get_dm_messages("dm_9")
    assert out["messages"][0]["content"] == "sdk via signer" and out["messages"][0]["decrypted"]
    # no signer: its own key file
    with patch.object(identity, "KEY_DIR", tmp_path / "solo-keys"), patch.dict(os.environ, {"EMPORIA_SIGNER_URL": ""}):
        _, pub = identity.generate_or_load_encryption_keypair("solo")
        env = crypto_box.seal("sdk on its own", {"solo": pub.hex()}, crypto_box.message_aad("dm", "dm_9", "bob",
                                                                                          "chat", "n9"))
        solo = EmporiaAgent(relay_url="http://fake.invalid", agent_id="solo", public_key_hex="ab" * 32)
        solo._bearer_request, solo._sender_public_key = fake_request, no_key
        assert (await solo.get_dm_messages("dm_9"))["messages"][0]["content"] == "sdk on its own"


# ─── 2c: keys made in the signer ──────────────────────────────────────────────

def test_keys_made_in_the_signer_never_touch_the_profile(held):
    assert not [f for f in held.keys.iterdir() if not f.name.endswith(".signer.pub")]
    kd = signer.agent_dir(AGENT) / "keys"
    assert {f.name for f in kd.iterdir()} >= {"identity.ed25519", "encryption.x25519"}
    p, sig = signing.signed(AGENT, "room-message", "r", "made inside", "chat", "n" * 32)
    assert identity.verify(p, sig, held.made["identity"])
    with pytest.raises(identity.IdentityInSigner):
        identity.generate_or_load_keypair(AGENT)


def test_an_encryption_binding_names_the_signers_key_and_a_new_one_is_made_there(held):
    at = "2026-11-05T00:00:00+00:00"
    with pytest.raises(signing.Refused, match="names the key it holds"):
        signing.sign(AGENT, "encryption-key", AGENT, "ab" * 32, at)        # a takeover's own key
    for i in range(3):                                                    # its key, at each registration: no limit
        signing.sign(AGENT, "encryption-key", AGENT, held.made["encryption"], f"2026-11-05T00:00:0{i}+00:00")
    old_env, aad = _sealed("before", held.made["encryption"])
    old, new = identity.rotate_encryption_keypair(AGENT)                  # made in the signer
    assert old == held.made["encryption"] != new == identity.get_encryption_public_key_hex(AGENT)
    assert not list(held.keys.glob("*.x25519.priv"))
    keys = identity.encryption_private_keys(AGENT)
    assert crypto_box.open_sealed(old_env, AGENT, keys, aad) == "before"   # the retired key, still in the signer
    new_env, aad = _sealed("after", new)
    assert crypto_box.open_sealed(new_env, AGENT, keys, aad) == "after"
    signing.sign(AGENT, "encryption-key", AGENT, new, "2026-11-05T00:01:00+00:00")   # re-binding it: not counted
    assert any(e[1] == "encryption-key" for e in held.sent)
    with pytest.raises(signing.Limited):
        identity.rotate_encryption_keypair(AGENT)                          # 1 a day


def test_a_backup_from_the_signers_side_restores_the_same_keys(held):
    with pytest.raises(ValueError, match="12 characters"):
        signer.backup(AGENT, "short")
    data, notes = signer.backup(AGENT, "correct horse battery")
    assert notes == [] and b"identity" not in data                         # sealed, not readable
    raw = (signer.agent_dir(AGENT) / "keys" / "identity.ed25519").read_bytes()
    with pytest.raises(ValueError, match="already holds"):
        signer.restore(AGENT, data, "correct horse battery")
    with pytest.raises(ValueError, match="passphrase"):
        signer.restore(AGENT, data, "wrong horse battery")
    import shutil
    shutil.rmtree(signer.agent_dir(AGENT) / "keys")
    assert set(signer.restore(AGENT, data, "correct horse battery")) == {"identity", "encryption", "evm", "svm"}
    assert (signer.agent_dir(AGENT) / "keys" / "identity.ed25519").read_bytes() == raw
    with pytest.raises(ValueError, match="not 'other'"):
        pass_ = data
        signer.restore("other", pass_, "correct horse battery")


def test_a_backend_held_key_is_named_in_the_backup_not_copied(held, monkeypatch):
    from emporia import signer_backends as sb
    from tests.test_signer_backends import FakeKms
    signer.save_backend("k", "aws-kms", {"region": "eu-west-1"}, {})
    monkeypatch.setitem(sb.OVERRIDES, "k", sb.KmsBackend("k", {}, client=FakeKms()))
    signer.set_route(AGENT, "evm", "k", sb.OVERRIDES["k"].make(AGENT, "evm"))
    data, notes = signer.backup(AGENT, "correct horse battery")
    assert notes and "evm: held by the k back end" in notes[0]


# ─── 2c: its person's commands, with the key in the signer ───────────────────

@pytest.fixture
def relay():
    from emporia.relay_server import app
    return TestClient(app, client=("127.0.0.1", 51461))


def _register(relay, name, pub):
    r = relay.post("/agents/register", json={"agent_id": name, "public_key_hex": pub})
    assert r.status_code == 200, r.text


def test_a_rotation_makes_the_new_key_in_the_signer(held, relay):
    name = "p2_rot_" + os.urandom(3).hex()
    signer.make_agent_keys(name, {"evm": "local", "svm": "local"})
    pub = signer.identity_public(name)
    (held.keys / f"{name}.signer.pub").write_text(pub + "\n")
    _register(relay, name, pub)
    out = key_rotation.rotate_identity_key(name, held.keys, relay)
    assert out["in_signer"] and out["old_public_key"] == pub != out["new_public_key"]
    assert signer.identity_public(name) == out["new_public_key"]
    assert (held.keys / f"{name}.signer.pub").read_text().strip() == out["new_public_key"]
    assert not list(held.keys.glob(f"{name}.priv*"))                      # no key file, old or new, in the profile
    served = relay.get(f"/agents/{name}", headers={"x-emporia-agent-id": name}).json()
    assert served["public_key_hex"] == out["new_public_key"]
    assert identity.key_log_windows(served["stable_id"], served["key_log"])[-1]["key"] == out["new_public_key"]
    assert list((signer.agent_dir(name) / "keys").glob("identity.ed25519.rotated-*"))   # kept, in the signer
    assert any(e.get("by") == "person" and e.get("kind") == "key-rotation" for e in signer.log(name))


def test_a_recovery_key_and_a_recovery_with_the_identity_in_the_signer(held, relay, tmp_path):
    name = "p2_rec_" + os.urandom(3).hex()
    signer.make_agent_keys(name, {"evm": "local", "svm": "local"})
    pub = signer.identity_public(name)
    (held.keys / f"{name}.signer.pub").write_text(pub + "\n")
    _register(relay, name, pub)
    out = tmp_path / "recovery.key"
    key_recovery.make_recovery_key(name, held.keys, relay, out)
    recovery = key_recovery.decode_recovery_key(out.read_text())
    time.sleep(0.01)
    done = key_recovery.recover_identity_key(name, held.keys, relay, recovery)
    assert done["in_signer"] and done["old_public_key"] == pub and done["new_public_key"] == signer.identity_public(name)
    assert (held.keys / f"{name}.signer.pub").read_text().strip() == done["new_public_key"]
    assert not list(held.keys.glob(f"{name}.priv*"))
    assert relay.get(f"/agents/{name}", headers={"x-emporia-agent-id": name}).json()["public_key_hex"] == \
        done["new_public_key"]


def test_person_kinds_are_signed_only_through_the_persons_door(held):
    with pytest.raises(signing.Refused):
        signer_client.Client.from_env().sign("key-rotation", ["sid", "a", "b", "t"], {})   # the agent's token
    with pytest.raises(signing.Refused, match="agent's own"):
        signer.person_sign(AGENT, "room-message", "r", "c", "chat", "n" * 32)
    sig = signer.person_sign(AGENT, "recovery-commitment", "sid", "c" * 64, "2026-11-05T00:00:00+00:00")
    assert identity.verify(wire.signed_recovery_commitment_payload("sid", "c" * 64, "2026-11-05T00:00:00+00:00"),
                           sig, held.made["identity"])


# ─── the commands: person-only, and what they write ──────────────────────────

def test_signer_add_make_leaves_only_public_halves_in_the_profile(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    from emporia import cli
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "maker", "--keys", "files"]) == 0
    prof = root / "maker"
    keys = prof / "home" / ".hermes" / "keys"
    for f in list(keys.glob("maker.*")) if keys.exists() else []:
        f.unlink()                                                         # a new agent: no keys yet
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    ran = []
    monkeypatch.setattr(cli, "_as_agent", lambda p, code, **e: ran.append(code) or SimpleNamespace(
        returncode=0, stdout="ok\n", stderr=""))
    assert cli.main(["signer", "add", "maker", "--url", "unix:///run/s.sock", "--make"]) == 0
    out = capsys.readouterr().out
    assert "never existed in its profile" in out and "Published" in out and "register_agent" in ran[0]
    assert sorted(f.name for f in keys.iterdir()) == ["maker.signer.pub", "maker.x25519.signer.pub"]
    assert signer.identity_public("maker") == (keys / "maker.signer.pub").read_text().strip()
    from emporia import profile_env
    env = profile_env.read(prof / ".env")
    assert env["EMPORIA_SIGNER_URL"] == "unix:///run/s.sock" and env["EMPORIA_SIGNER_TOKEN"]
    assert not any(k.endswith("_PRIVATE_KEY") for k in env)
    assert cli.main(["signer", "add", "maker", "--make"]) == 1              # never twice


def test_signer_add_make_refuses_an_agent_that_has_keys(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    from emporia import cli
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "haskeys", "--keys", "files"]) == 0
    keys = root / "haskeys" / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    identity._write_secret_atomically(keys / "haskeys.priv", ed25519.Ed25519PrivateKey.generate().private_bytes_raw())
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    assert cli.main(["signer", "add", "haskeys", "--make"]) == 1
    assert "already has key files" in capsys.readouterr().out
    assert not signer.agent_dir("haskeys").exists()


def test_signer_add_key_source_reads_from_a_vault_and_the_profile_forgets_it(profiles, tmp_path, monkeypatch,  # noqa: F811
                                                                               capsys):
    from emporia import cli, profile_env
    from emporia import signer_backends as sb
    from tests.test_signer_backends import _fake_vault
    sb.VaultBackend._HELD.clear()
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "vaulted", "--keys", "files"]) == 0
    prof = root / "vaulted"
    keys = prof / "home" / ".hermes" / "keys"
    for f in list(keys.glob("vaulted.*")) if keys.exists() else []:
        f.unlink()
    ident, enc = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    bin_ = _fake_vault(tmp_path, {"op://A/vaulted/identity": ident.private_bytes_raw(),
                                  "op://A/vaulted/encryption": enc.private_bytes_raw()})
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    cli.merge_env_file(prof / ".env", {"EMPORIA_IDENTITY_KEY_SOURCE": "cmd:op read op://A/vaulted/identity",
                                       "EMPORIA_ENCRYPTION_KEY_SOURCE": "cmd:op read op://A/vaulted/encryption",
                                       "OP_SERVICE_ACCOUNT_TOKEN": "fake-token"})   # the agent-side way, before
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_expected_public", lambda p, a, which: (
        ident if which == "identity" else enc).public_key().public_bytes_raw().hex())
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "fake-token" if "op_" in prompt else "")
    assert cli.main(["signer", "add", "vaulted", "--url", "unix:///run/s.sock",
                     "--key-source", "identity=cmd:op read op://A/vaulted/identity",
                     "--key-source", "encryption=cmd:op read op://A/vaulted/encryption"]) == 0
    out = capsys.readouterr().out
    assert "read by the signer from the password manager" in out and "removed from vaulted's profile" in out
    env = profile_env.read(prof / ".env")
    assert "EMPORIA_IDENTITY_KEY_SOURCE" not in env and "OP_SERVICE_ACCOUNT_TOKEN" not in env
    assert env["EMPORIA_SIGNER_URL"] == "unix:///run/s.sock"
    assert signer.identity_public("vaulted") == ident.public_key().public_bytes_raw().hex()
    assert (keys / "vaulted.signer.pub").exists() and not list(keys.glob("*.priv"))
    assert "fake-token" in (signer.signer_dir() / "backends" / "vault.secret").read_text()


def test_a_key_source_for_another_agent_is_refused(profiles, tmp_path, monkeypatch, capsys):  # noqa: F811
    from emporia import cli
    from emporia import signer_backends as sb
    from tests.test_signer_backends import _fake_vault
    sb.VaultBackend._HELD.clear()
    root, _ = profiles
    with patch.dict(os.environ):
        assert cli.main(["setup", "agent", "wrongkey", "--keys", "files"]) == 0
    keys = root / "wrongkey" / "home" / ".hermes" / "keys"
    for f in list(keys.glob("wrongkey.*")) if keys.exists() else []:
        f.unlink()
    bin_ = _fake_vault(tmp_path, {"i": b"\2" * 32, "e": b"\3" * 32})
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_expected_public", lambda p, a, which: "ab" * 32)
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "fake-token" if "op_" in prompt else "")
    assert cli.main(["signer", "add", "wrongkey", "--key-source", "identity=cmd:op read i",
                     "--key-source", "encryption=cmd:op read e"]) == 1
    assert "would be another agent" in capsys.readouterr().out
    assert not signer.routes("wrongkey")


def test_the_backend_command_is_a_persons_and_marks_adapters_not_tried_live(tmp_path, monkeypatch, capsys):
    from emporia import cli
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "sig"))
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.main(["signer", "backend", "k1", "add", "aws-kms", "region=us-east-1"]) == 1
    assert "only a person" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    assert cli.main(["signer", "backend", "p1", "add", "privy", "app_id=a", "app_secret=x"]) == 1
    assert "never a value on the command line" in capsys.readouterr().out
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hidden-secret" if "app_secret" in prompt else "")
    assert cli.main(["signer", "backend", "p1", "add", "privy", "app_id=a"]) == 0
    assert "not yet tested against the live service" in capsys.readouterr().out
    assert cli.main(["signer", "backend"]) == 0
    listing = capsys.readouterr().out
    assert "p1" in listing and "not yet tested against the live service" in listing and "hidden-secret" not in listing
    # MetaMask: its session token is a secret too, asked hidden or not at all (mm login as the signer's user)
    assert cli.main(["signer", "backend", "mm1", "add", "metamask", "cli_token=a:b"]) == 1
    assert "never a value on the command line" in capsys.readouterr().out
    assert cli.main(["signer", "backend", "mm1", "add", "metamask", "chain_ids=84532"]) == 0
    assert "not yet tested against the live service" in capsys.readouterr().out
    assert cli.main(["signer", "backend"]) == 0
    listing = capsys.readouterr().out
    assert "mm1" in listing and "metamask" in listing and "evm-typed-data" in listing


def test_the_key_guard_keeps_an_agent_from_the_signers_back_ends_and_backups():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "key_guard_p2", Path(__file__).parent.parent / "hermes_plugin" / "emporia" / "key_guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for cmd in ("emporia signer backend kms1 add aws-kms region=us-east-1",
                "emporia signer backend kms1 use alpha evm arn:aws:kms:x",
                "emporia signer backend",
                "emporia signer backup alpha --out /x/alpha.backup",
                "emporia signer restore alpha --from /x/alpha.backup",
                "emporia signer add alpha --make",
                "emporia signer add alpha --key-source identity=cmd:op read op://A/alpha/identity",
                "python -c 'from emporia import signer_backends'",
                "python -m emporia.signer_backends",
                # a MetaMask Agent Wallet back end: its person's to add, its mm session the signer's
                "emporia signer backend mm1 add metamask chain_ids=84532",
                "python -c 'from emporia.signer_backends import MetaMaskBackend'",
                "mm wallet sign-typed-data --chain-id 84532 --payload '{}' --wait",
                "mm wallet select 0xabc", "mm wallet policy set --policy x", "mm wallet trading-mode set beast",
                "mm transfer --to 0xabc --amount 1 --token native --chain-id 84532",
                "MM_CLI_TOKEN=a:b mm login", "cat ~/.metamask/session.json", "ls -la /home/signer/.metamask"):
        assert guard.pre_tool_call("terminal", {"command": cmd}) is not None, cmd
    assert guard.pre_tool_call("terminal", {"command": "emporia signer status"}) is None
    assert guard.pre_tool_call("terminal", {"command": "mm chains list --json"}) is None
    hidden = guard.transform_tool_result(result="export MM_CLI_TOKEN=cliTok123:refresh456")
    assert hidden is not None and "cliTok123" not in hidden
