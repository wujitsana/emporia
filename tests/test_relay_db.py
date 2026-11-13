"""Local database backup, rotation and the tested restore (relay_db.py). Corrupting a scratch file with SQL or
bytes is test setup; the code under test never deletes or repairs rows."""
import asyncio
import fcntl
import json
import os
import sqlite3
import stat
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from emporia import integrity, relay_db
from emporia import relay_server as rs
from tests.test_integrity import _db, _room


def _rows(path: Path, room: str = "r1") -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM room_messages WHERE room_id = ?", (room,)).fetchone()[0]
    finally:
        conn.close()


def test_backup_of_a_database_being_written_passes_the_check(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a", "b", "c"])
    writer = sqlite3.connect(db)                      # a second connection with an open write transaction
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE rooms SET name = 'renamed' WHERE room_id = 'r1'")
    out = relay_db.backup(db, tmp_path / "bk")
    writer.rollback()
    writer.close()
    assert out.name.startswith("emporia-") and out.suffix == ".sqlite3"
    m = json.loads(relay_db.manifest_path(out).read_text())
    assert m["integrity"]["status"] == "ok" and m["size"] == out.stat().st_size
    assert m["relay_version"] and "sha256" in m and "size" in m["log"]
    assert integrity.check(out)["status"] == "ok"
    assert _rows(out) == 3
    assert not list((tmp_path / "bk").glob("*.partial"))
    assert sqlite3.connect(out).execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_modes_are_private(tmp_path):
    db = _db(tmp_path)
    out = relay_db.backup(db, tmp_path / "bk")
    assert stat.S_IMODE((tmp_path / "bk").stat().st_mode) == 0o700
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(relay_db.manifest_path(out).stat().st_mode) == 0o600


def test_rotation_keeps_the_newest_and_never_touches_the_database(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    before = db.read_bytes()
    made = []
    for _ in range(5):
        made.append(relay_db.backup(db, tmp_path / "bk", keep=3))
    left = relay_db.list_backups(tmp_path / "bk")
    assert left == made[-3:]
    assert sorted(p.name for p in (tmp_path / "bk").glob("*.json")) == [relay_db.manifest_path(p).name for p in left]
    assert _rows(db) == 1 and db.exists()
    assert before[:16] == db.read_bytes()[:16]


def test_rotation_never_removes_the_last_good_backup(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a", "b"])
    good = relay_db.backup(db, tmp_path / "bk", keep=2)
    conn = sqlite3.connect(db)                        # test setup: break the chain
    conn.execute("UPDATE room_messages SET content = 'x' WHERE message_id = 'r1-0'")
    conn.commit()
    conn.close()
    bad = [relay_db.backup(db, tmp_path / "bk", keep=2) for _ in range(3)]
    left = relay_db.list_backups(tmp_path / "bk")
    assert good in left and bad[-1] in left
    assert json.loads(relay_db.manifest_path(bad[-1]).read_text())["integrity"]["status"] == "degraded"


def test_restore_round_trip(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a", "b"])
    bk = relay_db.backup(db, tmp_path / "bk")
    _room(db, "r2", ["later"])
    assert _rows(db, "r2") == 1
    done = relay_db.restore(bk, db)
    assert _rows(db, "r1") == 2 and _rows(db, "r2") == 0
    assert done["report"]["status"] == "ok"
    snap = done["snapshot"]
    assert snap.parent == bk.parent and snap.name.startswith("pre-restore-")
    assert _rows(snap, "r2") == 1                     # what was there is kept
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600
    assert not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists()
    assert relay_db.list_backups(bk.parent) == [bk]   # the snapshot is not a rotated backup


def test_restore_refuses_while_a_relay_holds_the_database(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    bk = relay_db.backup(db, tmp_path / "bk")
    _room(db, "r2", ["later"])
    held = open(db.with_name(db.name + ".lock"), "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert relay_db.running(db)
        with pytest.raises(relay_db.BackupError, match="relay is running"):
            relay_db.restore(bk, db)
    finally:
        held.close()
    assert _rows(db, "r2") == 1 and not list((tmp_path / "bk").glob("pre-restore-*"))
    assert not relay_db.running(db)


def test_a_corrupted_backup_is_refused(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    bk = relay_db.backup(db, tmp_path / "bk")
    _room(db, "r2", ["later"])
    data = bytearray(bk.read_bytes())
    data[len(data) // 2] ^= 0xFF
    os.chmod(bk, 0o600)
    bk.write_bytes(bytes(data))
    assert not relay_db.verify(bk)["ok"]
    with pytest.raises(relay_db.BackupError, match="sha256|size"):
        relay_db.restore(bk, db)
    assert _rows(db, "r2") == 1 and not list((tmp_path / "bk").glob("pre-restore-*"))


def test_a_backup_without_its_manifest_is_refused(tmp_path):
    db = _db(tmp_path)
    bk = relay_db.backup(db, tmp_path / "bk")
    relay_db.manifest_path(bk).unlink()
    with pytest.raises(relay_db.BackupError, match="manifest"):
        relay_db.restore(bk, db)


def test_a_backup_of_a_damaged_chain_is_refused(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a", "b"])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE room_messages SET content = 'x' WHERE message_id = 'r1-0'")
    conn.commit()
    conn.close()
    bk = relay_db.backup(db, tmp_path / "bk")
    with pytest.raises(relay_db.BackupError, match="integrity"):
        relay_db.restore(bk, tmp_path / "other.sqlite3")


def test_backup_if_due_is_daily_and_opt_in(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.delenv("EMPORIA_DB_BACKUP_DIR", raising=False)
    assert relay_db.backup_if_due(db) is None
    monkeypatch.setenv("EMPORIA_DB_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("EMPORIA_DB_BACKUP_KEEP", "2")
    first = relay_db.backup_if_due(db)
    assert first is not None and relay_db.backup_if_due(db) is None
    old = first.stat().st_mtime - 2 * 86400
    os.utime(first, (old, old))
    assert relay_db.backup_if_due(db) is not None


def test_the_relay_loop_backs_up_and_survives_a_failure(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("EMPORIA_DB_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    rs._db_backup_pass()
    assert len(relay_db.list_backups(tmp_path / "bk")) == 1
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        raise asyncio.CancelledError

    async def run():
        with patch.object(rs, "_db_backup_pass", boom), patch.object(rs, "_DB_BACKUP_CHECK_SECONDS", 0):
            await rs._db_backup_loop(first_delay=0)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert len(calls) == 2


def test_no_backup_task_without_the_setting(monkeypatch):
    monkeypatch.delenv("EMPORIA_DB_BACKUP_DIR", raising=False)
    assert relay_db.backup_dir() is None
    monkeypatch.setenv("EMPORIA_DB_BACKUP_DIR", "  ")
    assert relay_db.backup_dir() is None


# ─── the command ─────────────────────────────────────────────────────────────

def _cli(sub, files=(), **kw):
    return relay_db.cli(sub, list(files), **kw)


def test_cli_backup_list_check(tmp_path, capsys):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    assert _cli("backup", dest=str(tmp_path / "bk"), db_path=db) == 0
    assert _cli("list", dest=str(tmp_path / "bk"), db_path=db) == 0
    out = capsys.readouterr().out
    assert "backup:" in out and "integrity ok" in out
    assert _cli("check", db_path=db) == 0
    assert "room chains: 1 checked, 0 broken" in capsys.readouterr().out


def test_cli_check_names_private_ids(tmp_path, capsys):
    db = _db(tmp_path)
    _room(db, "secret-room", ["a", "b"], public=False)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE room_messages SET content = 'x' WHERE message_id = 'secret-room-0'")
    conn.commit()
    conn.close()
    assert _cli("check", db_path=db) == 1
    out = capsys.readouterr().out
    assert "secret-room (private)" in out


def test_cli_without_a_folder_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("EMPORIA_DB_BACKUP_DIR", raising=False)
    assert _cli("backup", db_path=_db(tmp_path)) == 1
    assert "EMPORIA_DB_BACKUP_DIR" in capsys.readouterr().out


def test_cli_restore_warns_asks_and_needs_a_person(tmp_path, capsys):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    bk = relay_db.backup(db, tmp_path / "bk")
    _room(db, "r2", ["later"])
    assert _cli("restore", [bk.name], dest=str(bk.parent), db_path=db, person=False, yes=True) == 1
    assert "only a person" in capsys.readouterr().out and _rows(db, "r2") == 1
    assert _cli("restore", [bk.name], dest=str(bk.parent), db_path=db, person=True, ask=lambda q, d: False) == 1
    out = capsys.readouterr().out
    assert "rolls the database back" in out and "Not restored" in out and _rows(db, "r2") == 1
    assert _cli("restore", [bk.name], dest=str(bk.parent), db_path=db, person=True, ask=lambda q, d: True) == 0
    out = capsys.readouterr().out
    assert "pre-restore" in out and _rows(db, "r2") == 0 and _rows(db, "r1") == 1


def test_cli_restore_refuses_while_running(tmp_path, capsys):
    db = _db(tmp_path)
    bk = relay_db.backup(db, tmp_path / "bk")
    held = open(db.with_name(db.name + ".lock"), "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert _cli("restore", [str(bk)], db_path=db, person=True, yes=True) == 1
    finally:
        held.close()
    assert "a relay is running" in capsys.readouterr().out


def test_the_command_is_wired(tmp_path, monkeypatch, capsys):
    from emporia import cli
    db = _db(tmp_path)
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    args = Namespace(action="db", url="check", names=[], dir="", yes=False)
    assert cli.cmd_relay(args) == 0
    assert "status: ok" in capsys.readouterr().out
