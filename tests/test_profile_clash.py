"""A profile made with `hermes profile create --clone-from` shares another's Emporia agent id (playtest 11-17):
setup and register refuse it, doctor warns. Two throwaway Hermes homes; no key file is ever opened."""
from __future__ import annotations

import argparse
import builtins
from pathlib import Path

from emporia import cli, profile_clash


def _profile(root: Path, name: str, agent_id: str, keys: str = "") -> Path:
    p = root / "profiles" / name
    (p / "home" / ".hermes" / "keys").mkdir(parents=True)
    (p / "config.yaml").write_text("{}\n")
    (p / ".env").write_text(f"EMPORIA_AGENT_ID={agent_id}\n" + (f"EMPORIA_KEYS_DIR={keys}\n" if keys else ""))
    return p


def test_a_clone_is_found_by_agent_id_and_names_the_other_profile(tmp_path):
    root = tmp_path / "hermes"
    src, clone = _profile(root, "src", "scout"), _profile(root, "clone", "scout")
    assert profile_clash.clashes(clone) == [{"profile": "src", "same": "agent id"}]
    assert profile_clash.clashes(src) == [{"profile": "clone", "same": "agent id"}]
    text = profile_clash.refusal("clone", profile_clash.clashes(clone))
    assert "src" in text and "Never clone an Emporia profile" in text and "hermes profile create" in text


def test_the_same_key_folder_is_a_clash_and_a_renamed_agent_in_its_own_folder_is_not(tmp_path):
    root = tmp_path / "hermes"
    shared = tmp_path / "shared-keys"
    shared.mkdir()
    _profile(root, "a", "one", str(shared))
    b = _profile(root, "b", "two", str(shared / ".." / "shared-keys"))        # the same folder, spelled another way
    assert profile_clash.clashes(b) == [{"profile": "a", "same": "key folder"}]
    c = _profile(root, "c", "three")
    assert profile_clash.clashes(c) == []
    # setup passes the id it is about to use
    assert profile_clash.clashes(c, "one") == [{"profile": "a", "same": "agent id"}]


def test_no_key_file_is_opened(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    src, clone = _profile(root, "src", "scout"), _profile(root, "clone", "scout")
    (src / "home" / ".hermes" / "keys" / "scout.priv").write_bytes(b"\x01" * 32)
    opened = []
    real = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "read_bytes", lambda self: opened.append(str(self)) or b"")
    assert profile_clash.clashes(clone)
    assert not [f for f in opened if f.endswith(".priv")]


def test_register_refuses_a_clone_and_changes_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "hermes"
    _profile(root, "src", "scout")
    clone = _profile(root, "clone", "scout")
    monkeypatch.setattr(cli, "_agent", lambda name: (clone, "scout", {}))
    called = []
    monkeypatch.setattr(cli, "_register_now", lambda *a, **k: called.append(1) or (True, "ok"))
    monkeypatch.setattr(cli, "health", lambda **k: True)
    assert cli.cmd_register(argparse.Namespace(name="clone")) == 1
    out = capsys.readouterr().out
    assert "shares its Emporia identity with src" in out and called == []


def test_doctor_and_setup_check_before_anything_is_written():
    src_text = Path(cli.__file__).read_text()
    assert "profile_clash.clashes(profile, agent_id)" in src_text and 'rep.warn(f"shares its' in src_text
    i = src_text.index("def _setup_agent_body")
    body = src_text[i:i + 12000]
    assert body.index("profile_clash.clashes(profile, agent_id)") < body.index('heading("Role and keys"')
