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


@pytest.fixture
def log_on(monkeypatch):
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")


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


# ─── audit fixes ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["emporia-0main.sqlite3", "emporia-relay.sqlite3", "emporia-20991231T000000Z.sqlite3x"])
def test_a_database_named_like_a_backup_is_refused_as_its_own_folder(tmp_path, name):
    db = _db(tmp_path, name)
    _room(db, "r1", ["a"])
    before = db.read_bytes()
    with pytest.raises(relay_db.BackupError, match="own folder"):
        relay_db.backup(db, tmp_path, keep=1)
    assert db.exists() and db.read_bytes() == before
    with pytest.raises(relay_db.BackupError):
        relay_db.backup_if_due(db, tmp_path, 1)
    assert db.exists()


def test_rotation_never_unlinks_the_database_and_lists_only_exact_names(tmp_path):
    bk = tmp_path / "bk"
    db = _db(tmp_path)
    made = [relay_db.backup(db, bk, keep=7) for _ in range(3)]
    for stray in ("emporia-0main.sqlite3", "emporia-relay.sqlite3", "emporia-20260101T000000Z.sqlite3.partial",
                  "emporia-2026.sqlite3", "emporia-20260101T000000Z-x.sqlite3"):
        (bk / stray).write_bytes(b"x")
    assert relay_db.list_backups(bk) == made
    # a symlink to the live file under a backup-shaped name is never removed by rotation
    link = bk / "emporia-19990101T000000Z.sqlite3"
    link.symlink_to(db)
    gone = relay_db.rotate(bk, 1, db_path=db)
    assert db.exists() and db.stat().st_size > 0
    assert link not in gone
    assert all((bk / s).exists() for s in ("emporia-0main.sqlite3", "emporia-relay.sqlite3"))


