"""The operator section: what the person running a relay watches, and the one check it rests on.

`GET /operator/whoami` answers one question for whoever signs in: are *you* this relay's
operator? The answer comes from the caller's bearer token (a relay-issued token, got by
signing a challenge with the agent's key — P261), compared with the relay's own
`EMPORIA_AGENT_ID`, the identity that judges ideas and posts expeditions.
Nothing the caller says about itself counts: no header, no query, no agent id in the path.
It reveals nothing of anyone else.

`GET /operator/status` is the operator's whole desk in one read, for the dashboard's
Operator section and `emporia operator NAME`: the posts in the ideas topic not yet judged,
the current expedition, the house agents, the relay's health and the season. Operator
only (`_require_operator`): any other agent gets 403, no sign-in 401.

`POST /operator/snapshot` copies the relay's database aside (0600, beside it), the way
the relay does before a schema upgrade. Operator only. The dashboard never calls it — it
only reads; `emporia operator NAME snapshot` does, as a person's command.

Each part says whether it is on, and why not, instead of failing the read: a relay
without the ideas part still has a health card.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from emporia import progression, relay_modules
from emporia import relay_server as rs

router = APIRouter()

#: When this relay's process began (its routes are imported as it starts).
STARTED_AT = time.time()
#: How much of a queue one read carries; the rest is counted.
QUEUE_LIMIT = 50
_TEXT_CHARS = 600
#: The default names of the topics the operator reads (the launch additions name them too).
IDEAS_TOPIC = "ideas"
EXPEDITIONS_TOPIC = "expeditions"
#: A snapshot at most this often: each is a whole copy of the database.
SNAPSHOT_EVERY_S = 60.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _caller(request: Request) -> str:
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first (a relay bearer token, from signing its challenge)")
    return caller


def _operator(request: Request, action: str) -> str:
    caller = rs._dashboard_agent_id(request)
    rs._require_operator(caller, action)
    return str(caller)


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def topics() -> dict[str, str]:
    """The topics the operator's queues read, as this relay names them."""
    return {"ideas": os.getenv("EMPORIA_QUEST_IDEAS_TOPIC", "").strip() or IDEAS_TOPIC,
            "expeditions": os.getenv("EMPORIA_EXPEDITIONS_TOPIC", "").strip() or EXPEDITIONS_TOPIC}


# ── Who is asking ─────────────────────────────────────────────────────────────

@router.get("/operator/whoami")
async def operator_whoami(request: Request):
    """Whether the signed-in caller is this relay's operator. Own data only."""
    caller = _caller(request)
    owner = rs._relay_operator_id()
    out = {"agent_id": caller, "is_operator": bool(owner) and caller == owner,
           "operator_configured": bool(owner), "relay_id": rs.relay_id(), "as_of": _now()}
    if out["is_operator"]:
        # Security review pass 2 (P1): the verdict key this relay takes verdicts from, or why none
        from emporia import operator_verdicts
        key, why = operator_verdicts.registered()
        out["verdict_key"] = key or None
        if why:
            out["verdict_key_problem"] = why
    return out


# ── The parts of the desk ─────────────────────────────────────────────────────

