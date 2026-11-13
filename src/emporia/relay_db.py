"""A local backup of the relay's database, a rotation of them, and a restore that is never silent.

`emporia relay db check | backup | list | restore FILE`. Backups are opt-in: the relay takes one a day only
when EMPORIA_DB_BACKUP_DIR is set (EMPORIA_DB_BACKUP_KEEP of them are kept, default 7). Off-host and encrypted
copies are not built here (docs/RUNBOOK.md, "Backups and integrity checks", says how they would be).

A backup is a consistent copy made with SQLite's online backup, written in the rollback-journal mode so the
file never changes after it is made, with a manifest beside it (size, sha256, relay version, the log's size and
root, and the integrity check's summary of the copy). Rotation removes only the oldest backup files beyond the
keep count, and never the newest one that passed its check. Nothing here writes to the live database.

A restore puts an older state back: it rolls the transparency log and every room and DM back to the backup's
moment. It refuses while a relay holds the database, refuses a backup that does not match its manifest or fails
its check, snapshots the current file first (`pre-restore-<stamp>.sqlite3`, next to the backup) and runs the
check on the result.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emporia import __version__, integrity

PREFIX = "emporia-"
SUFFIX = ".sqlite3"
DEFAULT_KEEP = 7
DAY_SECONDS = 24 * 3600
#: How long the integrity check of one fresh backup may take.
CHECK_BUDGET_SECONDS = 600.0

WARNING = (
    "Restoring an older copy rolls the database back to the moment of that backup. It undoes the transparency\n"
    "log's entries and every room message, DM, post and sign-up written since; peers and agents that pinned a\n"
    "later log head, or hold later room or DM chain heads, will show \"chain unverified\" against it, and signed\n"
    "break records that would explain it do not exist yet. The current file is copied aside first."
)


class BackupError(RuntimeError):
    """Something a person should read: the backup or restore did not go ahead."""


# ─── settings ────────────────────────────────────────────────────────────────

def backup_dir() -> Path | None:
    """EMPORIA_DB_BACKUP_DIR, or None when automatic backups are off."""
    raw = os.getenv("EMPORIA_DB_BACKUP_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def backup_keep() -> int:
    try:
        return max(1, int(os.getenv("EMPORIA_DB_BACKUP_KEEP", str(DEFAULT_KEEP))))
    except ValueError:
        return DEFAULT_KEEP


# ─── files ───────────────────────────────────────────────────────────────────

def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_path(backup: Path) -> Path:
    return backup.with_suffix(".json")


def _read_manifest(backup: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(manifest_path(backup).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _log_head(conn: sqlite3.Connection) -> dict[str, Any]:
    """The log's size and the latest signed root, from a database file (empty when it has none)."""
    try:
        size = conn.execute("SELECT COUNT(*) FROM translog_leaves").fetchone()[0]
        row = conn.execute("SELECT size, root FROM translog_states ORDER BY size DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return {"size": 0, "root": None, "signed_size": None}
    return {"size": size, "root": row[1] if row else None, "signed_size": row[0] if row else None}


# ─── backup ──────────────────────────────────────────────────────────────────

def list_backups(dest_dir: Path) -> list[Path]:
    """Backup files in `dest_dir`, oldest first (the stamp in the name sorts by time)."""
    try:
        return sorted((p for p in dest_dir.iterdir()
                       if p.is_file() and p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)),
                      key=lambda p: p.name.removesuffix(SUFFIX))
    except OSError:
        return []


