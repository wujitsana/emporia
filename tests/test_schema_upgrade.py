"""`emporia relay upgrade`: a live database (the schema-52 relay) carried to this schema, additively, backup first.

The fixture `tests/fixtures/schema_52.sql` is the live relay's SCHEMA_SQL (commit 82ffb5ef), verbatim."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from emporia import integrity, relay_db
from emporia import schema_upgrade as U
from emporia.schema import SCHEMA_VERSION

FIXTURE = Path(__file__).parent / "fixtures" / "schema_52.sql"
REPO = Path(__file__).resolve().parents[1]


def _fill(conn, table, **given):
    """Insert one row, giving every NOT NULL column without a default a plain value."""
    cols = {}
    for _cid, name, typ, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        if name in given:
            cols[name] = given[name]
        elif notnull and default is None:
            cols[name] = 0 if "INT" in typ.upper() else "x"
    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))


def make_old(path: Path, version: int = 52) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(FIXTURE.read_text())
    for who in ("alice", "bob"):
        _fill(conn, "authorized_agents", agent_id=who, public_key_hex=who * 8, display_name=who.title(),
              registered_at="2026-11-01T00:00:00+00:00", stable_id=who * 4)
    _fill(conn, "posts", post_id="p1", author_id="alice", author_stable_id="aaaa", text="hello feed",
          kind="note", at="2026-11-02T00:00:00+00:00", recorded_at="2026-11-02T00:00:00+00:00")
    _fill(conn, "sessions", session_id="s1", module_type="chess", config_json="{}", payment_rules_json="{}",
          state_json="{}", current_agent="alice", participants_json='["alice","bob"]',
          created_at="2026-11-03T00:00:00+00:00", updated_at="2026-11-03T00:00:00+00:00")
    _fill(conn, "participants", session_id="s1", agent_id="alice", joined_at="2026-11-03T00:00:00+00:00")
    _fill(conn, "rooms", room_id="r1", name="Lobby", creator_id="alice", members_json='["alice","bob"]',
          created_at="2026-11-03T00:00:00+00:00", updated_at="2026-11-03T00:00:00+00:00")
    _fill(conn, "reactions", agent_id="bob", target="post:p1", kind="post", target_id="p1", value="like",
          to_agent="alice", at="2026-11-04T00:00:00+00:00")
    # the welcome quests are derived from these, there is no quest table at 52: a pledge stands in
    _fill(conn, "pledges", pledge_id="q1", maker_id="alice", to_agent="bob", kind="x", session_id="s1",
          made_at="2026-11-05T00:00:00+00:00", recorded_at="2026-11-05T00:00:00+00:00")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()
    return path


def rows(path, table):
    conn = sqlite3.connect(path)
    try:
        return sorted(map(tuple, conn.execute(f"SELECT * FROM {table} ORDER BY 1")))
    finally:
        conn.close()


@pytest.fixture
def old(tmp_path):
    return make_old(tmp_path / "relay" / "emporia.sqlite3") if (tmp_path / "relay").mkdir() is None else None


def test_the_plan_is_additive_and_names_the_changes(old):
    plan = U.dry_run(old)
    text = "\n".join(plan.lines())
    assert plan.from_version == 52 and plan.to_version == SCHEMA_VERSION and not plan.refusals
    assert "add column authorized_agents.accepts_networks_json" in text
    assert "add column authorized_agents.home_json" in text and "add column authorized_agents.home_check_json" in text
    assert "create table tesseract_results" in text and "create index idx_agent_chain_links_agent" in text
    # schema 61 (shared topics): two columns with defaults, one nullable column, two tables, one index
    for what in ("add column authorized_agents.remote_mentions", "add column agora_topics.hosted_by",
                 "add column posts.signed_topic", "create table carried_topics", "create table remote_mention_log",
                 "create index idx_remote_mention_log_to"):
        assert what in text, what


def test_dry_run_changes_nothing(old, capsys):
    before = old.read_bytes()
    assert U.cli(old, dry=True, yes=False, tty=False) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "52" in out
    assert old.read_bytes() == before
    assert [p.name for p in old.parent.iterdir() if "pre-upgrade" in p.name] == []


def test_upgrade_keeps_the_data_backs_up_and_stamps(old):
    tables = ("authorized_agents", "posts", "sessions", "participants", "rooms", "reactions", "pledges")
    before = {t: rows(old, t) for t in tables}
    result = U.upgrade(old)
    assert result.applied > 0 and result.backup.exists() and result.backup.parent == old.parent
    assert result.backup.name.startswith("emporia.pre-upgrade-s52-") and result.backup.name.endswith(".sqlite3")
    b = sqlite3.connect(result.backup)
    assert b.execute("PRAGMA user_version").fetchone()[0] == 52 and b.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    b.close()
    conn = sqlite3.connect(old)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT accepts_networks_json FROM authorized_agents").fetchall() == [("",), ("",)]
    # schema 59: an agent from before homes has no declared home, and is treated as a member (home here)
    assert conn.execute("SELECT home_json, home_check_json FROM authorized_agents").fetchall() == [("", ""), ("", "")]
    from emporia import visitors
    assert visitors.kind_of("") == "here" and visitors.view("", "")["standing"] == "member"
    # schema 61: no agent has set a mention policy (the relay reads "" as the default, topics), no topic is a carried
    # mirror, no post has another signed form of its topic, and the new tables exist and are empty
    assert conn.execute("SELECT remote_mentions FROM authorized_agents").fetchall() == [("",), ("",)]
    assert conn.execute("SELECT remote_mentions FROM authorized_agents").fetchone()[0] == ""
    from emporia import remote_mentions
    assert remote_mentions.clean_mode("") == remote_mentions.DEFAULT_MODE == "topics"
    assert conn.execute("SELECT signed_topic FROM posts").fetchall() == [(None,)]
    assert conn.execute("SELECT COUNT(*) FROM carried_topics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM remote_mention_log").fetchone()[0] == 0
    conn.close()
    for t in tables:   # old columns intact; the one new column is on agents only
        got = rows(old, t)
        width = len(before[t][0])
        assert [r[:width] for r in got] == before[t]


def test_a_second_upgrade_is_a_noop(old, capsys):
    U.upgrade(old)
    again = U.upgrade(old)
    assert again.backup is None and again.applied == 0 and again.plan.noop
    assert len([p for p in old.parent.iterdir() if "pre-upgrade" in p.name]) == 1
    assert U.cli(old, dry=False, yes=True, tty=False) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_backup_dir_setting_is_used(old, tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_DB_BACKUP_DIR", str(tmp_path / "bk"))
    result = U.upgrade(old)
    assert result.backup.parent == tmp_path / "bk"


def test_a_running_relay_is_refused_before_anything_is_touched(old):
    lock = old.with_name(old.name + ".lock")
    import fcntl
    held = open(lock, "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        before = old.read_bytes()
        with pytest.raises(U.UpgradeError, match="relay is running"):
            U.upgrade(old)
        assert old.read_bytes() == before
        assert [p for p in old.parent.iterdir() if "pre-upgrade" in p.name] == []
        assert relay_db.running(old)
    finally:
        held.close()


def test_a_locked_database_is_refused(old):
    other = sqlite3.connect(old, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(U.UpgradeError, match="locked"):
            U.upgrade(old)
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_a_yes_is_needed_without_the_flag(old, capsys):
    assert U.cli(old, dry=False, yes=False, tty=False) == 1
    assert "pass --yes" in capsys.readouterr().out
    assert sqlite3.connect(old).execute("PRAGMA user_version").fetchone()[0] == 52
    answers = []
    assert U.cli(old, dry=False, yes=False, tty=True, ask=lambda q, d: answers.append(q) or True) == 0
    assert answers and sqlite3.connect(old).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# ─── refusals: one per kind ──────────────────────────────────────────────────

def _refused(old, sql, match):
    conn = sqlite3.connect(old)
    conn.executescript(sql)
    conn.commit()
    conn.close()
    before = old.read_bytes()
    plan = U.dry_run(old)
    assert any(match in r for r in plan.refusals), plan.refusals
    with pytest.raises(U.UpgradeError, match=match):
        U.upgrade(old)
    assert old.read_bytes() == before, "a refused database is not touched"
    assert [p for p in old.parent.iterdir() if "pre-upgrade" in p.name] == []


def test_refuses_a_removed_or_renamed_column(old):
    _refused(old, "ALTER TABLE posts RENAME COLUMN label TO tag;", "posts.tag is in the database but not")


def test_refuses_an_unknown_table(old):
    _refused(old, "CREATE TABLE old_thing (a TEXT);", "table old_thing is in the database but not")


def test_refuses_a_changed_type(old):
    _refused(old, "ALTER TABLE posts ADD COLUMN x INTEGER; ALTER TABLE posts DROP COLUMN label;"
                  "ALTER TABLE posts ADD COLUMN label INTEGER NOT NULL DEFAULT ''; ", "posts.label changed its type")


def test_refuses_a_changed_not_null_and_default(old):
    _refused(old, "ALTER TABLE rooms DROP COLUMN description; ALTER TABLE rooms ADD COLUMN description TEXT;",
             "rooms.description changed its NOT NULL")
    conn = sqlite3.connect(old)
    conn.execute("ALTER TABLE rooms DROP COLUMN currency")
    conn.execute("ALTER TABLE rooms ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'")
    conn.commit()
    conn.close()
    assert any("rooms.currency changed its default" in r for r in U.dry_run(old).refusals)


def test_refuses_a_changed_primary_key(tmp_path):
    path = tmp_path / "pk.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE.read_text().replace("PRIMARY KEY (agent_id, target, value)", "PRIMARY KEY (agent_id, target)"))
    conn.execute("PRAGMA user_version = 52")
    conn.commit()
    conn.close()
    plan = U.dry_run(path)
    assert any("reactions.value changed its primary key" in r for r in plan.refusals), plan.refusals


def test_refuses_a_newer_file(old):
    conn = sqlite3.connect(old)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(U.UpgradeError, match="newer relay"):
        U.upgrade(old)


def test_refuses_a_new_not_null_column_without_a_default(old, monkeypatch):
    import emporia.schema_upgrade as mod
    monkeypatch.setattr(mod, "SCHEMA_SQL", mod.SCHEMA_SQL.replace(
        "relay_secrets (", "relay_secrets (\n  extra TEXT NOT NULL,", 1))
    plan = U.dry_run(old)
    assert any("relay_secrets.extra is NOT NULL with no default" in r for r in plan.refusals)


def test_cli_exit_code_and_message_on_refusal(old, capsys):
    sqlite3.connect(old).executescript("CREATE TABLE old_thing (a TEXT);")
    assert U.cli(old, dry=False, yes=True, tty=False) == 1
    assert "Nothing was changed" in capsys.readouterr().out


# ─── the relay on an upgraded file ───────────────────────────────────────────

_START = r"""
import sys
from fastapi.testclient import TestClient
from emporia import relay_server as rs
with TestClient(rs.app, client=("127.0.0.1", 51999)) as c:
    h = c.get("/health"); f = c.get("/feed")
    st = rs.sign_log_state()      # what the relay's own timer does: fold the backlog in, sign a state
    print("LOG", st and st["size"])
    print("HEALTH", h.status_code, "FEED", f.status_code, len(f.json().get("posts", f.json().get("items", []))) if f.status_code == 200 else f.text[:200])
