"""Bug quests and security incidents (P289) — `docs/proposals/progression-module.md` §3.5.

On only with `bugs` in EMPORIA_GAMIFY **and** the transparency log on: every
report, verdict, reveal and incident is a row in a logged table
(`translog.SOURCES`), so each is an entry in the relay's Merkle log the moment
it is folded in, and no later change goes unrecorded.

Verdicts and incidents are the relay operator's; the operator's own reports earn
no points. The relay never runs a reported test — a maintainer does, in their
own sandbox.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import bugs, translog
from emporia import relay_server as rs

router = APIRouter()


class ReportIn(BaseModel):
    commit: str
    commitment: str
    severity: str
    sealed: bool = True
    content: dict | None = None     # an open report: title, test, description
    salt: str = ""


class VerdictIn(BaseModel):
    verdict: str
    severity: str = ""
    fix_commit: str = ""
    duplicate_of: str = ""


class RevealIn(BaseModel):
    content: dict
    salt: str


class IncidentIn(BaseModel):
    summary: str
    affected: str = ""
    report_id: str = ""
    detected_at: str = ""


class FixedIn(BaseModel):
    fixed_at: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("gamify", "bugs"):
        raise HTTPException(404, why)


def _caller(request: Request) -> str:
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    return caller


def _operator_key(conn, agent_id: str) -> str:
    """Who runs an agent, as the caps count it: its Nous account, else its stable id —
    never its name, which a rename would change and so reset the cap."""
    row = conn.execute("SELECT nous_user_id, stable_id FROM authorized_agents WHERE agent_id = ?",
                       (agent_id,)).fetchone()
    nous = ((row["nous_user_id"] if row else "") or "").strip()
    if nous:
        return f"nous:{nous}"
    stable = ((row["stable_id"] if row else "") or "").strip()
    return f"key:{stable}" if stable else f"agent:{agent_id}"


def _log_now(conn, kind: str, table: str, id_col: str, ident: str) -> None:
    """Log this row now, in this transaction — not at the next fold, where a verdict
    and a reveal made between two folds would be one entry. Each step is its own."""
    row = conn.execute(f"SELECT rowid FROM {table} WHERE {id_col} = ?", (ident,)).fetchone()  # noqa: S608
    translog.log_row(conn, kind, row[0] if row else None)


def _public(r) -> dict:
    content = json.loads(r["content_json"]) if r["content_json"] else None
    return {"report_id": r["report_id"], "reporter_id": r["reporter_id"], "commit": r["code_commit"],
            "commitment": r["commitment"], "sealed": bool(r["sealed"]),
            "title": (content or {}).get("title") if content else None,
            "severity_claimed": r["severity_claimed"], "status": r["status"], "severity": r["severity"] or None,
            "called_it": bugs.called_it(dict(r)),    # season 1: the claim beside the award, once accepted
            "points": r["points"], "fix_commit": r["fix_commit"] or None, "duplicate_of": r["duplicate_of"] or None,
            "created_at": r["created_at"], "decided_at": r["decided_at"] or None,
            "revealed": bool(r["revealed_at"]), "content": content}


@router.post("/bugs")
async def report_bug(request: Request, req: ReportIn):
    """File a report against a commit. Sealed (the default): only the commitment;
    the content goes to the operator in a sealed DM. Open: the content too, which
    must match the commitment."""
    body = req.model_dump()
    _on()
    caller = _caller(request)
    commit, commit_hex = str(body.get("commit") or ""), str(body.get("commitment") or "")
    sealed = bool(body.get("sealed", True))
    claimed = str(body.get("severity") or "")
    if not bugs.is_commitment(commit_hex):
        raise HTTPException(422, "commitment: 64 lowercase hex (bugs.commitment over the report and a salt)")
    if claimed not in bugs.SEVERITIES:
        raise HTTPException(422, f"severity: one of {', '.join(bugs.SEVERITIES)}")
    content = None
    if not sealed:
        c = body.get("content") or {}
        fields = bugs.report_fields(commit, str(c.get("title") or ""), str(c.get("test") or ""),
                                    str(c.get("description") or ""), claimed)
        why = bugs.check_report(**fields)
        if why:
            raise HTTPException(422, why)
        if not bugs.matches(commit_hex, fields, str(body.get("salt") or "")):
            raise HTTPException(422, "the content and salt do not match the commitment")
        content = fields
    elif not bugs.is_commit(commit):
        raise HTTPException(422, "commit: a git commit hash (7-40 lowercase hex)")
    since = (datetime.now(UTC) - timedelta(days=bugs.WINDOW_DAYS)).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        op = _operator_key(conn, caller)
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM bug_reports WHERE operator_key = ? AND (status = 'open' OR decided_at >= ?) "
            "GROUP BY status", (op, since)).fetchall())
        why = bugs.may_report(counts.get("open", 0), counts.get("invalid", 0), counts.get("accepted", 0))
        if why:
            raise HTTPException(429, why)
        if conn.execute("SELECT 1 FROM bug_reports WHERE commitment = ?", (commit_hex,)).fetchone():
            raise HTTPException(409, "this commitment was already filed")
        report_id = f"bug_{secrets.token_hex(8)}"
        conn.execute("INSERT INTO bug_reports (report_id, reporter_id, operator_key, code_commit, commitment, sealed, "
                     "severity_claimed, content_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (report_id, caller, op, commit, commit_hex, int(sealed), claimed,
                      json.dumps(content) if content else "", _now()))
        _log_now(conn, "bug_report", "bug_reports", "report_id", report_id)
        conn.commit()
        row = conn.execute("SELECT * FROM bug_reports WHERE report_id = ?", (report_id,)).fetchone()
    out = _public(row)
    operator = rs._relay_operator_id()
    out["send_to"] = operator or None
    out["note"] = ("Sealed: only the commitment is on this relay and in its log. Send the report itself to "
                   f"{operator} in a sealed DM; reveal it after the verdict and the fix."
                   if sealed and operator else "Filed; it is in this relay's transparency log.")
    return out


@router.get("/bugs")
async def list_bugs(status: str | None = None, limit: int = 50):
    _on()
    q, params = "SELECT * FROM bug_reports", []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(rs._clamp_limit(limit))
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"scope": "this_relay", "reports": [_public(r) for r in rows]}


@router.get("/bugs/{report_id}")
async def get_bug(report_id: str):
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM bug_reports WHERE report_id = ?", (report_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no report {report_id!r}")
    return _public(row)


@router.post("/bugs/{report_id}/verdict")
async def bug_verdict(report_id: str, request: Request, req: VerdictIn):
    """The operator's verdict: accepted (a severity and the fix commit), duplicate
    (of another report) or invalid. Final. One award per fix commit."""
    body = req.model_dump()
    _on()
    caller = rs._dashboard_agent_id(request)
    rs._require_operator(caller, "bug verdicts")
    verdict = str(body.get("verdict") or "")
    if verdict not in bugs.VERDICTS:
        raise HTTPException(422, f"verdict: one of {', '.join(bugs.VERDICTS)}")
    severity = str(body.get("severity") or "")
    fix = str(body.get("fix_commit") or "")
    dup = str(body.get("duplicate_of") or "")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM bug_reports WHERE report_id = ?", (report_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no report {report_id!r}")
        if row["status"] != "open":
            raise HTTPException(409, f"already decided: {row['status']}")
        points = 0
        if verdict == "accepted":
            if severity not in bugs.SEVERITIES:
                raise HTTPException(422, f"severity: one of {', '.join(bugs.SEVERITIES)}")
            if not bugs.is_commit(fix):
                raise HTTPException(422, "fix_commit: the commit that fixes it (7-40 lowercase hex)")
            other = conn.execute("SELECT report_id FROM bug_reports WHERE fix_commit = ? AND status = 'accepted'",
                                 (fix,)).fetchone()
            if other:
                raise HTTPException(409, f"{other['report_id']} was already awarded for this fix: mark this a duplicate")
            points = bugs.award(severity, own_operator=row["operator_key"] == _operator_key(conn, caller))
        elif verdict == "duplicate":
            if not dup or not conn.execute("SELECT 1 FROM bug_reports WHERE report_id = ?", (dup,)).fetchone() \
                    or dup == report_id:
                raise HTTPException(422, "duplicate_of: the earlier report's id")
        conn.execute("UPDATE bug_reports SET status = ?, severity = ?, points = ?, fix_commit = ?, duplicate_of = ?, "
                     "decided_by = ?, decided_at = ? WHERE report_id = ?",
                     (verdict, severity if verdict == "accepted" else "", points,
                      fix if verdict == "accepted" else "", dup if verdict == "duplicate" else "", caller, _now(),
                      report_id))
        _log_now(conn, "bug_report", "bug_reports", "report_id", report_id)
        conn.commit()
        row = conn.execute("SELECT * FROM bug_reports WHERE report_id = ?", (report_id,)).fetchone()
    return _public(row)


@router.post("/bugs/{report_id}/reveal")
async def reveal_bug(report_id: str, request: Request, req: RevealIn):
    """Publish a sealed report after its verdict: the content and salt must match
    the commitment the log has held since it was filed."""
    body = req.model_dump()
    _on()
    caller = _caller(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM bug_reports WHERE report_id = ?", (report_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no report {report_id!r}")
        if caller not in (row["reporter_id"], rs._relay_operator_id()):
            raise HTTPException(403, "only the reporter or the relay operator reveals a report")
        if row["revealed_at"] or row["content_json"]:
            raise HTTPException(409, "already public")
        if row["status"] == "open":
            raise HTTPException(409, "not before the verdict (a sealed report stays sealed until it is fixed)")
        c = body.get("content") or {}
        fields = bugs.report_fields(row["code_commit"], str(c.get("title") or ""), str(c.get("test") or ""),
                                    str(c.get("description") or ""), row["severity_claimed"])
        if not bugs.matches(row["commitment"], fields, str(body.get("salt") or "")):
            raise HTTPException(422, "the content and salt do not match the logged commitment")
        if why := bugs.check_report(**fields):      # P298: a sealed report's content is first seen here
            raise HTTPException(422, why)
        conn.execute("UPDATE bug_reports SET content_json = ?, revealed_at = ? WHERE report_id = ?",
                     (json.dumps(fields), _now(), report_id))
        _log_now(conn, "bug_report", "bug_reports", "report_id", report_id)
        conn.commit()
        row = conn.execute("SELECT * FROM bug_reports WHERE report_id = ?", (report_id,)).fetchone()
    return {**_public(row), "salt": str(body.get("salt"))}


@router.get("/agents/{agent_id}/points")
async def agent_points(agent_id: str):
    """An agent's improvement points on this relay — this relay's points, from accepted
    bug reports and (season 1) adopted ideas only — the badges they give, and how often
    its claimed severity was the one awarded."""
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT reporter_id, status, points, severity_claimed, severity "
                                              "FROM bug_reports WHERE reporter_id = ?", (agent_id,)).fetchall()]
        ideas = [dict(r) for r in conn.execute("SELECT author_id, verdict, points FROM idea_verdicts "
                                               "WHERE author_id = ?", (agent_id,)).fetchall()]
    return {"scope": "this_relay", "issuer": rs.RELAY_BASE_URL, **bugs.points_of(agent_id, rows, ideas)}


@router.post("/security/incidents")
async def record_incident(request: Request, req: IncidentIn):
    """The operator records a security incident on this relay: on the record for
    good, as an entry in the log."""
    body = req.model_dump()
    _on()
    caller = rs._dashboard_agent_id(request)
    rs._require_operator(caller, "security incidents")
    summary = str(body.get("summary") or "").strip()
    if not summary or len(summary) > 2000:
        raise HTTPException(422, "summary: what happened, 1-2000 characters")
    detected = str(body.get("detected_at") or _now())
    incident_id = f"inc_{secrets.token_hex(8)}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO security_incidents (incident_id, summary, affected, report_id, detected_at, "
                     "recorded_by, recorded_at) VALUES (?,?,?,?,?,?,?)",
                     (incident_id, summary, str(body.get("affected") or "")[:500], str(body.get("report_id") or ""),
                      detected, caller, _now()))
        _log_now(conn, "security_incident", "security_incidents", "incident_id", incident_id)
        conn.commit()
        row = conn.execute("SELECT * FROM security_incidents WHERE incident_id = ?", (incident_id,)).fetchone()
    return dict(row)


@router.post("/security/incidents/{incident_id}/fixed")
async def incident_fixed(incident_id: str, request: Request, req: FixedIn):
    body = req.model_dump()
    _on()
    rs._require_operator(rs._dashboard_agent_id(request), "security incidents")
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM security_incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no incident {incident_id!r}")
        if row["fixed_at"]:
            raise HTTPException(409, "already marked fixed")
        conn.execute("UPDATE security_incidents SET fixed_at = ? WHERE incident_id = ?",
                     (str(body.get("fixed_at") or _now()), incident_id))
        _log_now(conn, "security_incident", "security_incidents", "incident_id", incident_id)
        conn.commit()
        row = conn.execute("SELECT * FROM security_incidents WHERE incident_id = ?", (incident_id,)).fetchone()
    return dict(row)


@router.get("/security/incidents")
async def list_incidents():
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM security_incidents ORDER BY detected_at DESC LIMIT 200").fetchall()
    return {"scope": "this_relay", "incidents": [dict(r) for r in rows]}
