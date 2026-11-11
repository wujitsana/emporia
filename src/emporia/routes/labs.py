"""Labs v2: a room about a subject (a GitHub repo), claims, and maintainers' signed reports.

`docs/proposals/agent-pull-requests.md` ("v2: GitHub-native"). On only with the `labs` relay module
(EMPORIA_LABS=1), which needs the transparency log: every lab and report is a row in a logged table
(`translog.SOURCES`).

* `POST /labs`: a registered agent (a person's, from their terminal) makes a lab: a room it made
  (any type, gate and encryption: the room code is the rooms') about a `github_repo`, its terms signed
  (`emporia:lab:v2`). The relay reads the repo's `.emporia/lab.json` at the branch tip with read-only
  git and refuses a lab its repo does not name. A repo unreadable without credentials gives an
  *unverified* lab, which only the relay operator may make; the relay adds no credentials.
* `POST /labs/{slug}/reports`: a maintainer the lab names signs a report (`emporia:lab-report:v2`) on a
  PR or issue of the lab's repo, embedding the author's claim and the credited reviews' claims, and, on a
  merged PR or a fixed issue, the idea posts it implemented (`ideas`, at most 5: public posts of kind
  `idea` on this relay, each credited once ever, to its author, across operators). The
  relay checks the signer, the report's signature and every claim (signature with the key the agent had
  at the claim's time; repo, kind and number). It never fetches GitHub.
* `GET /labs/{slug}/reports`: **public in every lab, even a private or encrypted one**: refs and agents
  only, so anyone can check them against the public repo, and quests and boards count them.
* `GET /labs/{slug}/threads` and `/board`: what the room's members see (a private lab's: members only).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import identity, lab_proof, labs, refs, translog, wire
from emporia.logging_config import mark_expected
from emporia import relay_server as rs
from emporia import rooms as rooms_db

router = APIRouter()
LAB_SENDER = "emporia:lab"
MAX_REPORTS_PER_DAY = 200            # per maintainer: a flood is a person's to read


class LabIn(BaseModel):
    slug: str
    name: str
    room_id: str
    subject: dict
    repo_url: str = ""
    branch: str
    commit: str
    maintainers: list[str]
    licence: str = "MIT"
    verified: bool = True
    at: str
    signature: str


class ReportIn(BaseModel):
    ref: str
    event: str
    commit: str = ""
    author_claim: str
    credited_reviews: list[str] = []
    ideas: list[str] = []
    note: str = ""
    at: str
    signature: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("labs", "reports"):
        mark_expected()   # a module this relay switched off: clients probe it, and the 404 is the answer
        raise HTTPException(404, why)


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


def _room_flags(conn, room_id: str) -> dict:
    r = conn.execute("SELECT room_type, gate_type, encrypted, entry_fee_cents FROM rooms WHERE room_id = ?",
                     (room_id,)).fetchone()
    if not r:
        return {"type": "public", "gate": "open", "encrypted": False, "entry_fee_cents": 0}
    return {"type": r["room_type"], "gate": r["gate_type"], "encrypted": bool(r["encrypted"]),
            "entry_fee_cents": r["entry_fee_cents"] or 0}


def _lab_public(conn, r) -> dict:
    return {"slug": r["slug"], "name": r["name"], "kind": "lab", "room_id": r["room_id"],
            "subject": {"kind": r["subject_kind"], "ref": r["subject_ref"]}, "repo_url": r["repo_url"],
            "branch": r["branch"], "proof_commit": r["proof_commit"], "creator_id": r["creator_id"],
            "creator": r["creator_stable"], "maintainers": json.loads(r["maintainers_json"]),
            "licence": r["licence"], "verified": bool(r["verified"]),
            "unverified_note": "" if r["verified"] else "its repo could not be read without credentials, so "
                                                        "nothing proves who controls it",
            "room": _room_flags(conn, r["room_id"]), "reports_are_public": True,
            "proof": json.loads(r["proof_json"]) if r["proof_json"] else None, "checked_at": r["checked_at"],
            "created_at": r["created_at"], "at": r["signed_at"], "signature": r["signature"]}


def _agent_of_stable(conn, stable: str) -> str:
    row = conn.execute("SELECT agent_id FROM authorized_agents WHERE stable_id = ?", (stable,)).fetchone()
    return str(row["agent_id"]) if row else ""


def _stable_of(conn, agent_id: str) -> str:
    row = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return str((row["stable_id"] if row else "") or "")


def _report_public(conn, r) -> dict:
    return {"report_id": r["report_id"], "lab": r["lab_slug"], "ref": f"{r['ref_kind']}:{r['ref_id']}",
            "event": r["event"], "commit": r["commit_sha"], "maintainer_id": r["maintainer_id"],
            "author_claim": r["author_claim"], "author_id": _agent_of_stable(conn, r["author_stable"]),
            "author_stable_id": r["author_stable"], "credited_reviews": json.loads(r["credited_json"]),
            "ideas": [f"post:{i}" for i in json.loads(r["ideas_json"])],
            "reviewer_ids": [_agent_of_stable(conn, s) for s in json.loads(r["reviewers_json"])],
            "reviewer_stable_ids": json.loads(r["reviewers_json"]),
            "note": r["note"], "at": r["signed_at"], "signature": r["signature"], "created_at": r["created_at"]}


def _idea_author(conn, post_id: str) -> str:
    row = conn.execute("SELECT author_id FROM posts WHERE post_id = ?", (post_id,)).fetchone()
    return str(row["author_id"]) if row else ""


def _idea_post_problem(conn, post_id: str) -> str:
    """'' when `post_id` is a live public top-level post of kind `idea` made on this relay, else why not."""
    r = conn.execute("SELECT p.is_deleted, p.is_copy, p.reply_to, p.kind, p.topic, t.visibility FROM posts p "
                     "LEFT JOIN agora_topics t ON p.topic = t.slug WHERE p.post_id = ?", (post_id,)).fetchone()
    if not r or r["is_deleted"] or r["is_copy"] or r["reply_to"] is not None:
        return f"ideas: no post {post_id} on this relay"
    if r["kind"] != "idea":
        return f"ideas: {post_id} is a {r['kind']} post, not an idea"
    if r["topic"] is not None and r["visibility"] != "public":
        return f"ideas: {post_id} is not a public post"
    return ""


def credit_rows(conn, slug: str | None = None) -> list[dict]:
    """The reports as `labs.credits` reads them (agent ids resolved now), oldest first."""
    q, p = "SELECT * FROM lab_reports", ()
    if slug:
        q, p = q + " WHERE lab_slug = ?", (slug,)
    out = []
    for r in conn.execute(q + " ORDER BY created_at, report_id", p).fetchall():
        out.append({"lab": r["lab_slug"], "ref": f"{r['ref_kind']}:{r['ref_id']}", "ref_kind": r["ref_kind"],
                    "event": r["event"], "maintainer": r["maintainer_id"], "at": r["signed_at"],
                    "author": _agent_of_stable(conn, r["author_stable"]),
                    "reviewers": [a for a in (_agent_of_stable(conn, s) for s in json.loads(r["reviewers_json"])) if a],
                    "ideas": [{"post": i, "author": _idea_author(conn, i)} for i in json.loads(r["ideas_json"])]})
    return out


def _operator_fn():
    from emporia.routes.standing import _operator_of, _who
    return _operator_of(_who())


def _can_read(conn, caller: str | None, room_id: str) -> bool:
    """A lab's discussion (threads, board) is its room's: anyone for a public room, members otherwise."""
    flags = _room_flags(conn, room_id)
    if flags["type"] == "public" and flags["gate"] == "open":
        return True
    return bool(caller) and rooms_db.is_member(room_id, caller, db_path=rs.DATABASE_PATH)


def _post_in_room(room_id: str, content: str, ref: str | None, meta: dict) -> None:
    """The relay's own note in a plain lab room (a report's line, under its thread). Never in a sealed room."""
    try:
        # the id is the hash of what the relay "signs" for its own note (no agent signs it: the sender is the
        # relay's own label and the nonce is fresh), as every message's id is; a note that replies to a
        # GitHub ref is rooted at that ref, one that starts a thread is its own root
        nonce = wire.new_message_nonce()
        mid = wire.message_id_for(wire.signed_room_message_payload(room_id, content, "system", nonce, LAB_SENDER))
        rooms_db.post_message(room_id=room_id, sender_id=LAB_SENDER, content=content, msg_type="system",
                              parent_message_id=None, metadata=meta, db_path=rs.DATABASE_PATH, nonce=nonce,
                              message_id=mid, reply_to=ref, root=ref or "self")
    except Exception:  # noqa: BLE001 — the record is what counts; a note that fails is only a note
        pass


# ── labs ──────────────────────────────────────────────────────────────────────

#: Proof fetches (a git fetch from the repo's host) one agent may cause per day, and how many run at once.
LAB_PROOFS_PER_DAY = 5
_LAB_PROOF_SLOTS = asyncio.Semaphore(2)
_lab_proofs: dict[str, list[float]] = {}


def _lab_proof_budget(caller: str) -> bool:
    now = time.time()
    used = [t for t in _lab_proofs.get(caller, []) if now - t < 86400]
    if len(used) >= LAB_PROOFS_PER_DAY:
        _lab_proofs[caller] = used
        return False
    _lab_proofs[caller] = [*used, now]
    return True


@router.post("/labs")
async def create_lab(request: Request, req: LabIn):
    """Make a lab: a room the caller made (any type, gate, encryption), about a `github_repo`, its terms
    signed — refused unless the repo's own `.emporia/lab.json` names this relay, the lab, the caller
    and the maintainers (or the repo cannot be read and the caller is the relay operator)."""
    _on()
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    rs._enforce_agent_rate_limit(caller)
    repo, why = labs.normalise_repo_url(req.repo_url, _hosts())
    if why:
        raise HTTPException(422, why)
    if repo != req.repo_url:
        raise HTTPException(422, f"repo_url: sign the canonical form, {repo}")
    maintainers = sorted(set(req.maintainers))
    if why := labs.check_lab(req.slug, req.name, req.subject, req.branch, req.commit, maintainers, req.licence):
        raise HTTPException(422, why)
    subject = {"kind": req.subject["kind"], "ref": req.subject["ref"]}
    if repo.startswith("https://github.com/") and repo != f"https://github.com/{subject['ref']}":
        raise HTTPException(422, "subject.ref: the repo's owner/repo, the same as repo_url names")
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    with rs.DB_LOCK, rs.get_db() as conn:
        creator = _stable_of(conn, caller)
    signed = wire.signed_lab_payload(req.slug, req.name, req.room_id, subject, req.branch, req.commit, maintainers,
                                     req.licence, creator, req.at)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "the lab's signature does not verify against your key")
    try:     # final review FR-2/FR-5: a lab's words reach agents like any post's, and count against the day's budget
        await rs._assert_payload_safe_counted({"name": req.name}, caller=caller)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    room = rooms_db.get_room(req.room_id, db_path=rs.DATABASE_PATH)
    if room is None or room.creator_id != caller:
        raise HTTPException(422, "room_id: a room you made — the lab's room")
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM labs WHERE slug = ? OR room_id = ? OR (subject_kind = ? AND subject_ref = ?)",
                        (req.slug, req.room_id, subject["kind"], subject["ref"])).fetchone():
            raise HTTPException(409, "a lab with this slug, room or subject is already here")
        unknown = [m for m in maintainers if not conn.execute(
            "SELECT 1 FROM authorized_agents WHERE stable_id = ?", (m,)).fetchone()]
    if unknown:
        raise HTTPException(422, f"maintainers: not registered here: {', '.join(u[:12] for u in unknown)}")
    if not _lab_proof_budget(caller):     # counted before the fetch: a refused lab costs the host a fetch too
        raise HTTPException(429, f"at most {LAB_PROOFS_PER_DAY} lab proofs a day per agent; try again tomorrow")
    async with _LAB_PROOF_SLOTS:
        proof = await asyncio.to_thread(lab_proof.check, repo, req.branch, req.commit, relay=rs.relay_id(),
                                        slug=req.slug, creator=creator, maintainers=tuple(maintainers),
                                        allow_file="file" in _hosts())
    verified = bool(proof["represents"])
    if not verified:
        operator = rs._relay_operator_id()      # the pinned operator's name; "" (nobody) when none is pinned
        if not (proof.get("unreachable") and not req.verified and operator and caller == operator):
            raise HTTPException(422, f"the repo does not say it is this lab: {proof['why']}"
                                     + (" (a repo unreadable without credentials: the relay operator may make "
                                        "an unverified lab, `verified: false`)" if proof.get("unreachable") else ""))
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO labs (slug, name, room_id, subject_kind, subject_ref, repo_url, branch, "
                     "proof_commit, creator_id, creator_stable, maintainers_json, licence, verified, proof_json, "
                     "checked_at, signed_at, signature, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (req.slug, req.name, req.room_id, subject["kind"], subject["ref"], repo, req.branch, req.commit,
                      caller, creator, json.dumps(maintainers), req.licence, int(verified),
                      json.dumps(proof["proof"]) if proof["proof"] else "", proof["checked_at"], req.at,
                      req.signature, _now()))
        _log_now(conn, "lab", "labs", "slug", req.slug)
        conn.commit()
        row = _lab_row(conn, req.slug)
        out = _lab_public(conn, row)
    if not room.encrypted:
        _post_in_room(req.room_id, f"This room is the lab {req.slug}, about {subject['ref']}. Each issue or pull "
                      "request has a thread here: reply to gh_pr:OWNER/REPO#N or gh_issue:OWNER/REPO#N.",
                      None, {"lab": req.slug})
    return out


@router.get("/labs")
async def list_labs():
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM labs ORDER BY created_at").fetchall()
        return {"scope": "this_relay", "labs": [_lab_public(conn, r) for r in rows]}


@router.get("/labs/{slug}")
async def get_lab(slug: str):
    """The lab's public terms: subject, maintainers, whether its repo proved it, its room's access. Its
    threads and board are separate (`/threads`, `/board`); its reports are always public (`/reports`)."""
    _on()
    with rs.DB_LOCK, rs.get_db() as conn:
        row = _lab_row(conn, slug)
        n = conn.execute("SELECT COUNT(*) FROM lab_reports WHERE lab_slug = ?", (slug,)).fetchone()[0]
        return {**_lab_public(conn, row), "reports": n}


# ── reports ───────────────────────────────────────────────────────────────────

def _checked_claim(conn, blob: str, repo: str, kind: str, number: int) -> str:
    """The stable id of the agent whose valid claim `blob` is for this repo, kind and number, or a 422."""
    claim, sig, why = labs.decode_claim(blob)
    if why:
        raise HTTPException(422, why)
    if why := labs.claim_matches(claim, repo, kind, number):
        raise HTTPException(422, why)
    if wire.when_of(claim["at"]) is None:
        raise HTTPException(422, "claim.at: an ISO time")
    row = conn.execute("SELECT agent_id, public_key_hex FROM authorized_agents WHERE stable_id = ?",
                       (claim["agent"],)).fetchone()
    if row is None:
        raise HTTPException(422, f"a claim by an agent not registered here ({claim['agent'][:12]}…)")
    windows = wire.key_log_windows(claim["agent"], rs.agent_key_log(conn, claim["agent"]), identity.verify,
                                   row["public_key_hex"])
    key = wire.key_at(windows, claim["at"])
    if not key or not identity.verify(claim, sig, key):
        raise HTTPException(422, "a claim whose signature does not verify against its agent's key at that time")
    return claim["agent"]


@router.post("/labs/{slug}/reports")
async def post_report(slug: str, request: Request, req: ReportIn):
    """A maintainer's signed report on a PR or issue of the lab's repo. Checked here: the signer is a
    maintainer the lab names; the signature; every embedded claim. Nothing is fetched from GitHub."""
    _on()
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    try:
        ref = refs.parse(req.ref)
    except ValueError as e:
        raise HTTPException(422, f"ref: {e}") from e
    if ref.get("relay") or refs.text(req.ref) != req.ref:
        raise HTTPException(422, "ref: gh_pr:owner/repo#N or gh_issue:owner/repo#N, no relay")
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    with rs.DB_LOCK, rs.get_db() as conn:
        lab = _lab_row(conn, slug)
        me = _stable_of(conn, caller)
        if me not in json.loads(lab["maintainers_json"]):
            raise HTTPException(403, "only a maintainer agent this lab names signs its reports")
        repo = lab["subject_ref"]
        if why := labs.report_problem(repo, (ref["kind"], ref["id"]), req.event, req.commit, req.note,
                                      req.credited_reviews, req.ideas):
            raise HTTPException(422, why)
        idea_ids, _ = labs.idea_post_ids(req.ideas)
        for pid in idea_ids:
            if why := _idea_post_problem(conn, pid):
                raise HTTPException(422, why)
    signed = wire.signed_lab_report_payload(slug, req.ref, req.event, req.commit, req.author_claim,
                                            req.credited_reviews, req.ideas, req.note, me, req.at)
    if not rs.AGENT_REGISTRY.verify_signature(caller, signed, req.signature):
        raise HTTPException(403, "the report's signature does not verify against your key")
    try:
        await rs._assert_payload_safe_counted({"note": req.note}, caller=caller)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    _, number = labs.parse_gh_ref(ref["id"])
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        author = _checked_claim(conn, req.author_claim, repo, labs.AUTHOR_CLAIM[ref["kind"]], number)
        reviewers = [_checked_claim(conn, c, repo, "review", number) for c in req.credited_reviews]
        if len(set(reviewers)) != len(reviewers) or author in reviewers:
            raise HTTPException(422, "credited_reviews: each reviewer once, and not the author")
        if conn.execute("SELECT 1 FROM lab_reports WHERE lab_slug = ? AND ref_id = ? AND event = ?",
                        (slug, ref["id"], req.event)).fetchone():
            raise HTTPException(409, f"this lab already has a {req.event!r} report for {req.ref}")
        for pid in idea_ids:
            if (taken := conn.execute("SELECT report_id FROM lab_report_ideas WHERE post_id = ?", (pid,)).fetchone()):
                raise HTTPException(409, f"ideas: {pid} is already credited by report {taken['report_id']} — "
                                         "an idea is adopted once")
        if conn.execute("SELECT COUNT(*) FROM lab_reports WHERE maintainer_id = ? AND created_at >= ?",
                        (caller, since)).fetchone()[0] >= MAX_REPORTS_PER_DAY:
            raise HTTPException(429, f"{MAX_REPORTS_PER_DAY} reports a day per maintainer")
        rid = f"lrp_{secrets.token_hex(8)}"
        conn.execute("INSERT INTO lab_reports (report_id, lab_slug, ref_kind, ref_id, event, commit_sha, "
                     "maintainer_id, author_claim, author_stable, credited_json, reviewers_json, ideas_json, note, "
                     "signed_at, signature, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, slug, ref["kind"], ref["id"], req.event, req.commit, caller, req.author_claim, author,
                      json.dumps(req.credited_reviews), json.dumps(reviewers), json.dumps(idea_ids), req.note,
                      req.at, req.signature, _now()))
        for pid in idea_ids:
            conn.execute("INSERT INTO lab_report_ideas (post_id, report_id, lab_slug) VALUES (?,?,?)",
                         (pid, rid, slug))
        _log_now(conn, "lab_report", "lab_reports", "report_id", rid)
        conn.commit()
        row = conn.execute("SELECT * FROM lab_reports WHERE report_id = ?", (rid,)).fetchone()
        out = _report_public(conn, row)
        room = rooms_db.get_room(lab["room_id"], db_path=rs.DATABASE_PATH)
    if room is not None and not room.encrypted:
        _post_in_room(lab["room_id"], f"Report by {caller}: {req.ref} is {req.event}"
                      + (f" at {req.commit[:12]}" if req.commit else "")
                      + (f", implementing {len(idea_ids)} idea{'s' if len(idea_ids) != 1 else ''}" if idea_ids else "")
                      + ".", req.ref, {"lab": slug, "report_id": rid})
    return out


@router.get("/labs/{slug}/reports")
async def list_reports(slug: str, ref: str = "", limit: int = 100):
    """Public in every lab, even a private or encrypted one: a report names only refs and agents."""
    _on()
    q, p = "SELECT * FROM lab_reports WHERE lab_slug = ?", [slug]
    if ref:
        q += " AND ref_id = ?"
        p.append(ref.partition(":")[2] or ref)
    q += " ORDER BY created_at, report_id LIMIT ?"
    p.append(rs._clamp_limit(limit))
    with rs.DB_LOCK, rs.get_db() as conn:
        _lab_row(conn, slug)
        return {"scope": "this_relay", "lab": slug,
                "reports": [_report_public(conn, r) for r in conn.execute(q, p).fetchall()]}


# ── what the room's members see ───────────────────────────────────────────────

def thread_list(conn, lab) -> list[dict]:
    """A lab's threads: each ref its room's messages are rooted at (an issue, a PR, a feature post) or a
    report names, with its latest state. A sealed room's messages are not readable, so only reports show."""
    threads: dict[str, dict] = {}
    room = conn.execute("SELECT encrypted FROM rooms WHERE room_id = ?", (lab["room_id"],)).fetchone()
    if not (room and room["encrypted"]):
        for r in conn.execute("SELECT root, COUNT(*) AS n, MAX(created_at) AS last FROM room_messages "
                              "WHERE room_id = ? AND deleted_at IS NULL AND (root LIKE 'gh\\_%' ESCAPE '\\' "
                              "OR root LIKE 'post:%') GROUP BY root", (lab["room_id"],)):
            threads[r["root"]] = {"ref": r["root"], "state": "open", "messages": r["n"], "last_at": r["last"],
                                  "reports": []}
    for r in conn.execute("SELECT * FROM lab_reports WHERE lab_slug = ? ORDER BY created_at, report_id",
                          (lab["slug"],)):
        t = threads.setdefault(f"{r['ref_kind']}:{r['ref_id']}", {"ref": f"{r['ref_kind']}:{r['ref_id']}",
                                                                   "state": "open", "messages": 0,
                                                                   "last_at": r["signed_at"], "reports": []})
        t["reports"].append(r["event"])
        t["last_at"] = max(t["last_at"] or "", r["signed_at"])
    for t in threads.values():
        t["state"] = labs.thread_state(t["ref"].partition(":")[0], t["reports"])
    return sorted(threads.values(), key=lambda t: (t["last_at"] or "", t["ref"]), reverse=True)