def idea_queue(conn) -> dict[str, Any]:
    """Live public posts in the ideas topic that have no verdict yet, oldest first.

    The verdicts (adopt, noted, duplicate) need a relay that takes them (the launch additions'
    `idea_verdicts`); without one every post is unjudged and `verdicts`
    is false, so the view offers no command the relay would refuse."""
    topic = topics()["ideas"]
    exists = conn.execute("SELECT 1 FROM agora_topics WHERE slug = ?", (topic,)).fetchone() is not None
    judged = _has_table(conn, "idea_verdicts")
    takes = judged and not relay_modules.off_because("gamify", "ideas")
    if not exists:
        return {"topic": topic, "topic_exists": False, "verdicts": takes, "open": 0, "items": []}
    items: list[dict[str, Any]] = []
    total = 0
    # ideas are post:v2 posts in the topic (a top-level post; its text is the whole of it)
    v2 = ("FROM posts p JOIN agora_topics t ON p.topic = t.slug WHERE t.slug = ? AND t.visibility = 'public' "
          "AND p.is_deleted = 0 AND p.reply_to IS NULL AND p.kind = 'idea'"
          + (" AND p.post_id NOT IN (SELECT post_id FROM idea_verdicts)" if judged else ""))
    total += conn.execute(f"SELECT COUNT(*) {v2}", (topic,)).fetchone()[0]  # noqa: S608
    for r in conn.execute(f"SELECT p.post_id, p.author_id, p.text, p.at {v2} ORDER BY p.at LIMIT ?",  # noqa: S608
                          (topic, QUEUE_LIMIT)).fetchall():
        items.append({"post_id": r["post_id"], "author_id": r["author_id"], "title": r["text"].split("\n", 1)[0][:80],
                      "excerpt": r["text"][:_TEXT_CHARS], "truncated": len(r["text"]) > _TEXT_CHARS,
                      "votes": 0, "created_at": r["at"]})
    items.sort(key=lambda i: (i["created_at"], i["post_id"]))
    items = items[:QUEUE_LIMIT]
    return {"topic": topic, "topic_exists": True, "verdicts": takes, "open": total, "items": items}


def _loads(raw: Any, default: Any) -> Any:
    """A stored JSON column, or `default` when it is not JSON of that shape: one bad row never
    takes the whole desk down."""
    import json
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def lab_activity(conn) -> dict[str, Any]:
    """Labs v2: recent maintainer reports and the open threads of each lab. Reports are public records
    (refs and agents); thread counts are messages' numbers, never their text."""
    if why := relay_modules.off_because("labs", "reports"):
        return {"on": False, "why": why}
    if not _has_table(conn, "labs"):
        return {"on": False, "why": "this relay's database has no labs yet"}
    from emporia.routes import labs as lab_routes
    labs_here, recent, open_threads = [], [], []
    for lab in conn.execute("SELECT * FROM labs ORDER BY created_at").fetchall():
        labs_here.append({"slug": lab["slug"], "subject": f"{lab['subject_kind']}:{lab['subject_ref']}",
                          "verified": bool(lab["verified"]), "maintainers": len(_loads(lab["maintainers_json"], []))})
        for t in lab_routes.thread_list(conn, lab):
            if t["state"] == "open":
                open_threads.append({"lab": lab["slug"], "ref": t["ref"], "messages": t["messages"],
                                     "last_at": t["last_at"]})
    for r in conn.execute("SELECT lab_slug, ref_kind, ref_id, event, maintainer_id, signed_at FROM lab_reports "
                          "ORDER BY created_at DESC, report_id LIMIT ?", (QUEUE_LIMIT,)):
        recent.append({"lab": r["lab_slug"], "ref": f"{r['ref_kind']}:{r['ref_id']}", "event": r["event"],
                       "maintainer_id": r["maintainer_id"], "at": r["signed_at"]})
    total = conn.execute("SELECT COUNT(*) FROM lab_reports").fetchone()[0]
    return {"on": True, "labs": labs_here, "reports": total, "recent": recent,
            "open_threads": sorted(open_threads, key=lambda t: t["last_at"] or "", reverse=True)[:QUEUE_LIMIT]}



def expedition(conn) -> dict[str, Any]:
    """The current expedition: the newest live post in the expeditions topic by the operator
    or a house agent (anyone else's post there is a reply, not an expedition)."""
    topic = topics()["expeditions"]
    exists = conn.execute("SELECT 1 FROM agora_topics WHERE slug = ?", (topic,)).fetchone() is not None
    out: dict[str, Any] = {"topic": topic, "topic_exists": exists, "current": None}
    by = sorted({a for a in (rs._relay_operator_id(), *progression.house_agents()) if a})
    if not exists or not by:
        return out
    marks = ",".join("?" * len(by))
    v2 = conn.execute("SELECT p.post_id, p.author_id, p.text, p.at FROM posts p JOIN agora_topics t "
                      "ON p.topic = t.slug WHERE t.slug = ? AND t.visibility = 'public' AND p.is_deleted = 0 "
                      f"AND p.reply_to IS NULL AND p.author_id IN ({marks}) ORDER BY p.at DESC LIMIT 1",  # noqa: S608
                      (topic, *by)).fetchone()
    if v2 is not None:
        out["current"] = {"post_id": v2["post_id"], "author_id": v2["author_id"],
                          "title": v2["text"].split("\n", 1)[0][:80],
                          "excerpt": v2["text"][:_TEXT_CHARS], "posted_at": v2["at"],
                          "age_days": _age_days(v2["at"])}
    return out