def _new_name(dest_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")    # fixed width: names sort by time
    path = dest_dir / f"{PREFIX}{stamp}{SUFFIX}"
    n = 1
    while path.exists():
        n += 1
        path = dest_dir / f"{PREFIX}{stamp}-{n}{SUFFIX}"
    return path


def backup(db_path: Path, dest_dir: Path, keep: int = DEFAULT_KEEP, *,
           budget_seconds: float | None = CHECK_BUDGET_SECONDS) -> Path:
    """Copy the database at `db_path` into `dest_dir` and rotate. Returns the backup file's path; its manifest
    (`manifest_path`) says whether the copy passed the integrity check. A copy that did not pass is kept and
    reported, never hidden."""
    db_path, dest_dir = Path(db_path), Path(dest_dir)
    if not db_path.is_file():
        raise BackupError(f"there is no database at {db_path}")
    _private_dir(dest_dir)
    final = _new_name(dest_dir)
    partial = final.with_name(final.name + ".partial")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")   # one self-contained file that never changes after this
            dst.commit()
        finally:
            dst.close()
        head = _log_head(src)
    except sqlite3.Error as e:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise BackupError(f"the database could not be copied: {e}") from e
    finally:
        src.close()
    partial.chmod(0o600)
    report = integrity.check(partial, budget_seconds=budget_seconds)
    manifest = {
        "file": final.name, "created_at": datetime.now(UTC).isoformat(), "size": partial.stat().st_size,
        "sha256": _sha256(partial), "relay_version": __version__,
        "source": str(db_path), "log": head, "integrity": _summary(report),
    }
    _write_private(manifest_path(final), json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(partial, final)
    rotate(dest_dir, keep)
    return final


def _summary(report: dict) -> dict[str, Any]:
    return {"status": report["status"], "checked_at": report["checked_at"], "partial": report["partial"],
            "db_ok": report["db"]["ok"], "rooms_checked": report["rooms"]["checked"],
            "rooms_broken": [b["room_id"] for b in report["rooms"]["broken"]],
            "dms_checked": report["dms"]["checked"],
            "dms_broken": [b["thread_id"] for b in report["dms"]["broken"]],
            "log": report["log"]["status"], "log_detail": report["log"]["detail"]}


def rotate(dest_dir: Path, keep: int) -> list[Path]:
    """Remove the oldest backup files (and their manifests) beyond `keep`. The newest backup that passed its
    check is never removed, so a run of damaged copies cannot push the last good one out. The database is
    never touched. Returns what was removed."""
    files = list_backups(dest_dir)
    protected = None
    for p in reversed(files):
        m = _read_manifest(p)
        if m and m.get("integrity", {}).get("status") == "ok":
            protected = p
            break
    gone: list[Path] = []
    for p in files[:max(0, len(files) - max(1, keep))]:
        if p == protected:
            continue
        for f in (p, manifest_path(p)):
            with contextlib.suppress(OSError):
                f.unlink()
        gone.append(p)
    return gone


def latest_age_seconds(dest_dir: Path) -> float | None:
    """Seconds since the newest backup in `dest_dir` was made (None when there is none)."""
    files = list_backups(dest_dir)
    if not files:
        return None
    return max(0.0, time.time() - files[-1].stat().st_mtime)


def backup_if_due(db_path: Path, dest_dir: Path | None = None, keep: int | None = None) -> Path | None:
    """The relay's daily backup: one when `dest_dir` (EMPORIA_DB_BACKUP_DIR) is set and the newest is a day old.
    Returns the new file, or None when nothing was due."""
    dest_dir = dest_dir or backup_dir()
    if dest_dir is None:
        return None
    age = latest_age_seconds(dest_dir)
    if age is not None and age < DAY_SECONDS:
        return None
    return backup(db_path, dest_dir, keep if keep is not None else backup_keep())


# ─── verify and restore ──────────────────────────────────────────────────────

def verify(path: Path) -> dict[str, Any]:
    """A backup against its manifest (size and sha256), then `integrity.check` of the file. Never writes to it.
    -> {"ok": bool, "problems": [str], "manifest": dict | None, "report": dict | None}"""
    path = Path(path)
    problems: list[str] = []
    if not path.is_file():
        return {"ok": False, "problems": [f"{path} is not a file"], "manifest": None, "report": None}
    manifest = _read_manifest(path)
    if manifest is None:
        problems.append(f"there is no readable manifest ({manifest_path(path).name}) beside it: its content "
                        "cannot be proved to be what was backed up")
    else:
        if path.stat().st_size != manifest.get("size"):
            problems.append(f"its size is {path.stat().st_size}, the manifest says {manifest.get('size')}")
        elif _sha256(path) != manifest.get("sha256"):
            problems.append("its sha256 does not match the manifest: the file changed after it was made")
    if problems:
        return {"ok": False, "problems": problems, "manifest": manifest, "report": None}
    report = integrity.check(path)
    if report["status"] != "ok":
        problems.append("it does not pass the integrity check (see the report)")
    return {"ok": not problems, "problems": problems, "manifest": manifest, "report": report}


@contextlib.contextmanager
def _database_lock(db_path: Path) -> Iterator[None]:
    """The flock the relay holds on `<db>.lock` for its whole life (relay_server._hold_the_database): taking it
    here proves no relay is serving this file, and keeps one from starting during the swap."""
    try:
        import fcntl
    except ImportError:         # not POSIX: nothing to take, and nothing to tell
        yield
        return
    lock = db_path.with_name(db_path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock, "a")    # noqa: SIM115 - closed below
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError(f"a relay is running on {db_path} (it holds {lock.name}): stop it first "
                              "(emporia relay stop), restore, then start it again") from None
        yield
    finally:
        handle.close()


def running(db_path: Path) -> bool:
    """True when a relay holds the database right now."""
    try:
        with _database_lock(Path(db_path)):
            return False
    except BackupError:
        return True


def _snapshot_current(db_path: Path, dest_dir: Path) -> Path | None:
    if not db_path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap = dest_dir / f"pre-restore-{stamp}{SUFFIX}"
    n = 1
    while snap.exists():
        n += 1
        snap = dest_dir / f"pre-restore-{stamp}-{n}{SUFFIX}"
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
        try:
            dst = sqlite3.connect(snap)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error:
        # a file SQLite cannot read is the very thing a person may be restoring from: keep its bytes as they are
        with contextlib.suppress(OSError):
            snap.unlink()
        shutil.copyfile(db_path, snap)
        for ext in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + ext)
            if side.exists():
                shutil.copyfile(side, snap.with_name(snap.name + ext))
    snap.chmod(0o600)
    return snap


def restore(backup_file: Path, db_path: Path) -> dict[str, Any]:
    """Put `backup_file` in place of the database at `db_path`. The caller has shown WARNING and asked.
    Raises BackupError (nothing changed) when a relay holds the database or the backup does not verify.
    -> {"snapshot": Path | None, "report": dict (integrity.check of the restored file), "verified": dict}"""
    backup_file, db_path = Path(backup_file), Path(db_path)
    verified = verify(backup_file)
    if not verified["ok"]:
        raise BackupError("the backup is not restored: " + "; ".join(verified["problems"]))
    with _database_lock(db_path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = _snapshot_current(db_path, backup_file.parent)
        fd, tmp_name = tempfile.mkstemp(prefix=db_path.name + ".restoring-", dir=db_path.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copyfile(backup_file, tmp)
            os.chmod(tmp, 0o600)
            if _sha256(tmp) != verified["manifest"]["sha256"]:
                raise BackupError("the copy made for the swap differs from the backup (disk trouble?): "
                                  "nothing was changed")
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
            os.replace(tmp, db_path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        for ext in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                db_path.with_name(db_path.name + ext).unlink()
    return {"snapshot": snapshot, "report": integrity.check(db_path), "verified": verified}


# ─── CLI: emporia relay db … ─────────────────────────────────────────────────

def _fmt_report(report: dict) -> list[str]:
    out = [f"status: {report['status']}" + ("  (not finished: the time budget ran out)" if report["partial"] else ""),
           f"database file: {'ok' if report['db']['ok'] else 'BROKEN'} ({report['db']['detail']})",
           f"room chains: {report['rooms']['checked']} checked, {len(report['rooms']['broken'])} broken"]
    out += [f"  {b['room_id']} ({'public' if b['public'] else 'private'}): {b['detail']}"
            for b in report["rooms"]["broken"]]
    out.append(f"DM chains: {report['dms']['checked']} checked, {len(report['dms']['broken'])} broken")
    out += [f"  {b['thread_id']}: {b['detail']}" for b in report["dms"]["broken"]]
    out.append(f"transparency log: {report['log']['status']} (size {report['log']['size']}): {report['log']['detail']}")
    return out


def _dir_for(args_dir: str) -> Path:
    if args_dir:
        return Path(args_dir).expanduser()
    d = backup_dir()
    if d is None:
        raise BackupError("no backup folder: pass --dir, or set EMPORIA_DB_BACKUP_DIR "
                          "(emporia relay config set EMPORIA_DB_BACKUP_DIR /path)")
    return d


def cli(sub: str, files: list[str], *, dest: str = "", db_path: Path | None = None, yes: bool = False,
        person: bool = False, ask: Any = None) -> int:
    """`emporia relay db SUB [FILE]`. `person` says the terminal is a person's own (restore needs it);
    `ask(question, default) -> bool` is the yes/no prompt."""
    if db_path is None:
        from emporia import relay_server
        db_path = relay_server.DATABASE_PATH
    try:
        if sub == "check":
            target = Path(files[0]).expanduser() if files else db_path
            if not target.is_file():
                print(f"relay db check: {target} is not a file")
                return 1
            from emporia import translog
            print(f"{target}" + ("" if translog.enabled() else "   (the transparency log is off here: EMPORIA_TRANSPARENCY_LOG)"))
            report = integrity.check(target)
            print("\n".join(_fmt_report(report)))
            return 0 if report["status"] == "ok" else 1
        if sub == "backup":
            d = _dir_for(dest)
            made = backup(db_path, d, backup_keep())
            m = _read_manifest(made) or {}
            status = m.get("integrity", {}).get("status", "?")
            print(f"backup: {made} ({m.get('size', 0)} bytes, integrity {status})")
            if status != "ok":
                print("The copy does not pass the integrity check, so the database it came from is damaged. "
                      f"Look at it: emporia relay db check {made}")
                return 1
            return 0
        if sub == "list":
            d = _dir_for(dest)
            found = list_backups(d)
            if not found:
                print(f"no backups in {d}")
                return 0
            for p in found:
                m = _read_manifest(p)
                if m is None:
                    print(f"{p.name}  (no manifest: cannot be restored)")
                    continue
                lg = m.get("log", {})
                print(f"{p.name}  {m.get('created_at', '?')[:19]}Z  {m.get('size', 0)} bytes  "
                      f"integrity {m.get('integrity', {}).get('status', '?')}  v{m.get('relay_version', '?')}  "
                      f"log {lg.get('size', 0)}")
            return 0
        if sub == "restore":
            return _cli_restore(files, dest, db_path, yes=yes, person=person, ask=ask)
    except BackupError as e:
        print(f"relay db {sub}: {e}")
        return 1
    print("relay db: say check, backup, list or restore FILE")
    return 1


def _cli_restore(files: list[str], dest: str, db_path: Path, *, yes: bool, person: bool, ask: Any) -> int:
    if not person:
        print("relay db restore: only a person runs this, in their own terminal.")
        return 1
    if not files:
        print("relay db restore: which backup? (emporia relay db list; give the file's name or path)")
        return 1
    target = Path(files[0]).expanduser()
    if not target.is_absolute() and not target.exists():
        with contextlib.suppress(BackupError):
            target = _dir_for(dest) / files[0]
    if running(db_path):
        print(f"relay db restore: a relay is running on {db_path}: stop it first (emporia relay stop).")
        return 1
    v = verify(target)
    if not v["ok"]:
        print("relay db restore: the backup is not restored: " + "; ".join(v["problems"]))
        if v["report"]:
            print("\n".join(_fmt_report(v["report"])))
        return 1
    m = v["manifest"] or {}
    print(f"Restore {target.name} (made {m.get('created_at', '?')[:19]}Z, log size {m.get('log', {}).get('size', 0)}) "
          f"over {db_path}\n")
    print(WARNING)
    print(f"\nThe current database is copied to {target.parent}/pre-restore-<time>{SUFFIX} first.")
    if not (yes or (ask is not None and ask("Restore this backup, rolling the relay back?", False))):
        print("Not restored.")
        return 1
    done = restore(target, db_path)
    if done["snapshot"]:
        print(f"The database as it was: {done['snapshot']}")
    print(f"Restored {target.name}. Integrity of the restored file:")
    print("\n".join(_fmt_report(done["report"])))
    print("Start the relay again: emporia relay start")
    return 0 if done["report"]["status"] == "ok" else 1
