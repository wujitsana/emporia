"""scripts/secret_scan.py: the pre-push check that a push publishes no secret (11-05)."""

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("secret_scan", REPO / "scripts" / "secret_scan.py")
secret_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secret_scan)


def _repo(tmp_path, monkeypatch):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], capture_output=True, text=True, check=True).stdout
    git("init", "-q")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    monkeypatch.chdir(tmp_path)
    return git


def _commit(git, tmp_path, name, text):
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(text)
    git("add", name)
    git("commit", "-q", "-m", name)
    return git("rev-parse", "HEAD").strip()


def test_it_finds_secret_shapes_and_secret_files_but_never_prints_the_value(tmp_path, monkeypatch, capsys):
    git = _repo(tmp_path, monkeypatch)
    _commit(git, tmp_path, "ok.py", "x = 1\n")
    token = "ghp_" + "A1b2C3d4E5" * 4
    _commit(git, tmp_path, "conf.py", f"TOKEN = '{token}'\n")
    _commit(git, tmp_path, "profile/.env", "EMPORIA_AGENT_ID=a\n")
    hexkey = "ab" * 32
    _commit(git, tmp_path, "settings.txt", f"EMPORIA_IDENTITY_PRIVATE_KEY={hexkey}\n")
    kinds = {(path, kind) for _, path, kind in secret_scan.scan(["--all"])}
    assert ("conf.py", "GitHub token") in kinds
    assert ("profile/.env", "secret-like file") in kinds
    assert ("settings.txt", "hex key in a setting") in kinds
    assert not any(p == "ok.py" for p, _ in kinds)
    assert secret_scan.main(["--all"]) == 1
    out = capsys.readouterr().out
    assert token not in out and hexkey not in out


def test_fixtures_and_templates_pass(tmp_path, monkeypatch):
    git = _repo(tmp_path, monkeypatch)
    _commit(git, tmp_path, ".env.example", "EMPORIA_AGENT_ID=\n")
    _commit(git, tmp_path, "t.py", 'pem = "-----BEGIN EC PRIVATE KEY-----\\nMHcC..."\n'  # secret-scan: allow
                                   "key = 'sk_live_' + 'a' * 24\n"
                                   "real_looking = 'AKIA" + "ABCDEFGHIJKLMNOP' # secret-scan: allow\n")
    assert secret_scan.scan(["--all"]) == []


def test_the_hook_scans_only_what_the_push_adds(tmp_path, monkeypatch):
    git = _repo(tmp_path, monkeypatch)
    sha = _commit(git, tmp_path, "a.txt", "fine\n")
    assert secret_scan._pushed_revs(f"refs/heads/x {sha} refs/heads/x {'0' * 40}\n") == [sha, "--not", "--remotes"]
    assert secret_scan._pushed_revs(f"(delete) {'0' * 40} refs/heads/x abc\n") == []


def test_a_binary_or_empty_secret_file_is_found_by_its_name(tmp_path, monkeypatch):
    """Review pass (11-05): file names were read only from `+++ b/` lines, which git prints
    for text changes alone. A database, a .p12 or an empty .env shows no such line ("Binary
    files ... differ", or nothing), so the file patterns' own examples were never matched."""
    git = _repo(tmp_path, monkeypatch)
    (tmp_path / "relay.db").write_bytes(b"SQLite format 3\x00" + bytes(range(256)))
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "agent.p12").write_bytes(b"\x30\x82\x00\x00" + bytes(range(256)))
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / ".env").write_text("")
    git("add", "-A")
    git("commit", "-q", "-m", "binary and empty")
    kinds = {(path, kind) for _, path, kind in secret_scan.scan(["--all"])}
    assert {("relay.db", "secret-like file"), ("keys/agent.p12", "secret-like file"),
            ("profile/.env", "secret-like file")} <= kinds, kinds
    first = git("rev-parse", "HEAD").strip()
    git("rm", "-q", "relay.db")
    git("commit", "-q", "-m", "remove it")
    assert secret_scan.scan([f"{first}..HEAD"]) == [], "removing a secret file is not a finding"
