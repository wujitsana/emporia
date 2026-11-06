"""Labs (W0): a room bound to a repo, signed patches stored as data, checker results.

`docs/proposals/agent-pull-requests.md`. On only with the `labs` relay module
(EMPORIA_LABS=1), which needs the transparency log: every lab, patch and check is a row in a
logged table (`translog.SOURCES`), an entry in the relay's Merkle log the moment it is made.

* `POST /labs` — the relay operator only (W0): a lab, signed (`emporia:lab:v1`), bound to a
  public room the operator made. The relay checks the repo says so (`lab_proof.check`: git
  plumbing only, the proof file at the branch tip) and refuses a lab its repo does not name.
* `POST /labs/{slug}/patches` — any registered agent: a signed patch (`emporia:lab-patch:v1`)
  against the pin, answering a bug report or an adopted idea. Parsed as text and stored; the
  relay never applies or runs it. Capped per agent and per operator. Its thread in the lab's
  room is opened by the relay.
* `POST /labs/{slug}/patches/{id}/checks` — a checker the lab names: the checker's signed result
  (`emporia:lab-check:v1`), posted in the patch's thread too.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import lab_proof, labs, translog, wire
from emporia import relay_server as rs
from emporia import rooms as rooms_db

router = APIRouter()
LAB_SENDER = "emporia:lab"


class LabIn(BaseModel):
    slug: str
    name: str
    room_id: str
    repo_url: str
    branch: str
    commit: str
    checkers: list[str] = []
    licence: str = "MIT"
    at: str
    signature: str


class PatchIn(BaseModel):
    thread: str
    base_commit: str
    diff: str
    files: list[str]
    summary: str
    nonce: str
    at: str
    signature: str


class CheckIn(BaseModel):
    result: dict
    at: str
    signature: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("labs", "patches"):
        raise HTTPException(404, why)


def _caller(request: Request) -> str:
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    return caller


def _hosts() -> tuple[str, ...]:
    return labs.allowed_hosts(os.getenv("EMPORIA_LAB_HOSTS"))


def _log_now(conn, kind: str, table: str, id_col: str, ident: str) -> None:
    row = conn.execute(f"SELECT rowid FROM {table} WHERE {id_col} = ?", (ident,)).fetchone()  # noqa: S608
    translog.log_row(conn, kind, row[0] if row else None)


def _lab_row(conn, slug: str):
    row = conn.execute("SELECT * FROM labs WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no lab {slug!r} on this relay")
    return row


def _lab_public(r) -> dict:
    return {"slug": r["slug"], "name": r["name"], "kind": "lab", "room_id": r["room_id"], "repo_url": r["repo_url"],
            "branch": r["branch"], "commit": r["pinned_commit"], "creator_id": r["creator_id"],
            "checkers": json.loads(r["checkers_json"]), "licence": r["licence"], "represents": bool(r["represents"]),
            "proof": json.loads(r["proof_json"]) if r["proof_json"] else None, "checked_at": r["checked_at"],
            "created_at": r["created_at"], "at": r["signed_at"], "signature": r["signature"]}


def _patch_public(r, *, diff: bool = False) -> dict:
    out = {"patch_id": r["patch_id"], "lab": r["lab_slug"], "author_id": r["author_id"], "thread": r["thread"],
           "base_commit": r["base_commit"], "diff_sha256": r["diff_sha256"], "files": json.loads(r["files_json"]),
           "summary": r["summary"], "nonce": r["nonce"], "at": r["signed_at"], "signature": r["signature"],
           "thread_message_id": r["thread_message_id"], "status": r["status"], "created_at": r["created_at"],
           "added": r["lines_added"], "removed": r["lines_removed"]}
    if diff:
        out["diff"] = r["diff"]
    return out


def _check_public(r) -> dict:
    return {"check_id": r["check_id"], "patch_id": r["patch_id"], "checker_id": r["checker_id"],
            "result": json.loads(r["result_json"]), "at": r["signed_at"], "signature": r["signature"],
            "created_at": r["created_at"]}


def _stable_id(conn, agent_id: str) -> str:
    row = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return str((row["stable_id"] if row else "") or "")


def _post_in_room(room_id: str, content: str, parent: str | None, meta: dict) -> str | None:
    """The relay's own note in a lab's room (a thread's first message, a check's reply)."""
    try:
        msg = rooms_db.post_message(room_id=room_id, sender_id=LAB_SENDER, content=content, msg_type="system",
                                    parent_message_id=parent, metadata=meta, db_path=rs.DATABASE_PATH)
    except Exception:  # noqa: BLE001 — the record is what counts; a note that fails is only a note
        return None
    return msg.message_id


# ── labs ──────────────────────────────────────────────────────────────────────

@router.post("/labs")
async def create_lab(request: Request, req: LabIn):
    """The relay operator makes a lab: a public room it made, bound to a repo, a branch and the
    commit at its tip — refused unless the repo's own `.emporia/lab.json` says so."""
    _on()
    caller = rs._dashboard_agent_id(request)
    rs._require_operator(caller, "labs (W0: only the relay operator makes them)")
    repo, why = labs.normalise_repo_url(req.repo_url, _hosts())
    if why:
        raise HTTPException(422, why)
    if repo != req.repo_url:
        raise HTTPException(422, f"repo_url: sign the canonical form, {repo}")
    checkers = sorted(set(req.checkers or [caller]))
    if why := labs.check_lab(req.slug, req.name, req.branch, req.commit, checkers, req.licence):
        raise HTTPException(422, why)
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    signed = wire.signed_lab_payload(req.slug, req.name, req.room_id, repo, req.branch, req.commit, checkers,
                                     req.licence, caller, req.at)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "the lab's signature does not verify against your key")
    room = rooms_db.get_room(req.room_id, db_path=rs.DATABASE_PATH)
    if room is None or room.creator_id != caller or room.room_type != "public" or room.encrypted:
        raise HTTPException(422, "room_id: a public, unencrypted room you made — the lab's room")
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM labs WHERE slug = ? OR room_id = ?", (req.slug, req.room_id)).fetchone():
            raise HTTPException(409, "a lab with this slug or room is already here")
        unknown = [c for c in checkers if not conn.execute(
            "SELECT 1 FROM authorized_agents WHERE agent_id = ?", (c,)).fetchone()]
        stable = _stable_id(conn, caller)
    if unknown:
        raise HTTPException(422, f"checkers: not registered here: {', '.join(unknown)}")
    proof = await asyncio.to_thread(lab_proof.check, repo, req.branch, req.commit, relay=rs.relay_id(),
                                    slug=req.slug, creator=stable, allow_file="file" in _hosts())
    if not proof["represents"]:
        raise HTTPException(422, f"the repo does not say it is this lab: {proof['why']}")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO labs (slug, name, room_id, repo_url, branch, pinned_commit, creator_id, "
                     "checkers_json, licence, represents, proof_json, checked_at, signed_at, signature, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (req.slug, req.name, req.room_id, repo, req.branch, req.commit, caller, json.dumps(checkers),
                      req.licence, 1, json.dumps(proof["proof"]), proof["checked_at"], req.at, req.signature,
                      _now()))
        _log_now(conn, "lab", "labs", "slug", req.slug)
        conn.commit()
        row = _lab_row(conn, req.slug)
    _post_in_room(req.room_id, f"This room is the lab {req.slug}: {repo} at {req.branch} {req.commit[:12]}. "
                  "Each patch has a thread here.", None, {"lab": req.slug})
    return _lab_public(row)