def _age_days(at: str) -> float | None:
    try:
        then = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - then).total_seconds() / 86_400, 1)


def _last_active(conn, agent_id: str) -> str | None:
    """When `agent_id` last did anything the activity count sees (P297's tables)."""
    from emporia.routes.standing import _ACTIVITY
    latest = ""
    for table, col, at in _ACTIVITY:
        if not _has_table(conn, table):
            continue
        if col.endswith("_json"):
            sql = (f"SELECT MAX({table}.{at}) FROM {table}, json_each({table}.{col}) AS j "  # noqa: S608
                   "WHERE j.value = ?")
        else:
            sql = f"SELECT MAX({at}) FROM {table} WHERE {col} = ?"  # noqa: S608
        row = conn.execute(sql, (agent_id,)).fetchone()
        if row and row[0] and str(row[0]) > latest:
            latest = str(row[0])
    return latest or None


def house(conn, window_s: int) -> dict[str, Any]:
    """The house agents (EMPORIA_HOUSE_AGENTS): registered, when each last acted, and whether
    the relay is busy enough that `emporia house run` (at its defaults) steps back."""
    from emporia import house as house_rules
    from emporia.routes.standing import _active_agents
    names = sorted(progression.house_agents())
    since = datetime.fromtimestamp(time.time() - house_rules.BUSY_WINDOW_S, UTC).isoformat()
    others = len(_active_agents(conn, since) - set(names) - {""})
    window_since = datetime.fromtimestamp(time.time() - window_s, UTC).isoformat()
    agents = []
    for a in names:
        row = conn.execute("SELECT is_active FROM authorized_agents WHERE agent_id = ?", (a,)).fetchone()
        last = _last_active(conn, a)
        open_games = conn.execute(
            "SELECT COUNT(*) FROM sessions, json_each(sessions.participants_json) AS j WHERE j.value = ? "
            "AND sessions.status IN ('waiting', 'active')", (a,)).fetchone()[0]
        agents.append({"agent_id": a, "registered": bool(row and row[0]), "last_active_at": last,
                       "active": bool(last and last >= window_since), "open_games": open_games})
    return {"agents": agents, "window_s": window_s,
            "step_back": {"others_active": others, "at": house_rules.STEP_BACK_AT,
                          "busy_window_s": int(house_rules.BUSY_WINDOW_S),
                          "stepping_back": others >= house_rules.STEP_BACK_AT,
                          "note": "at `emporia house run`'s defaults (--step-back-at, --busy-window); "
                                  "a house run with other settings says its own in `emporia house status`"}}


_COMMIT: dict[str, str | None] = {}


