"""The integrity check (integrity.py): database file, room chains, DM chains, transparency log, and what
/health says of it. Tests corrupt a scratch database with SQL as test setup (a check that cannot see damage
proves nothing); the relay itself never does that."""
import asyncio
import hashlib
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from emporia import integrity, translog
from emporia import relay_server as rs
from emporia.rooms import compute_chain_hash
from emporia.schema_sql import SCHEMA_SQL

_KEY = ed25519.Ed25519PrivateKey.generate()
_PUB = _KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _sign(data: bytes) -> str:
    return _KEY.sign(data).hex()


@pytest.fixture
def log_on(monkeypatch):
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")


def _db(tmp_path: Path, name: str = "scratch.sqlite3") -> Path:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    return path


def _room(path: Path, room_id: str, texts: list[str], *, public: bool = True) -> None:
    conn = sqlite3.connect(path)
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO rooms (room_id, name, room_type, gate_type, creator_id, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (room_id, room_id, "public" if public else "private", "open" if public else "invite",
                  "alice", now, now))
    prev = None
    for i, text in enumerate(texts):
        at = f"2026-11-01T00:00:{i:02d}+00:00"
        h = compute_chain_hash(prev, "alice", text, at)
        conn.execute("INSERT INTO room_messages (message_id, room_id, sender_id, content, chain_hash, created_at) "
                     "VALUES (?,?,?,?,?,?)", (f"{room_id}-{i}", room_id, "alice", text, h, at))
        prev = h
    conn.commit()
    conn.close()


def _dm(path: Path, thread_id: str, texts: list[str]) -> None:
    conn = sqlite3.connect(path)
    prev = None
    for i, text in enumerate(texts):
        at = f"2026-11-02T00:00:{i:02d}+00:00"
        h = compute_chain_hash(prev, "alice", text, at)
        conn.execute("INSERT INTO dm_messages (message_id, thread_id, sender_id, content, created_at, chain_hash) "
                     "VALUES (?,?,?,?,?,?)", (f"{thread_id}-{i}", thread_id, "alice", text, at, h))
        prev = h
    conn.commit()
    conn.close()