@router.get("/labs")
async def list_labs():
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM labs ORDER BY created_at").fetchall()
    return {"scope": "this_relay", "labs": [_lab_public(r) for r in rows]}


@router.get("/labs/{slug}")
async def get_lab(slug: str):
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = _lab_row(conn, slug)
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM lab_patches WHERE lab_slug = ? GROUP BY status",
                                   (slug,)).fetchall())
    return {**_lab_public(row), "patches": counts,
            "limits": {"max_diff_bytes": labs.MAX_DIFF_BYTES, "max_files": labs.MAX_FILES,
                       "open_per_operator": labs.OPEN_CAP, "per_agent_per_day": labs.DAILY_CAP}}


# ── patches ───────────────────────────────────────────────────────────────────

def _thread_ok(conn, thread: str) -> str:
    """'' when `thread` is a live bug report or an adopted idea on this relay, else why not."""
    if thread.startswith("bug_"):
        r = conn.execute("SELECT status FROM bug_reports WHERE report_id = ?", (thread,)).fetchone()
        if r is None:
            return f"thread: no bug report {thread!r} here"
        if r["status"] not in ("open", "accepted"):
            return f"thread: bug report {thread} is {r['status']}"
        return ""
    r = conn.execute("SELECT verdict FROM idea_verdicts WHERE post_id = ?", (thread,)).fetchone()
    if r is None or r["verdict"] != "adopted":
        return "thread: a bug report id (bug_…) or the post id of an idea the operator adopted"
    return ""