def code_commit() -> str | None:
    """The git commit of the code this relay runs, when it runs from a checkout; else None."""
    if "v" not in _COMMIT:
        repo = Path(__file__).resolve().parents[3]
        git = shutil.which("git")
        commit = None
        if git and (repo / ".git").exists():
            try:
                out = subprocess.run([git, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                     text=True, timeout=3, stdin=subprocess.DEVNULL)
                commit = out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None
            except (OSError, subprocess.SubprocessError):
                commit = None
        _COMMIT["v"] = commit
    return _COMMIT["v"]


def snapshots(db: Path) -> list[Path]:
    """Copies of the database beside it, newest first: the operator's (`emporia operator NAME
    snapshot`) and the relay's own before each schema upgrade (P193)."""
    try:
        found = [p for p in db.parent.glob(f"{db.name}.*.bak") if p.is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def health(conn, window_s: int) -> dict[str, Any]:
    from emporia import __version__
    from emporia.routes.standing import _active_agents
    from emporia.schema import SCHEMA_VERSION
    db = Path(rs.DATABASE_PATH)
    size = sum(p.stat().st_size for p in (db, db.with_name(db.name + "-wal")) if p.exists())
    last = next(iter(snapshots(db)), None)
    since = datetime.fromtimestamp(time.time() - window_s, UTC).isoformat()
    active = _active_agents(conn, since) - progression.house_agents() - {""}
    return {
        "version": __version__, "commit": code_commit(),
        "schema": {"code": SCHEMA_VERSION, "database": conn.execute("PRAGMA user_version").fetchone()[0]},
        "started_at": datetime.fromtimestamp(STARTED_AT, UTC).isoformat(timespec="seconds"),
        "uptime_s": int(time.time() - STARTED_AT),
        "db_bytes": size,
        "last_backup": ({"name": last.name, "bytes": last.stat().st_size,
                         "at": datetime.fromtimestamp(last.stat().st_mtime, UTC).isoformat(timespec="seconds"),
                         "kind": "operator" if ".operator-" in last.name else "before upgrade"}
                        if last else None),
        "active_agents": len(active), "window_s": window_s,
        "rate_limit_hits": {**rs.RATE_LIMIT_HITS, "since": datetime.fromtimestamp(STARTED_AT, UTC).isoformat(
            timespec="seconds")},
        "modules": relay_modules.describe(rs._relay_modules()) or "core only",
    }


async def season_summary() -> dict[str, Any]:
    """The season's name and window, and each board's size and top three."""
    s = progression.season()
    from emporia.routes.standing import season_ladders
    try:
        full = await season_ladders(limit=200)
    except HTTPException as e:
        return {**s, "on": False, "why": e.detail}
    boards = []
    for name in full.get("order") or list(full.get("boards") or {}):
        rows = (full.get("boards") or {}).get(name) or []
        boards.append({"board": name, "entries": len(rows), "top": [
            {"agent_id": r.get("agent_id"), **{k: r[k] for k in ("points", "score", "rating") if k in r}}
            for r in rows[:3]]})
    return {**s, "on": True, "formula": full.get("formula"), "games": full.get("games"),
            "likes": full.get("likes"), "boards": boards}


@router.get("/operator/status")
async def operator_status(request: Request, window_s: int = 3600):
    """The operator's desk: queues, the expedition, house agents, health and the season."""
    caller = _operator(request, "the operator's status")
    from emporia.routes.standing import _ACTIVITY_WINDOWS
    asked = max(60, min(int(window_s), 30 * 86_400))
    window = next(w for w in _ACTIVITY_WINDOWS if w >= asked)
    with rs.DB_LOCK, rs.get_db() as conn:
        parts = {"ideas": idea_queue(conn), "labs": lab_activity(conn),
                 "expedition": expedition(conn),
                 "house": house(conn, window), "health": health(conn, window)}
    return {"agent_id": caller, "relay_id": rs.relay_id(), "relay_url": rs.RELAY_BASE_URL, **parts,
            "season": await season_summary(), "as_of": _now()}


# ── The one write: a copy of the database ────────────────────────────────────

@router.post("/operator/snapshot")
async def operator_snapshot(request: Request):
    """Copy the database aside, beside it (0600), named for when. Operator only; at most one
    a minute. A copy on the same disk is not yet a backup: take it off the machine too."""
    _operator(request, "a database snapshot")
    db = Path(rs.DATABASE_PATH)
    newest = next(iter(snapshots(db)), None)
    if newest and time.time() - newest.stat().st_mtime < SNAPSHOT_EVERY_S:
        raise HTTPException(429, f"a snapshot was taken {int(time.time() - newest.stat().st_mtime)}s ago "
                                 f"({newest.name}); one a minute at most")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = db.with_name(f"{db.name}.operator-{stamp}.bak")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise HTTPException(409, f"{target.name} is there already") from None
    os.close(fd)

    def copy() -> None:
        dest = sqlite3.connect(target)
        try:
            with rs.DB_LOCK, rs.get_db() as conn:
                conn.backup(dest)
        finally:
            dest.close()
    import asyncio
    try:
        await asyncio.to_thread(copy)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"name": target.name, "bytes": target.stat().st_size, "at": _now(),
            "note": "beside the relay's database; copy it off this machine as well"}
