"""A profile made with `hermes profile create --clone-from` shares another's Emporia agent id and key (playtest
11-17): setup and register refuse it, doctor warns. Two throwaway Hermes homes; only public halves are compared."""
from __future__ import annotations

import argparse
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import cli, profile_clash


def _profile(root: Path, name: str, agent_id: str, key: bytes | None) -> Path:
    p = root / "profiles" / name
    keys = p / "home" / ".hermes" / "keys"
    keys.mkdir(parents=True)
    (p / "config.yaml").write_text("{}\n")
    (p / ".env").write_text(f"EMPORIA_AGENT_ID={agent_id}\n")
    if key is not None:
        (keys / f"{agent_id}.priv").write_bytes(key)
    return p


def _two(tmp_path, *, same_id=True, same_key=True):
    root = tmp_path / "hermes"
    key_a = ed25519.Ed25519PrivateKey.generate().private_bytes_raw()
    key_b = key_a if same_key else ed25519.Ed25519PrivateKey.generate().private_bytes_raw()
    src = _profile(root, "src", "scout", key_a)
    clone = _profile(root, "clone", "scout" if same_id else "scout2", key_b)
    return root, src, clone


def test_a_clone_is_found_by_agent_id_and_by_key_and_names_the_other_profile(tmp_path):
    _, src, clone = _two(tmp_path)
    assert profile_clash.clashes(clone) == [{"profile": "src", "same": "agent id and identity key"}]
    assert profile_clash.clashes(src) == [{"profile": "clone", "same": "agent id and identity key"}]
    text = profile_clash.refusal("clone", profile_clash.clashes(clone))
    assert "src" in text and "Never clone an Emporia profile" in text and "hermes profile create" in text


def test_only_the_key_or_only_the_id_is_found_and_unrelated_profiles_are_not(tmp_path):
    _, _, clone = _two(tmp_path / "a", same_id=False)
    [c] = profile_clash.clashes(clone)
    assert c["same"] == "identity key" and "rotate-key" in profile_clash.refusal("clone", [c])
    _, _, clone2 = _two(tmp_path / "b", same_key=False)
    assert [c["same"] for c in profile_clash.clashes(clone2)] == ["agent id"]
    _, _, clean = _two(tmp_path / "c", same_id=False, same_key=False)
    assert profile_clash.clashes(clean) == []
    # setup passes the id it is about to use
    assert profile_clash.clashes(clean, "scout")[0]["same"] == "agent id"


def test_no_private_key_material_is_ever_in_what_is_returned(tmp_path):
    _, src, clone = _two(tmp_path)
    priv = (src / "home" / ".hermes" / "keys" / "scout.priv").read_bytes().hex()
    shown = repr(profile_clash.clashes(clone)) + profile_clash.refusal("clone", profile_clash.clashes(clone))
    assert priv not in shown and profile_clash.public_key_of(clone, "scout") not in shown


def test_register_refuses_a_clone_and_changes_nothing(tmp_path, monkeypatch, capsys):
    _, _, clone = _two(tmp_path)
    monkeypatch.setattr(cli, "_agent", lambda name: (clone, "scout", {}))
    called = []
    monkeypatch.setattr(cli, "_register_now", lambda *a, **k: called.append(1) or (True, "ok"))
    monkeypatch.setattr(cli, "health", lambda **k: True)
    assert cli.cmd_register(argparse.Namespace(name="clone")) == 1
    out = capsys.readouterr().out
    assert "shares its Emporia identity with src" in out and called == []


def test_doctor_warns_and_names_the_other_profile(tmp_path):
    _, _, clone = _two(tmp_path)
    src_text = (Path(cli.__file__)).read_text()
    assert "profile_clash.clashes(profile, agent_id)" in src_text and "rep.warn(f\"shares its" in src_text


def test_setup_refuses_before_it_writes_anything(tmp_path):
    src_text = (Path(cli.__file__)).read_text()
    i = src_text.index("def _setup_agent_body")
    body = src_text[i:i + 12000]
    assert body.index("profile_clash.clashes(profile, agent_id)") < body.index("heading(\"Role and keys\"")