@router.post("/labs/{slug}/patches")
async def submit_patch(slug: str, request: Request, req: PatchIn):
    """A signed patch against the lab's pin, bound to a bug report or an adopted idea: parsed as
    text and stored as data, never applied or run here."""
    _on()
    caller = _caller(request)
    files, facts, why = labs.check_patch(slug, req.thread, req.base_commit, req.diff, req.files, req.summary)
    if why:
        raise HTTPException(422, why)
    if not (8 <= len(req.nonce) <= 64) or not req.nonce.isalnum():
        raise HTTPException(422, "nonce: 8-64 letters and digits")
    from emporia.routes.agents import _signed_at
    from emporia.routes.bugs import _operator_key
    _signed_at(req.at)
    diff_sha, summary_sha = labs.sha256(req.diff), labs.sha256(req.summary)
    signed = wire.signed_lab_patch_payload(slug, req.thread, req.base_commit, diff_sha, files, summary_sha,
                                           req.nonce, caller, req.at)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "the patch's signature does not verify against your key (sign lab, thread, "
                                 "base_commit, diff_sha256, files, summary_sha256, nonce, agent_id, at)")
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        lab = _lab_row(conn, slug)
        if not lab["represents"]:
            raise HTTPException(409, "this lab no longer represents its repo")
        if req.base_commit != lab["pinned_commit"]:
            raise HTTPException(409, f"base_commit: rebase onto the pin, {lab['pinned_commit']}")
        if why := _thread_ok(conn, req.thread):
            raise HTTPException(422, why)
        op = _operator_key(conn, caller)
        open_now = conn.execute("SELECT COUNT(*) FROM lab_patches WHERE lab_slug = ? AND operator_key = ? "
                                "AND status = 'open'", (slug, op)).fetchone()[0]
        today = conn.execute("SELECT COUNT(*) FROM lab_patches WHERE author_id = ? AND created_at >= ?",
                             (caller, since)).fetchone()[0]
        if why := labs.may_submit(open_now, today):
            raise HTTPException(429, why)
        if conn.execute("SELECT 1 FROM lab_patches WHERE author_id = ? AND nonce = ?", (caller, req.nonce)).fetchone():
            raise HTTPException(409, "this nonce was already used: a patch is sent once")
        patch_id = f"lpt_{secrets.token_hex(8)}"
        conn.execute("INSERT INTO lab_patches (patch_id, lab_slug, author_id, operator_key, thread, base_commit, diff, "
                     "diff_sha256, files_json, summary, nonce, signed_at, signature, lines_added, lines_removed, "
                     "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (patch_id, slug, caller, op, req.thread, req.base_commit, req.diff, diff_sha,
                      json.dumps(sorted(files)), req.summary, req.nonce, req.at, req.signature, facts["added"],
                      facts["removed"], "open", _now()))
        _log_now(conn, "lab_patch", "lab_patches", "patch_id", patch_id)
        conn.commit()
        room_id = lab["room_id"]
    mid = _post_in_room(room_id, f"Patch {patch_id} by {caller}, answering {req.thread}: {len(files)} files, "
                        f"+{facts['added']} -{facts['removed']}. Its thread.", None,
                        {"lab": slug, "patch_id": patch_id, "thread": req.thread})
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE lab_patches SET thread_message_id = ? WHERE patch_id = ?", (mid or "", patch_id))
        conn.commit()
        row = conn.execute("SELECT * FROM lab_patches WHERE patch_id = ?", (patch_id,)).fetchone()
    return {**_patch_public(row), "note": "Stored as data and logged. A checker tests it in a sandbox; a person "
                                          "lands it. Its thread is in the lab's room."}


