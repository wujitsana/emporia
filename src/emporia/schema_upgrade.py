"""`emporia relay upgrade`: carry a relay database from an older schema version to this one.

The upgrade is generic and additive-only. The target shape is built from `schema_sql.SCHEMA_SQL` in an
in-memory database and compared with the file's tables, columns and indexes. The plan may create missing
tables, add missing columns that are nullable or carry a default, and create missing indexes (and
triggers or views, if the schema ever has any). Everything else is refused by name: a removed or renamed
column or table, a changed type, NOT NULL, default, primary key, foreign key or unique constraint, or a file
from a newer relay. A refused file is left exactly as it was.

Order: refuse a running relay, take a backup with the sqlite backup API (never deleted), then apply the whole
plan in one transaction, stamp `PRAGMA user_version`, re-compare, run `integrity_check` and
`foreign_key_check`, and only then commit. Anything that fails rolls the transaction back.

What it does not touch: the transparency log's own tables and triggers (`translog_*`, `idx_translog_*`) are
the log's to manage; the relay rebuilds or folds them in at its next start (`translog.init_schema`).
Inline CHECK constraints on an existing column are not compared.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from emporia.schema import SCHEMA_VERSION
from emporia.schema_sql import SCHEMA_SQL


class UpgradeError(Exception):
    """The upgrade cannot or must not go ahead; the message is for the operator."""


def _managed_elsewhere(name: str) -> bool:
    """The transparency log keeps its own tables, indexes and triggers (translog.init_schema)."""
    return name.startswith(("translog_", "idx_translog", "trg_translog", "sqlite_"))


@dataclass
class Plan:
    from_version: int
    to_version: int
    statements: list[tuple[str, str]] = field(default_factory=list)    # (what, sql)
    refusals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def noop(self) -> bool:
        return not self.statements and not self.refusals and self.from_version == self.to_version

    def lines(self) -> list[str]:
        out = [f"schema version: database {self.from_version}, this relay {self.to_version}"]
        if self.refusals:
            out.append("cannot upgrade, because:")
            out += [f"  - {r}" for r in self.refusals]
        elif self.noop:
            out.append("nothing to do: the database is already at this schema")
        else:
            out.append(f"{len(self.statements)} change(s), all additive:")
            out += [f"  + {what}" for what, _ in self.statements]
        out += [f"  note: {n}" for n in self.notes]
        return out


# ─── reading a shape ─────────────────────────────────────────────────────────

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _norm(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip()).rstrip(";").strip()


def _tables(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """name -> (type, notnull, default, pk position)."""
    return {r[1]: ((r[2] or "").upper(), int(r[3]), r[4], int(r[5]))
            for r in conn.execute(f"PRAGMA table_info({_q(table)})")}


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple]:
    return {(r[3], r[2], r[4], r[5].upper(), r[6].upper())
            for r in conn.execute(f"PRAGMA foreign_key_list({_q(table)})")}


def _unique_sets(conn: sqlite3.Connection, table: str) -> set[tuple]:
    """Column lists of the table's constraint-made unique indexes (UNIQUE in the CREATE TABLE)."""
    out = set()
    for r in conn.execute(f"PRAGMA index_list({_q(table)})"):
        if r[3] == "u":
            out.add(tuple(c[2] for c in conn.execute(f"PRAGMA index_info({_q(r[1])})")))
    return out