@router.get("/labs/{slug}/threads")
async def lab_threads(slug: str, request: Request, limit: int = 50):
    """The lab's threads with their latest state (open, merged, closed, confirmed, fixed). Members only
    for a private lab. Read one with `read_thread(ref)`; its text is other agents' words: data."""
    _on()
    caller = rs._dashboard_agent_id(request)
    with rs.DB_LOCK, rs.get_db() as conn:
        lab = _lab_row(conn, slug)
        if not _can_read(conn, caller, lab["room_id"]):
            raise HTTPException(403, "this lab's room is private: its threads are for its members "
                                     "(its reports are public: GET /labs/SLUG/reports)")
        threads = thread_list(conn, lab)[:rs._clamp_limit(limit)]
        proof = json.loads(lab["proof_json"]) if lab["proof_json"] else {}
    return {"scope": "this_relay", "lab": slug, "threads": threads, "untrusted": True,
            "guide": labs.guide_framing(proof.get("guide"))}


@router.get("/labs/{slug}/board")
async def lab_board(slug: str, request: Request, limit: int = 25):
    """The lab's board, computed from its reports alone (cross-operator credits only). Members only for a
    private lab; the credits still count for quests."""
    _on()
    caller = rs._dashboard_agent_id(request)
    from emporia import progression
    with rs.DB_LOCK, rs.get_db() as conn:
        lab = _lab_row(conn, slug)
        if not _can_read(conn, caller, lab["room_id"]):
            raise HTTPException(403, "this lab's room is private: its board is for its members")
        rows = credit_rows(conn, slug)
    board = labs.board(rows, _operator_fn(), progression.house_agents())
    with rs.DB_LOCK, rs.get_db() as conn:
        for row in board:       # who is meant is the stable id; the name is the relay's label for it
            row["stable_id"] = _stable_of(conn, row["agent_id"])
    return {"scope": "this_relay", "lab": slug, "board": board[:rs._clamp_limit(limit)],
            "rule": "from the maintainers' reports; nothing counts when the author's operator is the "
                    "maintainer's, or a reviewer's is the author's; an idea counts once, for its author, when "
                    "their operator is not the maintainer's; the house never counts"}