def _log(path: Path, entries: int = 5, sign_at: int | None = None) -> None:
    """A log with `entries` leaves, one signed state (at `sign_at`, default all)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    translog.init_schema(conn)
    for i in range(entries):
        translog.append(conn, "test", f"ref{i}", {"n": i})
    n = sign_at or entries
    state = {"type": translog.STATE_TYPE, "relay_public_key": _PUB, "size": n,
             "root": translog.stored_root(conn, n).hex(), "at": datetime.now(UTC).isoformat(), "epoch": 1}
    sig = _sign(translog.state_bytes(state))
    conn.execute("INSERT INTO translog_states (size, root, at, relay_public_key, signature, epoch) "
                 "VALUES (?,?,?,?,?,?)", (n, state["root"], state["at"], _PUB, sig, 1))
    conn.commit()
    conn.close()


def _sql(path: Path, sql: str, *args) -> None:
    conn = sqlite3.connect(path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _full(tmp_path: Path) -> Path:
    path = _db(tmp_path)
    _room(path, "pub", ["a", "b", "c"])
    _room(path, "priv", ["x", "y", "z"], public=False)
    _dm(path, "dm1", ["hi", "yo", "bye"])
    _log(path)
    return path


def test_a_clean_database_is_ok(tmp_path, log_on):
    report = integrity.check(_full(tmp_path), relay_public_keys=[_PUB])
    assert report["status"] == "ok", report
    assert report["db"]["ok"] and report["partial"] is False
    assert report["rooms"] == {"checked": 2, "broken": []}
    assert report["dms"] == {"checked": 1, "broken": []}
    assert report["log"]["status"] == "ok" and report["log"]["size"] == 5


def test_the_check_does_not_change_the_database_file(tmp_path, log_on):
    path = _full(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    integrity.check(path, relay_public_keys=[_PUB])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_file_not_in_wal_mode_is_checked_without_being_written(tmp_path, log_on):
    path = _full(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    for ext in ("-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert integrity.check(path, relay_public_keys=[_PUB])["status"] == "ok"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_rewritten_room_message_is_degraded_and_names_the_room(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE room_messages SET content = 'rewritten' WHERE message_id = 'pub-1'")
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["status"] == "degraded"
    assert [(b["room_id"], b["public"]) for b in report["rooms"]["broken"]] == [("pub", True)]
    assert "message 1" in report["rooms"]["broken"][0]["detail"]


def test_a_deleted_middle_row_breaks_the_private_room_and_is_marked_private(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "DELETE FROM room_messages WHERE message_id = 'priv-1'")
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["status"] == "degraded"
    assert [(b["room_id"], b["public"]) for b in report["rooms"]["broken"]] == [("priv", False)]


def test_a_signed_removal_is_not_a_break(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE room_messages SET content = '', deleted_at = ? WHERE message_id = 'pub-1'",
         datetime.now(UTC).isoformat())
    _sql(path, "UPDATE dm_messages SET content = '', deleted_at = ? WHERE message_id = 'dm1-1'",
         datetime.now(UTC).isoformat())
    assert integrity.check(path, relay_public_keys=[_PUB])["status"] == "ok"


def test_a_tampered_dm_row_is_degraded(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE dm_messages SET content = 'changed' WHERE message_id = 'dm1-2'")
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["status"] == "degraded"
    assert [b["thread_id"] for b in report["dms"]["broken"]] == ["dm1"]


def test_a_changed_log_leaf_is_degraded(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE translog_leaves SET record = replace(record, '\"n\":2', '\"n\":9') WHERE idx = 2")
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["status"] == "degraded"
    assert report["log"]["status"] == "broken"
    assert "does not match" in report["log"]["detail"]


def test_a_changed_subtree_hash_is_degraded(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE translog_nodes SET hash = zeroblob(32) WHERE level = 1 AND idx = 0")
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["log"]["status"] == "broken" and "1/0" in report["log"]["detail"]


def test_a_signed_state_with_a_wrong_root_is_degraded(tmp_path, log_on):
    path = _db(tmp_path)
    _log(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    s = conn.execute("SELECT * FROM translog_states").fetchone()
    bad = {"type": translog.STATE_TYPE, "relay_public_key": _PUB, "size": s["size"], "root": "00" * 32,
           "at": s["at"], "epoch": 1}
    conn.execute("UPDATE translog_states SET root = ?, signature = ?", ("00" * 32, _sign(translog.state_bytes(bad))))
    conn.commit()
    conn.close()
    report = integrity.check(path, relay_public_keys=[_PUB])    # validly signed, but the root is not the log's
    assert report["log"]["status"] == "broken" and "root" in report["log"]["detail"]


def test_a_state_with_a_bad_signature_or_a_foreign_key_is_degraded(tmp_path, log_on):
    path = _db(tmp_path)
    _log(path)
    assert integrity.check(path, relay_public_keys=["ab" * 32])["log"]["status"] == "broken"   # not the relay's key
    _sql(path, "UPDATE translog_states SET signature = ?", "00" * 64)
    assert integrity.check(path, relay_public_keys=[_PUB])["log"]["status"] == "broken"
    assert integrity.check(path)["log"]["status"] == "broken"


def test_a_log_that_shrank_is_degraded(tmp_path, log_on):
    path = _db(tmp_path)
    _log(path, entries=6, sign_at=6)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # a later (by time) state at a smaller size, validly signed, in the same epoch
    later = {"type": translog.STATE_TYPE, "relay_public_key": _PUB, "size": 3,
             "root": translog.stored_root(conn, 3).hex(), "at": "2999-01-01T00:00:00+00:00", "epoch": 1}
    conn.execute("INSERT INTO translog_states (size, root, at, relay_public_key, signature, epoch) "
                 "VALUES (?,?,?,?,?,?)", (3, later["root"], later["at"], _PUB, _sign(translog.state_bytes(later)), 1))
    conn.commit()
    conn.close()
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["log"]["status"] == "broken" and "shrank" in report["log"]["detail"]


def test_log_statuses_off_and_empty(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    assert integrity.check(path)["log"]["status"] == "off"
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert integrity.check(path)["log"]["status"] == "empty"        # no log tables at all
    conn = sqlite3.connect(path)
    translog.init_schema(conn)
    conn.close()
    assert integrity.check(path)["log"]["status"] == "empty"        # tables, no entries
    assert integrity.check(path)["status"] == "ok"


def test_a_damaged_database_file_is_degraded(tmp_path, log_on):
    path = _full(tmp_path)
    raw = bytearray(path.read_bytes())
    for i in range(4096, min(len(raw), 4096 * 3)):
        raw[i] = 0xFF
    path.write_bytes(bytes(raw))
    for ext in ("-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)
    report = integrity.check(path, relay_public_keys=[_PUB])
    assert report["status"] == "degraded" and report["db"]["ok"] is False


def test_a_spent_budget_leaves_the_rest_unchecked_and_says_so(tmp_path, log_on):
    path = _full(tmp_path)
    report = integrity.check(path, relay_public_keys=[_PUB], budget_seconds=0)
    assert report["partial"] is True
    assert report["rooms"]["checked"] == 0 and report["dms"]["checked"] == 0
    assert report["status"] == "ok"


def test_the_public_summary_names_public_rooms_only(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE room_messages SET content = 'x1' WHERE message_id = 'pub-1'")
    _sql(path, "UPDATE room_messages SET content = 'y1' WHERE message_id = 'priv-1'")
    _sql(path, "UPDATE dm_messages SET content = 'z1' WHERE message_id = 'dm1-1'")
    s = integrity.public_summary(integrity.check(path, relay_public_keys=[_PUB]))
    assert s["status"] == "degraded" and s["rooms_broken"] == 2 and s["dms_broken"] == 1
    assert s["public_rooms"] == ["pub"] and s["db"] == "ok" and s["log"] == "ok"
    text = str(s)
    assert "priv" not in text and "dm1" not in text
    assert integrity.public_summary(None)["status"] == "unknown"


def test_health_shows_the_summary_and_no_private_id(tmp_path, log_on):
    path = _full(tmp_path)
    _sql(path, "UPDATE room_messages SET content = 'x1' WHERE message_id = 'pub-1'")
    _sql(path, "UPDATE room_messages SET content = 'y1' WHERE message_id = 'priv-1'")
    _sql(path, "UPDATE dm_messages SET content = 'z1' WHERE message_id = 'dm1-1'")
    report = integrity.check(path, relay_public_keys=[_PUB])
    with patch.object(rs, "_INTEGRITY_REPORT", report):
        body = TestClient(rs.app, client=("127.0.0.1", 51216)).get("/health").json()
    assert body["integrity"]["status"] == "degraded"
    assert body["integrity"]["public_rooms"] == ["pub"]
    assert "priv" not in str(body["integrity"]) and "dm1" not in str(body["integrity"])


def test_health_is_unknown_before_the_first_run():
    with patch.object(rs, "_INTEGRITY_REPORT", None):
        body = TestClient(rs.app, client=("127.0.0.1", 51216)).get("/health").json()
    assert body["integrity"]["status"] == "unknown" and body["integrity"]["checked_at"] is None


def test_the_loop_runs_once_then_on_its_interval_and_survives_an_exception():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def go():
        with patch.object(rs, "_integrity_pass", flaky):
            task = asyncio.create_task(rs._integrity_loop(0.01, 0.01))
            for _ in range(200):
                if len(calls) >= 3:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    assert len(calls) >= 3          # survived the first raise and kept going


def test_lifespan_starts_the_task_and_interval_zero_starts_none(monkeypatch):
    seen = {}

    def fake_pass():
        seen["ran"] = True

    monkeypatch.setattr(rs, "_integrity_pass", fake_pass)
    monkeypatch.setattr(rs, "_INTEGRITY_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setenv("EMPORIA_INTEGRITY_INTERVAL_SECONDS", "0")
    with TestClient(rs.app):
        assert rs._INTEGRITY_TASK is None
        time.sleep(0.1)
    assert "ran" not in seen
    monkeypatch.setenv("EMPORIA_INTEGRITY_INTERVAL_SECONDS", "3600")
    with TestClient(rs.app):
        assert rs._INTEGRITY_TASK is not None
        for _ in range(100):
            if seen.get("ran"):
                break
            time.sleep(0.02)
    assert seen.get("ran") is True
    assert rs._INTEGRITY_TASK is None


def test_the_relay_checks_its_own_database_and_keeps_the_report():
    report = rs.run_integrity_check()
    assert report["status"] in ("ok", "degraded") and rs._INTEGRITY_REPORT is report
    body = TestClient(rs.app, client=("127.0.0.1", 51216)).get("/health").json()
    assert body["integrity"]["checked_at"] == report["checked_at"]


def test_the_watcher_calls_a_degraded_check_attention():
    from emporia import relay_watch
    summary = {"server_errors": 0, "payments": {"failed": 0}, "disk_events": {},
               "window": {"requested_minutes": 60, "first": None}}
    ok = {"ok": True, "integrity": {"status": "ok"}}
    assert relay_watch.verdict(summary, ok) == ("all normal", False)
    bad = {"ok": True, "integrity": {"status": "degraded", "rooms_broken": 2, "dms_broken": 1, "log": "broken",
                                     "db": "ok"}}
    line, attention = relay_watch.verdict(summary, bad)
    assert attention and "2 room chain(s) broken" in line and "transparency log broken" in line
    assert relay_watch.verdict(summary, {"ok": True}) == ("all normal", False)     # an older relay: no field


def test_a_stop_request_ends_the_pass_at_its_next_check(tmp_path, log_on, monkeypatch):
    path = _full(tmp_path)
    real_check_file = integrity._check_file

    def file_then_shutdown(*a, **kw):      # the relay shuts down while the pass is in its first section
        out = real_check_file(*a, **kw)
        integrity.request_stop()
        return out

    monkeypatch.setattr(integrity, "_check_file", file_then_shutdown)
    report = integrity.check(path, relay_public_keys=[_PUB], budget_seconds=600)
    assert report["partial"] is True
    assert report["rooms"]["checked"] == 0 and report["dms"]["checked"] == 0
    monkeypatch.undo()
    # the stop belongs to the pass it ended: a pass begun later (the next relay in this process) runs in full
    assert integrity.check(path, relay_public_keys=[_PUB], budget_seconds=600)["partial"] is False


def test_a_relay_shutdown_does_not_stop_later_passes(tmp_path, log_on):
    path = _full(tmp_path)
    integrity.request_stop()               # what every relay's shutdown does, with no pass running
    report = integrity.check(path, relay_public_keys=[_PUB], budget_seconds=600)
    assert report["partial"] is False and report["rooms"]["checked"] > 0