def _indexes(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """name -> (table, normalised sql) of the explicitly created indexes."""
    return {r[0]: (r[1], _norm(re.sub(r"(?i)IF NOT EXISTS\s+", "", r[2])))
            for r in conn.execute("SELECT name, tbl_name, sql FROM sqlite_master "
                                  "WHERE type = 'index' AND sql IS NOT NULL")}


def _other_objects(conn: sqlite3.Connection, kind: str) -> dict[str, tuple[str, str]]:
    return {r[0]: (r[1], _norm(re.sub(r"(?i)IF NOT EXISTS\s+", "", r[2])))
            for r in conn.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = ?", (kind,))}


def _target() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    return conn


def _split_top(body: str) -> list[str]:
    parts, depth, cur, quote = [], 0, [], ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"`[":
            quote = "]" if ch == "[" else ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _column_ddl(create_sql: str, column: str) -> str | None:
    """The column's own definition, verbatim from the target's CREATE TABLE."""
    start, end = create_sql.find("("), create_sql.rfind(")")
    if start < 0 or end < start:
        return None
    for part in _split_top(create_sql[start + 1:end]):
        first = re.match(r'\s*(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\w+))', part)
        if first and next(g for g in first.groups() if g) == column:
            return part
    return None


# ─── the plan ────────────────────────────────────────────────────────────────

def plan_for(conn: sqlite3.Connection) -> Plan:
    """What it takes to bring the open database to this schema, or why it cannot be done."""
    found = int(conn.execute("PRAGMA user_version").fetchone()[0])
    plan = Plan(found, SCHEMA_VERSION)
    if found > SCHEMA_VERSION:
        plan.refusals.append(f"the database was written by a newer relay (schema {found}; this one knows "
                             f"{SCHEMA_VERSION}): run that relay, or a newer `emporia`")
        return plan
    want = _target()
    try:
        have_tables, want_tables = _tables(conn), _tables(want)
        for name in sorted(have_tables):
            if name not in want_tables and not _managed_elsewhere(name):
                plan.refusals.append(f"table {name} is in the database but not in this relay's schema "
                                     "(removed or renamed): an upgrade never drops data")
        for name in sorted(want_tables):
            if name not in have_tables:
                plan.statements.append((f"create table {name}", want_tables[name]))
                continue
            _compare_table(conn, want, name, want_tables[name], plan)
        have_idx, want_idx = _indexes(conn), _indexes(want)
        for name, (table, sql) in sorted(want_idx.items()):
            if name not in have_idx:
                plan.statements.append((f"create index {name} on {table}", sql))
            elif have_idx[name] != (table, sql):
                plan.refusals.append(f"index {name} differs (database: {have_idx[name][1]!r}; "
                                     f"this relay: {sql!r}): an upgrade only adds indexes")
        extra = sorted(n for n in have_idx if n not in want_idx and not _managed_elsewhere(n))
        if extra:
            plan.notes.append("indexes in the database that this schema does not name are left alone: "
                              + ", ".join(extra))
        for kind in ("trigger", "view"):
            have_o, want_o = _other_objects(conn, kind), _other_objects(want, kind)
            for name, (table, sql) in sorted(want_o.items()):
                if name not in have_o:
                    plan.statements.append((f"create {kind} {name}", sql))
                elif have_o[name] != (table, sql):
                    plan.refusals.append(f"{kind} {name} differs from this relay's schema: an upgrade only adds")
    finally:
        want.close()
    return plan


def _compare_table(conn: sqlite3.Connection, want: sqlite3.Connection, table: str, create_sql: str,
                   plan: Plan) -> None:
    have_cols, want_cols = _columns(conn, table), _columns(want, table)
    for col in sorted(have_cols):
        if col not in want_cols:
            plan.refusals.append(f"column {table}.{col} is in the database but not in this relay's schema "
                                 "(removed or renamed): an upgrade never drops data")
    for col, (typ, notnull, default, pk) in want_cols.items():
        if col in have_cols:
            h_typ, h_notnull, h_default, h_pk = have_cols[col]
            for what, a, b in (("type", h_typ, typ), ("NOT NULL", h_notnull, notnull),
                               ("default", h_default, default), ("primary key", h_pk, pk)):
                if a != b:
                    plan.refusals.append(f"column {table}.{col} changed its {what} (database: {a!r}, this "
                                         f"relay: {b!r}): an upgrade does not rewrite a column")
            continue
        if pk:
            plan.refusals.append(f"new column {table}.{col} is part of the primary key: it cannot be added")
            continue
        if notnull and default is None:
            plan.refusals.append(f"new column {table}.{col} is NOT NULL with no default: existing rows would "
                                 "have no value for it")
            continue
        ddl = _column_ddl(create_sql, col)
        if not ddl or re.search(r"(?i)\b(UNIQUE|PRIMARY\s+KEY|GENERATED|AS\s*\()", ddl):
            plan.refusals.append(f"new column {table}.{col} cannot be added in place (unique, generated or not "
                                 "readable from the schema)")
            continue
        if default is not None and re.search(r"(?i)CURRENT_(TIME|DATE|TIMESTAMP)|\(", str(default)):
            plan.refusals.append(f"new column {table}.{col} has a non-constant default: SQLite cannot add it")
            continue
        plan.statements.append((f"add column {table}.{col}", f"ALTER TABLE {_q(table)} ADD COLUMN {ddl}"))
    if _foreign_keys(conn, table) != _foreign_keys(want, table):
        added_only = _foreign_keys(want, table) - _foreign_keys(conn, table)
        new_cols = {c for c in want_cols if c not in have_cols}
        removed = _foreign_keys(conn, table) - _foreign_keys(want, table)
        if removed or any(fk[0] not in new_cols for fk in added_only):
            plan.refusals.append(f"table {table} changed its foreign keys: an upgrade does not rewrite a table")
    new_cols = {c for c in want_cols if c not in have_cols}
    if any(not set(u) & new_cols for u in _unique_sets(want, table) - _unique_sets(conn, table)):
        plan.refusals.append(f"table {table} gained a UNIQUE constraint: an upgrade does not rewrite a table")
    if _unique_sets(conn, table) - _unique_sets(want, table):
        plan.refusals.append(f"table {table} lost a UNIQUE constraint: an upgrade does not rewrite a table")


# ─── doing it ────────────────────────────────────────────────────────────────

def backup_dir_for(db_path: Path) -> Path:
    raw = os.getenv("EMPORIA_DB_BACKUP_DIR", "").strip()
    return Path(raw).expanduser() if raw else db_path.parent


def make_backup(db_path: Path, from_version: int, dest_dir: Path | None = None) -> Path:
    """A consistent copy by the sqlite backup API: `<name>.pre-upgrade-s<from>-<UTC stamp>.sqlite3`. Never
    deleted by anything here. The copy is opened and integrity-checked before it is trusted."""
    dest_dir = dest_dir or backup_dir_for(db_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = dest_dir / f"{db_path.stem}.pre-upgrade-s{from_version}-{stamp}.sqlite3"
    if dest.exists():
        raise UpgradeError(f"{dest} already exists: run it again")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    out = sqlite3.connect(dest)
    try:
        with contextlib.suppress(OSError):
            os.chmod(dest, 0o600)
        src.backup(out)
        out.commit()
        verdict = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
        src.close()
    if verdict != "ok":
        raise UpgradeError(f"the backup {dest} does not pass integrity_check ({verdict}): the database itself "
                           "is damaged; nothing was changed. Look at it with `emporia relay db check`")
    return dest


def _inspect(db_path: Path) -> Plan:
    if not db_path.is_file():
        raise UpgradeError(f"there is no database at {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as e:
        raise UpgradeError(f"cannot open {db_path}: {e}") from e
    try:
        try:
            if not _tables(conn):
                raise UpgradeError(f"{db_path} has no tables: there is nothing to upgrade (the relay makes a "
                                   "new database itself)")
            return plan_for(conn)
        except sqlite3.DatabaseError as e:
            raise UpgradeError(f"{db_path} is not a readable SQLite database: {e}") from e
    finally:
        conn.close()


def dry_run(db_path: Path) -> Plan:
    return _inspect(Path(db_path))


@dataclass
class Result:
    plan: Plan
    backup: Path | None
    applied: int


def upgrade(db_path: Path, *, backup_dir: Path | None = None) -> Result:
    """Back up, then apply the plan in one transaction. Raises UpgradeError, with the file unchanged, on any
    refusal or failure."""
    from emporia import relay_db

    db_path = Path(db_path)
    plan = _inspect(db_path)
    if plan.refusals:
        raise UpgradeError("\n".join(plan.lines()))
    if plan.noop:
        return Result(plan, None, 0)
    try:
        lock = relay_db._database_lock(db_path)
        lock.__enter__()
    except relay_db.BackupError:
        raise UpgradeError(f"a relay is running on {db_path}: stop it first (emporia relay stop, or "
                           "`docker compose stop emporia-relay`), upgrade, then start it again") from None
    try:
        probe = sqlite3.connect(db_path, timeout=1.0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
        except sqlite3.OperationalError:
            raise UpgradeError(f"{db_path} is locked by another process (a relay still running?): stop it, "
                               "then run this again") from None
        finally:
            probe.close()
        backup = make_backup(db_path, plan.from_version, backup_dir)
        conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            fk_before = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            conn.execute("BEGIN IMMEDIATE")
            try:
                for _what, sql in plan.statements:
                    conn.execute(sql)
                conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
                left = plan_for(conn)
                if left.refusals or left.statements:
                    raise UpgradeError("after the changes the database still differs from the schema ("
                                       + "; ".join(left.refusals or [w for w, _ in left.statements]) + ")")
                verdict = conn.execute("PRAGMA integrity_check").fetchall()
                if [r[0] for r in verdict] != ["ok"]:
                    raise UpgradeError("integrity_check failed after the upgrade: "
                                       + "; ".join(str(r[0]) for r in verdict[:5]))
                fk_after = conn.execute("PRAGMA foreign_key_check").fetchall()
                if len(fk_after) > fk_before:
                    raise UpgradeError(f"foreign_key_check found {len(fk_after) - fk_before} new violation(s) "
                                       "after the upgrade")
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as e:
            raise UpgradeError(f"the upgrade failed and was rolled back ({e}); the backup is {backup}") from e
        finally:
            conn.close()
        return Result(plan, backup, len(plan.statements))
    finally:
        lock.__exit__(None, None, None)


# ─── the command ─────────────────────────────────────────────────────────────

def cli(db_path: Path, *, dry: bool, yes: bool, tty: bool, ask=None) -> int:
    """`emporia relay upgrade`. `ask(question, default) -> bool` is the yes/no prompt."""
    try:
        plan = _inspect(db_path)
        print(f"database: {db_path}")
        print("\n".join(plan.lines()))
        if plan.refusals:
            print("\nNothing was changed. Move the file aside and start fresh only if you do not need its history.")
            return 1
        if plan.noop:
            return 0
        if dry:
            print(f"\nDry run: nothing was changed. A backup would go to {backup_dir_for(db_path)}"
                  f"/{db_path.stem}.pre-upgrade-s{plan.from_version}-<UTC stamp>.sqlite3")
            return 0
        if not yes:
            if not tty or ask is None:
                print("\nNothing was changed: this asks for a yes in a terminal; pass --yes in a script.")
                return 1
            if not ask("Stop the relay first, then back up and upgrade this database?", False):
                print("Nothing was changed.")
                return 1
        result = upgrade(db_path)
    except UpgradeError as e:
        print(f"relay upgrade: {e}")
        return 1
    print(f"\nBacked up first: {result.backup} (kept; nothing deletes it)")
    print(f"Upgraded {db_path}: schema {result.plan.from_version} -> {result.plan.to_version}, "
          f"{result.applied} change(s), integrity_check and foreign_key_check passed.")
    print("Start the relay again. It folds the transparency log in on its first start, which can take a moment.")
    return 0
