"""The relay's integrity check: its database file, every room chain, every DM chain and the transparency log.

Detection only. It reads; it never repairs, pauses or deletes anything, and it never writes to the database.
It works on any database file, so a backup can be checked the same way as the live one (`check(path)`).

A signed removal is not a break: `rooms.verify_chain` and the DM check take a removed message's stored link
as it is, exactly as a reader's check does.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emporia import translog

#: How many public room ids /health names.
_PUBLIC_LISTED = 20


#: Set on relay shutdown: a pass in its thread ends at its next budget check instead of running out its time.
#: Each budget keeps the event that was current when it began; a stop sets that one and starts a fresh one,
#: so a pass begun after the stop (the next relay in the same process, a direct call) is not stopped.
_STOP = threading.Event()


def request_stop() -> None:
    global _STOP
    stopped, _STOP = _STOP, threading.Event()
    stopped.set()


def clear_stop() -> None:
    global _STOP
    _STOP = threading.Event()


class _Budget:
    def __init__(self, seconds: float | None):
        self.deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)
        self.spent = False
        self._stop = _STOP

    def out(self) -> bool:
        if self._stop.is_set():
            self.spent = True
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.spent = True
        return self.spent


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _check_file(db_path: Path, budget: _Budget) -> dict[str, Any]:
    """PRAGMA quick_check, interrupted when the budget runs out."""
    try:
        conn = _open(db_path)
    except sqlite3.Error as e:
        return {"ok": False, "detail": f"cannot open: {e}"}
    try:
        if budget.deadline is not None:
            conn.set_progress_handler(lambda: 1 if budget.out() else 0, 10000)
        rows = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.OperationalError as e:
        if budget.spent:
            return {"ok": True, "detail": "not finished: the time budget ran out"}
        return {"ok": False, "detail": f"quick_check failed: {e}"}
    except sqlite3.DatabaseError as e:
        return {"ok": False, "detail": f"quick_check failed: {e}"}
    finally:
        conn.close()
    if rows == ["ok"]:
        return {"ok": True, "detail": "ok"}
    return {"ok": False, "detail": "; ".join(str(r) for r in rows[:5])}


def _check_rooms(db_path: Path, budget: _Budget) -> dict[str, Any]:
    """`rooms.verify_chain` opens its connection in WAL mode, which would rewrite the header of a file that is
    not in it (a copied or restored one); such a file is walked as a throwaway copy so the check never writes."""
    try:
        conn = _open(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        mode = "wal"
    if str(mode).lower() == "wal":
        return _walk_rooms(db_path, budget)
    import shutil
    import tempfile
    # a throwaway copy beside the file being checked (never the system temp dir: that may be a small tmpfs),
    # removed whatever happens
    scratch: Path | None = None
    try:
        scratch = Path(tempfile.mkdtemp(prefix=".emporia-integrity-", dir=db_path.parent))
        copy = scratch / "copy.sqlite3"
        shutil.copyfile(db_path, copy)
        return _walk_rooms(copy, budget)
    except OSError as e:
        return {"checked": 0, "broken": [{"room_id": "", "public": False,
                                          "detail": f"could not be checked: {type(e).__name__}: {e}"}]}
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _walk_rooms(db_path: Path, budget: _Budget) -> dict[str, Any]:
    from emporia import rooms
    out: dict[str, Any] = {"checked": 0, "broken": []}
    try:
        conn = _open(db_path)
        try:
            if "room_messages" not in _tables(conn):
                return out
            ids = [r[0] for r in conn.execute("SELECT DISTINCT room_id FROM room_messages ORDER BY room_id")]
            info = {}
            if "rooms" in _tables(conn):
                for r in conn.execute("SELECT * FROM rooms").fetchall():
                    keys = r.keys()
                    info[r["room_id"]] = (r["room_type"] == "public" and r["gate_type"] == "open"
                                          and not ("encrypted" in keys and r["encrypted"]))
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"checked": 0, "broken": [{"room_id": "", "public": False, "detail": f"cannot read rooms: {e}"}]}
    for room_id in ids:
        if budget.out():
            break
        try:
            v = rooms.verify_chain(room_id, db_path)
        except Exception as e:      # noqa: BLE001 — a check that cannot run is reported as failed, never raised; a check that cannot run is a finding, not a crash
            ok, detail = False, f"could not be checked: {type(e).__name__}: {e}"
        else:
            ok, detail = v.ok, v.message
        out["checked"] += 1
        if not ok:
            out["broken"].append({"room_id": room_id, "public": bool(info.get(room_id, False)), "detail": detail})
    return out


def _check_dms(db_path: Path, budget: _Budget) -> dict[str, Any]:
    # the one recomputation, imported late: routes.dm imports the relay, and only loads after relay_server does
    from emporia import relay_server  # noqa: F401
    from emporia.routes.dm import _verify_dm_chain
    out: dict[str, Any] = {"checked": 0, "broken": []}
    try:
        conn = _open(db_path)
        try:
            if "dm_messages" not in _tables(conn):
                return out
            ids = [r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM dm_messages ORDER BY thread_id")]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"checked": 0, "broken": [{"thread_id": "", "detail": f"cannot read DMs: {e}"}]}
    for thread_id in ids:
        if budget.out():
            break
        try:
            v = _verify_dm_chain(thread_id, None, db_path)
        except Exception as e:  # noqa: BLE001 — a check that cannot run is reported as failed, never raised
            ok, detail = False, f"could not be checked: {type(e).__name__}: {e}"
        else:
            ok, detail = v.ok, v.message
        out["checked"] += 1
        if not ok:
            out["broken"].append({"thread_id": thread_id, "detail": detail})
    return out


def _check_log(db_path: Path, relay_public_keys: Sequence[str], budget: _Budget) -> dict[str, Any]:
    if not translog.enabled():
        return {"status": "off", "size": 0, "detail": "the transparency log is off"}
    try:
        conn = _open(db_path)
    except sqlite3.Error as e:
        return {"status": "broken", "size": 0, "detail": f"cannot open: {e}"}
    try:
        if "translog_leaves" not in _tables(conn):
            return {"status": "empty", "size": 0, "detail": "no log tables"}
        return _verify_log(conn, relay_public_keys, budget)
    except sqlite3.Error as e:
        return {"status": "broken", "size": 0, "detail": f"cannot read the log: {e}"}
    finally:
        conn.close()


def _verify_log(conn: sqlite3.Connection, relay_public_keys: Sequence[str], budget: _Budget) -> dict[str, Any]:
    from emporia import identity
    problems: list[str] = []

    def note(msg: str) -> None:
        if len(problems) < 10:
            problems.append(msg)

    n = conn.execute("SELECT COUNT(*) FROM translog_leaves").fetchone()[0]
    if n == 0:
        return {"status": "empty", "size": 0, "detail": "no entries yet"}
    # leaves: contiguous from 0, each leaf's hash recomputed from its record
    hashes: list[bytes] = []
    for r in conn.execute("SELECT idx, record FROM translog_leaves ORDER BY idx"):
        if r["idx"] != len(hashes):
            note(f"leaf {len(hashes)} is missing (next stored is {r['idx']})")
            return {"status": "broken", "size": n, "detail": "; ".join(problems)}
        hashes.append(translog.leaf_hash(r["record"].encode()))
        if len(hashes) % 5000 == 0 and budget.out():
            return {"status": "ok", "size": n, "detail": "not finished: the time budget ran out"}
    # stored subtree hashes against the recomputed ones (level L, index i covers 2^L leaves)
    level, row = 0, hashes
    while len(row) >= 2:
        row = [translog.node_hash(row[i], row[i + 1]) for i in range(0, len(row) - 1, 2)]
        level += 1
        stored = {r["idx"]: bytes(r["hash"]) for r in conn.execute(
            "SELECT idx, hash FROM translog_nodes WHERE level = ?", (level,))}
        for i, h in enumerate(row):
            got = stored.pop(i, None)
            if got is None:
                note(f"subtree hash {level}/{i} is missing")
            elif got != h:
                note(f"subtree hash {level}/{i} does not match its leaves")
        if stored:
            note(f"{len(stored)} stored subtree hash(es) at level {level} beyond the log's end")
        if budget.out():
            break
    # signed states: signature, key, root at its size, and sizes that never shrink within an epoch
    states = conn.execute("SELECT * FROM translog_states ORDER BY size").fetchall()
    tree = translog._Tree(leaves=hashes)
    allowed = set(relay_public_keys)
    last_by_epoch: dict[Any, tuple[int, str]] = {}
    for s in states:
        if budget.out():
            break
        if s["size"] > n:
            note(f"a signed state claims size {s['size']}, the log holds {n}")
            continue
        state = translog._state_row(s)
        if not identity.verify_raw(translog.state_bytes(state), s["signature"], s["relay_public_key"]):
            note(f"the signature of the state at size {s['size']} does not verify")
        elif allowed and s["relay_public_key"] not in allowed:
            note(f"the state at size {s['size']} is signed by a key that is not the relay's")
        if tree.mth(0, s["size"]).hex() != s["root"]:
            note(f"the root of the state at size {s['size']} does not match the recomputed root")
        epoch = s["epoch"] if "epoch" in s.keys() else None    # a file from before that column has none
        prev = last_by_epoch.get(epoch)
        if prev is not None and s["at"] < prev[1]:
            note(f"the log shrank: size {s['size']} was signed at {s['at']}, before size {prev[0]} at {prev[1]}")
        last_by_epoch[epoch] = (s["size"], max(s["at"], prev[1]) if prev else s["at"])
    if problems:
        return {"status": "broken", "size": n, "detail": "; ".join(problems)}
    detail = "ok" if states else "ok (no signed state yet)"
    if budget.spent:
        detail = "not finished: the time budget ran out"
    return {"status": "ok", "size": n, "detail": detail}


def _safely(run: Any, failed: dict[str, Any]) -> dict[str, Any]:
    """One section of the check; whatever it raises is a finding ("could not be checked"), never a crash."""
    try:
        return run()
    except Exception as e:      # noqa: BLE001 - the check never raises
        detail = f"could not be checked: {type(e).__name__}: {e}"
        out = dict(failed)
        if "broken" in out:
            out["broken"] = [{**b, "detail": detail} for b in out["broken"]]
        else:
            out["detail"] = detail
        return out


def check(db_path: Path, *, relay_public_keys: Sequence[str] = (), budget_seconds: float | None = None) -> dict:
    """Check the database at `db_path`. `budget_seconds` bounds the whole pass: what it did not reach is left
    unchecked and `partial` says so. `relay_public_keys` are the relay's current and earlier keys; with none
    given, a signed state need only verify under the key it names. Never raises: a section that cannot run is
    reported as not checked (and so as degraded)."""
    db_path = Path(db_path)
    budget = _Budget(budget_seconds)
    report: dict[str, Any] = {"status": "ok", "checked_at": datetime.now(UTC).isoformat()}
    report["db"] = _safely(lambda: _check_file(db_path, budget), {"ok": False})
    if report["db"]["ok"]:
        report["rooms"] = _safely(lambda: _check_rooms(db_path, budget),
                                  {"checked": 0, "broken": [{"room_id": "", "public": False}]})
        report["dms"] = _safely(lambda: _check_dms(db_path, budget), {"checked": 0, "broken": [{"thread_id": ""}]})
        report["log"] = _safely(lambda: _check_log(db_path, relay_public_keys, budget),
                                {"status": "broken", "size": 0})
    else:       # a file that is not sound is not walked further
        report["rooms"] = {"checked": 0, "broken": []}
        report["dms"] = {"checked": 0, "broken": []}
        report["log"] = {"status": "off" if not translog.enabled() else "empty", "size": 0,
                         "detail": "not checked: the database file is not sound"}
    report["partial"] = budget.spent
    if (not report["db"]["ok"] or report["rooms"]["broken"] or report["dms"]["broken"]
            or report["log"]["status"] == "broken"):
        report["status"] = "degraded"
    return report


def public_summary(report: dict | None) -> dict:
    """What /health says: counts, and the ids of public rooms only. Never a private room id or a DM thread."""
    if not report:
        return {"status": "unknown", "checked_at": None, "rooms_broken": 0, "public_rooms": [], "dms_broken": 0,
                "log": "unknown", "db": "unknown", "partial": False}
    broken = report["rooms"]["broken"]
    return {
        "status": report["status"],
        "checked_at": report["checked_at"],
        "rooms_broken": len(broken),
        "public_rooms": [b["room_id"] for b in broken if b["public"]][:_PUBLIC_LISTED],
        "dms_broken": len(report["dms"]["broken"]),
        "log": report["log"]["status"],
        "db": "ok" if report["db"]["ok"] else "broken",
        "partial": bool(report.get("partial")),
    }