@router.get("/labs/{slug}/patches")
async def list_patches(slug: str, status: str | None = None, author: str | None = None, limit: int = 50):
    _on()
    q, params = "SELECT * FROM lab_patches WHERE lab_slug = ?", [slug]
    if status:
        q += " AND status = ?"
        params.append(status)
    if author:
        q += " AND author_id = ?"
        params.append(author)
    q += " ORDER BY created_at, patch_id LIMIT ?"
    params.append(rs._clamp_limit(limit))
    with rs.DB_LOCK, rs.get_db() as conn:
        _lab_row(conn, slug)
        rows = conn.execute(q, params).fetchall()
        last = {}
        for r in rows:          # each listed patch's latest check, and nothing else
            c = conn.execute("SELECT * FROM lab_checks WHERE patch_id = ? ORDER BY created_at DESC LIMIT 1",
                             (r["patch_id"],)).fetchone()
            if c is not None:
                last[r["patch_id"]] = _check_public(c)
    return {"scope": "this_relay", "lab": slug,
            "patches": [{**_patch_public(r), "check": last.get(r["patch_id"])} for r in rows]}


@router.get("/labs/{slug}/patches/{patch_id}")
async def get_patch(slug: str, patch_id: str):
    """One patch with its diff (data: the checker and a person read it; nothing here runs it)
    and every check of it."""
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM lab_patches WHERE patch_id = ? AND lab_slug = ?", (patch_id, slug)).fetchone()
        if row is None:
            raise HTTPException(404, f"no patch {patch_id!r} in lab {slug!r}")
        checks = [_check_public(r) for r in conn.execute(
            "SELECT * FROM lab_checks WHERE patch_id = ? ORDER BY created_at", (patch_id,)).fetchall()]
    return {**_patch_public(row, diff=True), "checks": checks}


@router.post("/labs/{slug}/patches/{patch_id}/checks")
async def post_check(slug: str, patch_id: str, request: Request, req: CheckIn):
    """A checker's signed result. Only an agent the lab names as a checker; it decides nothing —
    a person lands a patch."""
    _on()
    caller = _caller(request)
    if why := labs.check_result(req.result):
        raise HTTPException(422, why)
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    with rs.DB_LOCK, rs.get_db() as conn:
        lab = _lab_row(conn, slug)
        if caller not in json.loads(lab["checkers_json"]):
            raise HTTPException(403, f"only this lab's checkers sign results ({', '.join(json.loads(lab['checkers_json']))})")
        row = conn.execute("SELECT * FROM lab_patches WHERE patch_id = ? AND lab_slug = ?", (patch_id, slug)).fetchone()
        if row is None:
            raise HTTPException(404, f"no patch {patch_id!r} in lab {slug!r}")
    signed = wire.signed_lab_check_payload(patch_id, row["diff_sha256"], row["base_commit"], req.result, caller, req.at)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "the check's signature does not verify against your key")
    check_id = f"lck_{secrets.token_hex(8)}"
    verdict = req.result["verdict"]
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO lab_checks (check_id, patch_id, checker_id, result_json, signed_at, signature, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                     (check_id, patch_id, caller, json.dumps(req.result, sort_keys=True), req.at, req.signature, _now()))
        conn.execute("UPDATE lab_patches SET status = ? WHERE patch_id = ?",
                     ("passed" if verdict == "pass" else "failed", patch_id))
        _log_now(conn, "lab_check", "lab_checks", "check_id", check_id)
        _log_now(conn, "lab_patch", "lab_patches", "patch_id", patch_id)
        conn.commit()
        crow = conn.execute("SELECT * FROM lab_checks WHERE check_id = ?", (check_id,)).fetchone()
    tests = req.result.get("tests") or {}
    iso = (req.result.get("sandbox") or {}).get("isolation")
    _post_in_room(lab["room_id"], f"Checked by {caller}: {verdict} — tests exit {tests.get('exit_code')}, "
                  f"secret scan {req.result.get('secret_scan')}, {iso} sandbox, risk {req.result.get('risk_floor')}.",
                  row["thread_message_id"] or None, {"lab": slug, "patch_id": patch_id, "check_id": check_id})
    return _check_public(crow)
