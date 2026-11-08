"""`GET /relay/activity` (P297), counted in SQL (security follow-up to the 11-05 audit).

It read every row of six tables in the window — up to 30 days — into Python on each
call, on the event loop, for anyone to ask: the cost of a call grew with the relay's
history. It now asks SQLite for the distinct agents per table (`json_each` for a game's
players) and keeps the answer for 30 seconds; the count is the same."""
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from emporia import relay_server as rs   # first: the routes import it
from emporia.routes import standing


def _scan(conn, since: str) -> set[str]:
    """The Python scan the route ran before, kept here as the reference."""
    agents: set[str] = set()
    for table, col, at in standing._ACTIVITY:
        if not standing._has_table(conn, table):
            continue
        for r in conn.execute(f"SELECT {col} FROM {table} WHERE {at} >= ?", (since,)):  # noqa: S608
            v = r[0] or ""
            agents |= set(json.loads(v)) if col.endswith("_json") else {v}
    return agents


def _relay_db(missing: tuple[str, ...] = ()) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    now = datetime.now(UTC)
    for table, col, at in standing._ACTIVITY:
        if table in missing:
            continue
        conn.execute(f"CREATE TABLE {table} ({col} TEXT, {at} TEXT)")
        for i in range(60):
            when = (now - timedelta(minutes=37 * i)).isoformat()
            who = (json.dumps([f"p{i % 7}", f"p{(i * 3) % 11}", "shared"]) if col.endswith("_json")
                   else ("" if i % 13 == 0 else f"{table[:3]}{i % 9}"))
            conn.execute(f"INSERT INTO {table} VALUES (?, ?)", (who, when))  # noqa: S608
    return conn


def test_the_sql_count_is_the_scans_count():
    for missing in ((), ("room_messages", "reactions")):
        conn = _relay_db(missing)
        for minutes in (1, 30, 61, 600, 3000, 60 * 24 * 30):
            since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
            assert standing._active_agents(conn, since) == _scan(conn, since), (missing, minutes)


def test_the_count_is_kept_for_thirty_seconds(monkeypatch):
    calls = []
    real = standing._active_agents
    monkeypatch.setattr(standing, "_active_agents", lambda conn, since: calls.append(since) or real(conn, since))
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(standing, "_monotonic", lambda: clock["t"])
    standing._ACTIVITY_CACHE.clear()
    client = TestClient(rs.app)
    first = client.get("/relay/activity", params={"window_s": 3600}).json()["active_agents"]
    clock["t"] += 29
    assert client.get("/relay/activity", params={"window_s": 3600}).json()["active_agents"] == first
    assert len(calls) == 1, "asked again within 30 s"
    client.get("/relay/activity", params={"window_s": 7200})
    assert len(calls) == 2, "another window is its own answer"
    clock["t"] += 2
    client.get("/relay/activity", params={"window_s": 3600})
    assert len(calls) == 3, "after 30 s it is counted again"
    for w in range(60, 60 + 3 * standing._ACTIVITY_CACHE_MAX):   # bounded, whatever windows are asked
        client.get("/relay/activity", params={"window_s": w})
    assert len(standing._ACTIVITY_CACHE) <= standing._ACTIVITY_CACHE_MAX


def test_varying_the_window_does_not_get_past_the_cache(monkeypatch):
    """Review follow-up L5: the cache was keyed on the exact `window_s`, so asking for
    3601, 3602, … seconds missed it every time. Windows are rounded up to fixed steps."""
    calls = []
    real = standing._active_agents
    monkeypatch.setattr(standing, "_active_agents", lambda conn, since: calls.append(since) or real(conn, since))
    monkeypatch.setattr(standing, "_monotonic", lambda: 2_000_000.0)
    standing._ACTIVITY_CACHE.clear()
    client = TestClient(rs.app)
    answers = {client.get("/relay/activity", params={"window_s": w}).json()["window_s"] for w in range(3601, 3700)}
    assert answers == {7200} and len(calls) == 1, (answers, len(calls))
    assert client.get("/relay/activity", params={"window_s": 10**9}).json()["window_s"] == 30 * 86_400
    assert len(standing._ACTIVITY_WINDOWS) <= standing._ACTIVITY_CACHE_MAX, "every window fits in the cache"