"""


def _run_relay(db: Path, home: Path, log: str = "on") -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EMPORIA_") and not k.startswith("HERMES_")}
    env.update(EMPORIA_NO_DOTENV="1", EMPORIA_DB_PATH=str(db), EMPORIA_KEYS_DIR=str(home / "keys"),
               EMPORIA_LOG_DIR=str(home / "logs"), EMPORIA_TRANSPARENCY_LOG=log, HOME=str(home),
               PYTHONPATH=str(REPO / "src"))
    r = subprocess.run([sys.executable, "-c", _START], env=env, capture_output=True, text=True, cwd=str(home), timeout=170)
    assert r.returncode == 0, r.stderr[-1500:]
    return r.stdout


def test_an_old_database_is_refused_by_the_relay_and_the_upgraded_one_starts(old, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, EMPORIA_NO_DOTENV="1", EMPORIA_DB_PATH=str(old), EMPORIA_KEYS_DIR=str(home / "keys"),
               EMPORIA_LOG_DIR=str(home / "logs"), HOME=str(home), PYTHONPATH=str(REPO / "src"))
    r = subprocess.run([sys.executable, "-c", _START], env=env, capture_output=True, text=True, cwd=str(home), timeout=170)
    assert r.returncode != 0 and "emporia relay upgrade" in r.stderr
    U.upgrade(old)
    out = _run_relay(old, home)
    assert "HEALTH 200 FEED 200" in out, out
    conn = sqlite3.connect(old)
    assert conn.execute("SELECT text FROM posts WHERE post_id='p1'").fetchone()[0] == "hello feed"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_the_upgraded_database_passes_the_integrity_pass_and_the_log_verifies(old, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    U.upgrade(old)
    _run_relay(old, home, log="on")       # first start folds the log in over the old rows and signs a state
    report = integrity.check(old)
    assert report["status"] == "ok", report
    assert report["db"]["ok"] and report["log"]["status"] == "ok" and report["log"]["size"] > 0, report["log"]
    # and a second start over the same file is quiet and still verifies
    _run_relay(old, home, log="on")
    assert integrity.check(old)["log"]["status"] == "ok"


def test_the_upgrade_does_not_touch_a_log_that_was_already_there(old, tmp_path):
    """An old file that already has the log's tables: they are left to the log, never flagged or changed."""
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE translog_extra (a TEXT)")
    conn.commit()
    conn.close()
    assert not U.dry_run(old).refusals
