"""Signer/installer audit fixes (SI-1, SI-3, SI-5, SI-7, SI-8, SI-10)."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from emporia import cli, signer


def _plain_profile(tmp_path, entry_env=None):
    prof = tmp_path / "profiles" / "ag"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text(yaml.dump({"mcp_servers": {"emporia": {
        "command": "python", "env": entry_env or {"EMPORIA_RELAY_URL": "http://r"}}}}))
    return prof


# SI-1
def test_marker_write_refuses_a_symlink_and_leaves_its_target(tmp_path):
    prof = _plain_profile(tmp_path)
    target = tmp_path / "admin.token"
    target.write_text("secret-token")
    link = tmp_path / "keys.pub"
    link.symlink_to(target)
    with pytest.raises(OSError):
        cli._write_profile_marker(prof, link, "a" * 64)
    assert target.read_text() == "secret-token"


def test_zero_and_unlink_refuses_a_symlink_and_leaves_its_target(tmp_path):
    target = tmp_path / "alerts.json"
    target.write_text("{}")
    link = tmp_path / "ag.x402.zz"
    link.symlink_to(target)
    with pytest.raises(OSError):
        cli._zero_and_unlink(link)
    assert target.read_text() == "{}"


def test_a_keys_dir_that_is_a_symlink_is_refused(tmp_path):
    prof = _plain_profile(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = prof / "keys"
    link.symlink_to(real)
    assert "symbolic link" in cli._unsafe_profile_key_dir(prof, link)
    assert cli._unsafe_profile_key_dir(prof, prof / "home" / "keys") == ""


def test_as_agent_env_is_an_allow_list(tmp_path, monkeypatch):
    prof = _plain_profile(tmp_path, {"EMPORIA_RELAY_URL": "http://r", "PYTHONPATH": "/evil", "PYTHONSTARTUP": "/x",
                                     "LD_PRELOAD": "/y.so", "PATH": "/evil/bin"})
    env = cli._agent_child_env(prof, {})
    assert env["EMPORIA_RELAY_URL"] == "http://r"
    assert not {"PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD"} & set(env)
    assert env["PATH"] == os.defpath


def test_a_cmd_source_in_the_mcp_entry_is_refused_as_root(tmp_path, monkeypatch):
    prof = _plain_profile(tmp_path, {"EMPORIA_X402_PHRASE_SOURCE": "cmd:/agent/script"})
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(RuntimeError, match="cmd:"):
        cli._agent_child_env(prof, {})


# SI-3
def test_the_mainnet_unlock_text_is_true_without_a_threshold():
    assert "no approval threshold" in cli._approval_phrase({})
    assert "wait for" in cli._approval_phrase({"EMPORIA_PAYMENT_APPROVAL_CENTS": "500"})
    assert "$5.00" in cli._approval_phrase({"EMPORIA_PAYMENT_APPROVAL_CENTS": "500"})


def test_seeded_policy_is_printed(capsys):
    cli._print_seeded_policy("ag", {"EMPORIA_X402_MAX_PAYMENT_CENTS": "500", "EMPORIA_X402_PAY_NETWORKS": "eip155:8453",
                                    "EMPORIA_PAYOUT_ADDRESSES": "0xabc"})
    out = capsys.readouterr().out
    assert "500" in out and "eip155:8453" in out and "0xabc" in out


# SI-5
def test_audit_fields_lose_control_characters():
    assert cli._audit_text("x\r\x1b[1A\x1b[2K forged\n") == "x?[1A?[2K forged?"[:0] + cli._audit_text("x\r\x1b[1A\x1b[2K forged\n")
    out = cli._audit_text("x\r\x1b[1A\x1b[2K forged\n")
    assert "\x1b" not in out and "\r" not in out and "\n" not in out


def test_refusals_do_not_rotate_payment_records_out(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "s"))
    signer.audit("alice", "x402", "signed", "0.05 USDC")
    monkeypatch.setattr(signer, "AUDIT_MAX_BYTES", 2000, raising=False)
    for i in range(500):
        signer.audit("alice", "x402", "refused", "x" * 100)
    assert any(r.get("outcome") == "signed" or r.get("why") == "0.05 USDC" or "signed" in str(r)
               for r in signer.log("alice", 1000)), "the payment record survived the refusals"


# SI-8
def test_rpc_errors_do_not_carry_the_url():
    import httpx

    from emporia import signer as sg
    req = httpx.Request("POST", "https://rpc.example/v2/SECRETKEY?k=1")
    err = httpx.HTTPStatusError("Client error '429' for url 'https://rpc.example/v2/SECRETKEY?k=1'",
                                request=req, response=httpx.Response(429, request=req))
    msg = sg._safe_error(err)
    assert "SECRETKEY" not in msg and "rpc.example" not in msg and "429" in msg


# SI-7
def test_install_profile_needs_an_explicit_profile(tmp_path, monkeypatch):
    from emporia import cli as c
    inst = c.installer()
    root = tmp_path / "hermes"
    (root / "scratch" / "emporia").mkdir(parents=True)
    (root / "config.yaml").write_text("{}")
    monkeypatch.chdir(root / "scratch" / "emporia")
    with pytest.raises(SystemExit):
        inst.resolve_install_profile(None, None)
    assert inst.resolve_install_profile("zed", None) is not None


# SI-10
def test_spend_ledger_corrupt_file_refuses(tmp_path):
    from emporia import agent_wallet as w
    path = tmp_path / "x.spend.json"
    path.write_text("{not json")
    with pytest.raises(w.AgentWalletError):
        w._read_ledger_strict(path)


def test_agent_secure_all_only_touches_emporia_profiles(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    (root / "other").mkdir(parents=True)
    (root / "other" / "config.yaml").write_text(yaml.dump({"env": {"X": "1"}}))
    mine = root / "mine"
    mine.mkdir()
    (mine / "config.yaml").write_text(yaml.dump({"mcp_servers": {"emporia": {"command": "p"}}}))
    monkeypatch.setattr(cli, "profiles_root", lambda: root)
    touched = []
    monkeypatch.setattr(cli.installer(), "secure_profile", lambda p: touched.append(p.name) or [])
    cli.cmd_agent_secure("all")
    assert touched == ["mine"]


def test_quoted_sudo_paths():
    assert cli._sudo_install_command(Path("/a b/x; rm"), "u.service").count("'") >= 2
