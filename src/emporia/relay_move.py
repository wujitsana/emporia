"""Move a relay's database and identity to a new home (another volume, another host).

    python -m emporia.relay_move export OUT_DIR     # on the old relay, with its settings
    python -m emporia.relay_move import IN_DIR      # in the new relay's container

`export` reads EMPORIA_DB_PATH and EMPORIA_KEYS_DIR as the relay does and writes
OUT_DIR/emporia.sqlite3 (a consistent copy, safe while the relay runs; SQLite's own
backup, then an integrity check) and OUT_DIR/keys/ with only the relay's own files: its
identity key and id, key history, session-signing secret, federation pins and escrow
operator key. A keys directory also holds agents' keys when a Hermes profile shares it;
those are never copied (an allowlist, not a blocklist). Stop the old relay before
copying the folder: the identity must not be live on two hosts.

`import` refuses to replace a database already there (a relay that has run) unless
`--force`, checks the copy, and writes files 0600 in the new keys directory.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

DB_NAME = "emporia.sqlite3"
# The relay's own files in its keys directory: everything `_relay…`, the escrow operator
# key and the federation pins. Nothing else — an agent's key is not the relay's to move.
OWN_FILES = ("relay_operator.secp256k1", "federation_peer_pubkeys.json")
OWN_PREFIX = "_relay"


def relay_files(keys_dir: Path) -> list[Path]:
    if not keys_dir.is_dir():
        return []
    return sorted(p for p in keys_dir.iterdir()
                  if p.is_file() and not p.is_symlink() and (p.name.startswith(OWN_PREFIX) or p.name in OWN_FILES))


def _check(db: Path) -> None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise SystemExit(f"relay_move: {db} fails its integrity check: {result}")


def export(db: Path, keys_dir: Path, out: Path) -> list[Path]:
    if not db.is_file():
        raise SystemExit(f"relay_move: no database at {db}")
    keys = relay_files(keys_dir)
    if not any(p.name.startswith("_relay_identity") for p in keys):
        raise SystemExit(f"relay_move: no relay identity key in {keys_dir} — without it a copy is a different relay")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    (out / "keys").mkdir(exist_ok=True, mode=0o700)
    dest = out / DB_NAME
    if dest.exists():
        raise SystemExit(f"relay_move: {dest} exists; use an empty folder")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    dest.chmod(0o600)
    _check(dest)
    written = [dest]
    for p in keys:
        target = out / "keys" / p.name
        shutil.copyfile(p, target)
        target.chmod(0o600)
        written.append(target)
    return written


def import_(src: Path, db: Path, keys_dir: Path, force: bool = False) -> list[Path]:
    source_db = src / DB_NAME
    if not source_db.is_file() or not relay_files(src / "keys"):
        raise SystemExit(f"relay_move: {src} is not an export (no {DB_NAME} and keys/_relay…)")
    if (db.exists() or any(relay_files(keys_dir))) and not force:
        raise SystemExit(f"relay_move: {db} or the keys in {keys_dir} already exist — this relay has run. "
                         "Nothing replaced; --force only if you mean to overwrite it.")
    _check(source_db)
    db.parent.mkdir(parents=True, exist_ok=True)
    keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for suffix in ("", "-wal", "-shm"):                       # a stale journal would be replayed onto the copy
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(source_db, db)
    db.chmod(0o600)
    _check(db)
    written = [db]
    for p in relay_files(src / "keys"):
        target = keys_dir / p.name
        shutil.copyfile(p, target)
        target.chmod(0o600)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    if len(argv) != 2 or argv[0] not in ("export", "import"):
        print(__doc__)
        return 2
    from emporia import config

    keys_dir = Path(os.getenv("EMPORIA_KEYS_DIR", "~/.hermes/keys")).expanduser()
    db = config.database_path()
    folder = Path(argv[1]).expanduser()
    done = export(db, keys_dir, folder) if argv[0] == "export" else import_(folder, db, keys_dir, force)
    for path in done:
        print(f"  {argv[0]}ed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