def test_a_failure_after_the_copy_leaves_no_partial_and_no_manifest_temp(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    bk = tmp_path / "bk"

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(integrity, "_check_rooms", boom)       # the section raising is turned into a finding
    out = relay_db.backup(db, bk)
    assert relay_db._read_manifest(out)["integrity"]["status"] == "degraded"
    for patched in ("_sha256", "_write_private"):
        monkeypatch.setattr(relay_db, patched, boom)
        for _ in range(3):
            with pytest.raises(OSError):
                relay_db.backup(db, bk)
        monkeypatch.undo()
        monkeypatch.setattr(integrity, "_check_rooms", boom)
    assert sorted(p.name for p in bk.iterdir()) == sorted([out.name, relay_db.manifest_path(out).name])


def test_the_check_never_raises_and_does_not_use_the_system_temp_dir(tmp_path, monkeypatch):
    import tempfile
    db = _db(tmp_path)
    _room(db, "r1", ["a", "b"])
    out = relay_db.backup(db, tmp_path / "bk")                  # a rollback-journal file: walked as a copy
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-such-tmp"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "no-such-tmp"))
    report = integrity.check(out)
    assert report["status"] == "ok" and report["rooms"]["checked"] == 1
    assert [p.name for p in out.parent.iterdir() if p.name.startswith(".")] == []     # scratch removed
    import shutil

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", full)
    report = integrity.check(out)
    assert report["status"] == "degraded" and "could not be checked" in report["rooms"]["broken"][0]["detail"]
    assert [p.name for p in out.parent.iterdir() if p.name.startswith(".")] == []


def test_the_check_of_an_old_schema_log_does_not_raise(tmp_path, log_on):
    from tests.test_integrity import _log
    db = _db(tmp_path)
    _log(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE translog_states DROP COLUMN epoch")
    conn.commit()
    conn.close()
    report = integrity.check(db, relay_public_keys=["ab" * 32])
    assert report["log"]["status"] in ("ok", "broken")
    # and a section that does raise is a finding, not a crash
    with patch.object(integrity, "_check_log", side_effect=IndexError("No item with that key")):
        r = integrity.check(db)
    assert r["status"] == "degraded" and "could not be checked" in r["log"]["detail"]


def test_stale_partials_are_cleared_when_a_backup_starts(tmp_path):
    db = _db(tmp_path)
    bk = tmp_path / "bk"
    bk.mkdir()
    old, fresh = bk / "emporia-20260101T000000Z.sqlite3.partial", bk / "emporia-20260102T000000Z.sqlite3.partial"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (1, 1))
    relay_db.backup(db, bk)
    assert not old.exists() and fresh.exists()


def test_relay_keys_are_checked_and_recorded_in_the_manifest(tmp_path, log_on):
    from tests.test_integrity import _PUB, _log
    db = _db(tmp_path)
    _log(db)
    stranger = "cd" * 32
    bad = relay_db.backup(db, tmp_path / "bk", relay_public_keys=[stranger])
    m = relay_db._read_manifest(bad)
    assert m["relay_public_keys"] == [stranger] and m["integrity"]["log"] == "broken"
    good = relay_db.backup(db, tmp_path / "bk2", relay_public_keys=[_PUB])
    assert relay_db._read_manifest(good)["relay_public_keys"] == [_PUB]
    assert relay_db.verify(good)["ok"]
    # verify uses the manifest's keys: a manifest naming another key fails the same file
    mp = relay_db.manifest_path(good)
    data = json.loads(mp.read_text())
    data["relay_public_keys"] = [stranger]
    mp.write_text(json.dumps(data))
    assert not relay_db.verify(good)["ok"]


def test_the_relay_pass_hands_its_keys_to_the_backup(tmp_path, monkeypatch, log_on):
    from tests.test_integrity import _PUB, _log
    db = _db(tmp_path)
    _log(db)
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    monkeypatch.setenv("EMPORIA_DB_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setattr(rs, "_relay_keys_for_integrity", lambda: [_PUB])
    rs._db_backup_pass()
    (made,) = relay_db.list_backups(tmp_path / "bk")
    assert relay_db._read_manifest(made)["relay_public_keys"] == [_PUB]


def test_the_cli_check_reads_the_key_file_without_creating_one(tmp_path, capsys, monkeypatch, log_on):
    from emporia import identity
    from tests.test_integrity import _log
    keys = tmp_path / "keys"
    monkeypatch.setattr(identity, "KEY_DIR", keys)
    monkeypatch.setattr(identity, "_RELAY_KEYPAIR_CACHE", None)
    db = _db(tmp_path)
    _log(db)
    assert _cli("check", db_path=db) == 0
    out = capsys.readouterr().out
    assert "NO relay key was used" in out
    assert not keys.exists()                                  # nothing was created
    keys.mkdir()
    (keys / identity._RELAY_KEY_PATH_NAME).write_bytes(b"\x01" * 32)
    assert _cli("check", db_path=db) == 1                     # the log is signed by another key: now it shows
    assert "this host's relay key" in capsys.readouterr().out


def test_restore_removes_the_old_wal_before_the_swap_and_fails_loudly(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    bk = relay_db.backup(db, tmp_path / "bk")
    wal = db.with_name(db.name + "-wal")
    wal.write_bytes(b"stale wal frames")
    order = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append(("replace", wal.exists())), real_replace(a, b))[1])
    relay_db.restore(bk, db)
    assert order[-1] == ("replace", False)                    # the WAL was already gone at the swap
    # a WAL that cannot be removed stops the restore, and the database stays as it was
    wal.write_bytes(b"stale")
    before = db.read_bytes()
    real_unlink = Path.unlink

    def deny(self, *a, **k):
        if self.name.endswith("-wal"):
            raise PermissionError(13, "denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", deny)
    with pytest.raises(relay_db.BackupError, match="could not be removed"):
        relay_db.restore(bk, db)
    assert db.read_bytes() == before
    assert not list(db.parent.glob("*.restoring-*"))


def test_the_pre_restore_snapshot_has_a_manifest_and_can_be_restored(tmp_path):
    db = _db(tmp_path)
    _room(db, "r1", ["a"])
    bk = relay_db.backup(db, tmp_path / "bk")
    _room(db, "r2", ["later"])
    snap = relay_db.restore(bk, db)["snapshot"]
    assert relay_db.manifest_path(snap).exists() and relay_db.verify(snap)["ok"]
    relay_db.restore(snap, db)
    assert _rows(db, "r2") == 1


def test_an_existing_backup_folder_keeps_its_mode_and_a_new_one_is_private(tmp_path):
    db = _db(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    out = relay_db.backup(db, shared)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755 and stat.S_IMODE(out.stat().st_mode) == 0o600
    relay_db.backup(db, tmp_path / "fresh")
    assert stat.S_IMODE((tmp_path / "fresh").stat().st_mode) == 0o700


def test_the_cli_reports_an_unwritable_folder_instead_of_a_traceback(tmp_path, capsys):
    db = _db(tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert _cli("backup", dest=str(blocker / "sub"), db_path=db) == 1
    assert "relay db backup:" in capsys.readouterr().out


def test_the_degraded_report_in_the_log_names_no_private_id(tmp_path, monkeypatch, log_on):
    import logging
    db = _db(tmp_path)
    _room(db, "secret-room", ["a", "b"], public=False)
    _room(db, "pub-room", ["a", "b"])
    conn = sqlite3.connect(db)
    conn.execute("UPDATE room_messages SET content = 'x' WHERE room_id IN ('secret-room', 'pub-room') AND message_id LIKE '%-0'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    monkeypatch.setattr(rs, "_relay_keys_for_integrity", lambda: [])
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    rs.logger.addHandler(handler)
    try:
        rs.run_integrity_check()
    finally:
        rs.logger.removeHandler(handler)
    (rec,) = [r for r in records if r.getMessage() == "integrity_degraded"]
    text = json.dumps(rec.report)
    assert "secret-room" not in text and "pub-room" in text and rec.report["rooms_broken"] == 2
