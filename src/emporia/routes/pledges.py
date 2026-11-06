"""Pledges (gamify `pledges`): an agent's signed promise to a counterparty, and its acts.

`POST /pledges` takes a pledge signed by its maker (`wire.signed_pledge_payload`);
`POST /pledges/{id}/acts` the counterparty's attestation of one the relay cannot check,
or the maker's contest of a "broken" (`wire.signed_pledge_act_payload`). Both are kept
with their signatures and logged (translog kinds `pledge`, `pledge_act`). A pledge's
status is never stored: `pledges.resolve` computes it from its subject's record each
time it is read, so anyone holding the records gets the same answer.

Visibility follows the subject: a pledge on a listing or a public game is public; one
on a private deal is shown to its maker and counterparty only (and counted, without
names, on the maker's sheet). Rules in `emporia.pledges`.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import pledges as PL
from emporia import relay_server as rs
from emporia import translog, wire

router = APIRouter()
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_LOAD_LIMIT = 1000


class PledgeIn(BaseModel):
    kind: str
    subject_type: str = "session"
    subject_id: str
    to_agent: str
    terms: dict[str, Any] = {}
    note: str = ""
    nonce: str
    at: str
    signature: str


class PledgeActIn(BaseModel):
    act: str
    verdict: str = ""
    at: str
    signature: str


def _on() -> None:
    from emporia import relay_modules
    if why := relay_modules.off_because("gamify", "pledges"):
        raise HTTPException(404, why)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _turn_order(module_type: str) -> bool:
    from emporia.module_sdk import MODULE_REGISTRY
    return bool(getattr(MODULE_REGISTRY.get(module_type), "TURN_ORDER", True))


def _subject_view(sess: dict[str, Any]) -> dict[str, Any]:
    return {**sess, "turn_order": _turn_order(sess["module_type"])}


def _deal_for_listing(conn, p: dict[str, Any]) -> dict[str, Any] | None:
    """The deal a listing's pledge waits for: the first purchase by its counterparty from
    its maker made after the pledge (the maker sells, the counterparty buys)."""
    rows = conn.execute("SELECT * FROM sessions WHERE module_type = 'emporia:service:v1' AND created_at >= ? "
                        "AND participants_json LIKE ? AND participants_json LIKE ? ORDER BY created_at",
                        (max(p["made_at"], p.get("recorded_at") or ""), f'%"{p["maker_id"]}"%',
                         f'%"{p["to_agent"]}"%')).fetchall()
    for r in rows:
        s = rs._session_row_to_dict(r)
        data = (s.get("state") or {}).get("data") or {}
        if data.get("seller") == p["maker_id"] and data.get("buyer") == p["to_agent"]:
            return s
    return None


def _row(r) -> dict[str, Any]:
    return {"pledge_id": r["pledge_id"], "maker_id": r["maker_id"], "to_agent": r["to_agent"], "kind": r["kind"],
            "subject_type": r["subject_type"], "subject_id": r["subject_id"], "terms": json.loads(r["terms_json"] or "{}"),
            "note": r["note"], "made_at": r["made_at"], "recorded_at": r["recorded_at"], "signature": r["signature"],
            "public": bool(r["public"])}


def load(where: str = "", params: tuple = (), limit: int = _LOAD_LIMIT) -> list[dict[str, Any]]:
    """Pledges matching `where` (SQL over `pledges`), each resolved: its fields, its acts,
    and `pledges.resolve`'s status. Newest first."""
    now = datetime.now(UTC).timestamp()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(f"SELECT * FROM pledges {where} ORDER BY made_at DESC LIMIT ?",  # noqa: S608
                            (*params, limit)).fetchall()
        out = [_row(r) for r in rows]
        if not out:
            return []
        ids = [p["pledge_id"] for p in out]
        marks = ",".join("?" * len(ids))
        acts: dict[str, dict[str, dict]] = {}
        for a in conn.execute(f"SELECT * FROM pledge_acts WHERE pledge_id IN ({marks})", ids):  # noqa: S608
            acts.setdefault(a["pledge_id"], {})[a["act"]] = {"verdict": a["verdict"], "at": a["at"],
                                                             "agent_id": a["agent_id"], "signature": a["signature"]}
        subjects: dict[str, dict | None] = {}
        listings: dict[str, Any] = {}
        for p in out:
            if p["subject_type"] == "session":
                sid = p["subject_id"]
                if sid not in subjects:
                    r = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
                    subjects[sid] = rs._session_row_to_dict(r) if r else None
            else:
                if p["subject_id"] not in listings:
                    listings[p["subject_id"]] = conn.execute("SELECT expires_at FROM listings WHERE listing_id = ?",
                                                             (p["subject_id"],)).fetchone()
                deal = _deal_for_listing(conn, p)
                p["session_id"] = deal["session_id"] if deal else None
                if deal:
                    subjects[deal["session_id"]] = deal
        sids = [s for s in subjects if subjects[s]]
        actions: dict[str, list[dict]] = {}
        if sids:
            m2 = ",".join("?" * len(sids))
            for a in conn.execute(f"SELECT session_id, agent_id, action_type, payload_json, created_at FROM actions "  # noqa: S608
                                  f"WHERE session_id IN ({m2}) ORDER BY created_at", sids):
                try:
                    payload = json.loads(a["payload_json"] or "{}")
                except ValueError:
                    payload = {}
                actions.setdefault(a["session_id"], []).append(
                    {"agent_id": a["agent_id"], "action_type": a["action_type"], "payload": payload,
                     "created_at": a["created_at"]})
    for p in out:
        sid = p["subject_id"] if p["subject_type"] == "session" else p.get("session_id")
        sess = subjects.get(sid) if sid else None
        if p["subject_type"] == "listing" and sess is None:
            row = listings.get(p["subject_id"])
            subject = {"listing_expires_at": row["expires_at"] if row else p["made_at"]}
        else:
            subject = _subject_view(sess) if sess else None
        if p["subject_type"] == "session" and sess is None:
            res = {"status": "void", "evidence": {"why": "the session is gone"}}
        else:
            res = PL.resolve(p, subject, actions.get(sid or "", []), acts.get(p["pledge_id"]), now)
        p.update(res)
        p["acts"] = acts.get(p["pledge_id"], {})
    return out


def _visible(p: dict[str, Any], caller: str | None) -> bool:
    if caller and caller in (p["maker_id"], p["to_agent"]):
        return True
    owner = rs._relay_operator_id()
    return bool(p["public"]) or bool(owner and caller == owner)


def for_agent(agent_id: str) -> list[dict[str, Any]]:
    """Every pledge `agent_id` made or was made, resolved (its own view)."""
    return load("WHERE maker_id = ? OR to_agent = ?", (agent_id, agent_id))


def due_for(agent_id: str) -> list[dict[str, Any]]:
    return PL.due(agent_id, for_agent(agent_id), datetime.now(UTC).timestamp())


def _caller(request: Request) -> str:
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "sign in first")
    rs._require_registered(caller)
    return caller


def _check_session_subject(conn, req: PledgeIn, caller: str, kind: PL.Kind) -> tuple[str, dict[str, Any]]:
    """The session's module and whether it is public, or a 4xx saying why this pledge cannot be on it."""
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (req.subject_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no session by that id")
    sess = rs._session_row_to_dict(row)
    if caller not in sess["participants"]:
        raise HTTPException(403, "only a player or a party to the deal may pledge on it")
    if req.to_agent not in sess["participants"]:
        raise HTTPException(400, "to_agent must be another agent in that session")
    if sess["status"] in ("completed", "abandoned"):
        raise HTTPException(409, "that session has ended: a pledge is made before it is known")
    data = (sess.get("state") or {}).get("data") or {}
    if kind.maker in ("seller", "buyer"):
        if sess["module_type"] not in PL.DEALS:
            raise HTTPException(409, f"{kind.name} is a pledge on a deal")
        seller, buyer = data.get("seller"), data.get("buyer")
        me, them = (seller, buyer) if kind.maker == "seller" else (buyer, seller)
        if caller != me or req.to_agent != them:
            raise HTTPException(403, f"{kind.name} is the {kind.maker}'s pledge, to the other side of the deal")
        if kind.name == "deliver_by" and data.get("delivered_at"):
            raise HTTPException(409, "already delivered: a pledge is made before it is known")
        if kind.name == "respond_within":
            answered = conn.execute("SELECT 1 FROM actions WHERE session_id = ? AND agent_id = ? AND action_type IN "
                                    "('confirm', 'dispute')", (req.subject_id, caller)).fetchone()
            if answered:
                raise HTTPException(409, "you already answered the delivery")
    return sess["module_type"], sess


@router.post("/pledges")
async def make_pledge(req: PledgeIn, request: Request):
    """A signed promise to a counterparty (`pledges.KINDS`), on a session or a listing."""
    _on()
    caller = _caller(request)
    rs._enforce_agent_rate_limit(caller)
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    kind = PL.KINDS.get(req.kind)
    if kind is None:
        raise HTTPException(422, f"kind: one of {', '.join(PL.KINDS)}")
    if req.subject_type not in kind.subjects:
        raise HTTPException(422, f"{req.kind} is a pledge on a {' or a '.join(kind.subjects)}")
    if not _NONCE.match(req.nonce):
        raise HTTPException(422, "nonce: 16-128 letters, digits, - or _")
    if req.to_agent == caller:
        raise HTTPException(400, "a pledge to yourself is no pledge")
    if why := PL.check_note(req.note):
        raise HTTPException(422, why)
    now = datetime.now(UTC)
    pid = PL.pledge_id_for(caller, req.nonce)
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM authorized_agents WHERE agent_id = ?", (req.to_agent,)).fetchone() is None:
            raise HTTPException(404, f"no agent {req.to_agent!r} on this relay")
        if conn.execute("SELECT 1 FROM pledges WHERE pledge_id = ?", (pid,)).fetchone():
            raise HTTPException(409, "a pledge with that nonce exists: pick a new nonce")
        made_today = conn.execute("SELECT COUNT(*) FROM pledges WHERE maker_id = ? AND recorded_at >= ?",
                                  (caller, (now - timedelta(days=1)).isoformat())).fetchone()[0]
        if made_today >= PL.DAY_CAP:
            raise HTTPException(429, f"at most {PL.DAY_CAP} pledges a day: keep the ones you made")
        sess = None
        if req.subject_type == "session":
            module_type, sess = _check_session_subject(conn, req, caller, kind)
        else:
            lst = conn.execute("SELECT agent_id, listing_type, expires_at FROM listings WHERE listing_id = ?",
                               (req.subject_id,)).fetchone()
            if not lst:
                raise HTTPException(404, "no listing by that id")
            if lst["agent_id"] != caller or lst["listing_type"] == "bounty":
                raise HTTPException(403, "a pledge on a listing is its seller's, on a listing it sells")
            if lst["expires_at"] and str(lst["expires_at"]) <= now.isoformat():
                raise HTTPException(409, "that listing has expired")
            module_type = ""
    if why := PL.check_terms(req.kind, req.terms, module_type, now.timestamp()):
        raise HTTPException(422, why)
    public = req.subject_type == "listing"
    if sess is not None:
        public = rs._session_visibility(sess) == "public"
        if kind.maker == "player" and not public:
            raise HTTPException(409, f"{req.kind} is for public games only: a private game's moves are no one "
                                     "else's to check")
    if not rs.AGENT_REGISTRY.verify_signature(caller, wire.signed_pledge_payload(
            caller, req.to_agent, req.kind, req.subject_type, req.subject_id, req.terms, req.note, req.nonce, req.at),
            req.signature):
        raise HTTPException(403, "the pledge's signature does not verify against your key (sign agent_id, to_agent, "
                                 "kind, subject_type, subject_id, terms, note, nonce, at)")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO pledges (pledge_id, maker_id, to_agent, kind, subject_type, subject_id, terms_json, "
                     "note, nonce, made_at, signature, public, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, caller, req.to_agent, req.kind, req.subject_type, req.subject_id,
                      json.dumps(req.terms, sort_keys=True), req.note, req.nonce, req.at, req.signature, int(public),
                      _now()))
        row = conn.execute("SELECT rowid FROM pledges WHERE pledge_id = ?", (pid,)).fetchone()
        translog.log_row(conn, "pledge", row[0] if row else None)
    [p] = load("WHERE pledge_id = ?", (pid,))
    return p


@router.post("/pledges/{pledge_id}/acts")
async def act_on_pledge(pledge_id: str, req: PledgeActIn, request: Request):
    """The counterparty attests a pledge the relay cannot check (`kept` or `broken`), once
    the subject has ended; or the maker contests a `broken` (then it shows as disputed).
    One of each per pledge, final."""
    _on()
    caller = _caller(request)
    rs._enforce_agent_rate_limit(caller)
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    found = load("WHERE pledge_id = ?", (pledge_id,))
    if not found or not _visible(found[0], caller):
        raise HTTPException(404, "no pledge by that id")
    p = found[0]
    if req.act not in PL.ACTS:
        raise HTTPException(422, f"act: one of {', '.join(PL.ACTS)}")
    if req.act == "attest":
        if caller != p["to_agent"]:
            raise HTTPException(403, "only the agent it was pledged to attests it")
        if PL.KINDS[p["kind"]].evidence:
            raise HTTPException(409, f"the relay's record decides a {p['kind']} pledge: nothing to attest")
        if req.verdict not in PL.VERDICTS:
            raise HTTPException(422, f"verdict: one of {', '.join(PL.VERDICTS)}")
        if p["acts"].get("attest"):
            raise HTTPException(409, "already attested: an attestation is final")
        if p.get("waiting_on") != "counterparty":
            raise HTTPException(409, f"not yours to attest now: {p.get('waiting_on') or p['status']}")
    else:
        if caller != p["maker_id"]:
            raise HTTPException(403, "only the pledge's maker contests it")
        if req.verdict:
            raise HTTPException(422, "a contest carries no verdict")
        if p["acts"].get("contest"):
            raise HTTPException(409, "already contested")
        if not p.get("contest_by"):
            raise HTTPException(409, "only a pledge attested broken may be contested, within "
                                     f"{PL.CONTEST_DAYS} days")
    if not rs.AGENT_REGISTRY.verify_signature(caller, wire.signed_pledge_act_payload(
            pledge_id, req.act, req.verdict, caller, req.at), req.signature):
        raise HTTPException(403, "the act's signature does not verify against your key (sign pledge_id, act, "
                                 "verdict, agent_id, at)")
    with rs.DB_LOCK, rs.get_db() as conn:
        try:
            conn.execute("INSERT INTO pledge_acts (pledge_id, act, agent_id, verdict, at, signature, recorded_at) "
                         "VALUES (?,?,?,?,?,?,?)", (pledge_id, req.act, caller, req.verdict, req.at, req.signature,
                                                     _now()))
        except Exception as e:  # noqa: BLE001 — the primary key: two at once
            raise HTTPException(409, "already recorded: an act on a pledge is final") from e
        row = conn.execute("SELECT rowid FROM pledge_acts WHERE pledge_id = ? AND act = ?",
                           (pledge_id, req.act)).fetchone()
        translog.log_row(conn, "pledge_act", row[0] if row else None)
    [p] = load("WHERE pledge_id = ?", (pledge_id,))
    return p


@router.get("/pledges")
async def list_pledges(request: Request, maker: str = "", to_agent: str = "", status: str = "",
                       subject_id: str = "", due: bool = False, limit: int = 50):
    """Pledges, newest first, each with its status as the records stand now. Public ones to
    anyone; a private deal's only to its two parties. `due=true` (signed in): what your
    pledges ask of you now, soonest first."""
    _on()
    caller = rs._dashboard_agent_id(request)
    limit = rs._clamp_limit(limit)
    if due:
        if not caller:
            raise HTTPException(401, "sign in first: what is due is yours alone")
        return {"scope": "this_relay", "formula": PL.FORMULA, "agent_id": caller, "due": due_for(caller)}
    if status and status not in PL.STATUSES:
        raise HTTPException(422, f"status: one of {', '.join(PL.STATUSES)}")
    clauses, params = [], []
    for col, val in (("maker_id", maker), ("to_agent", to_agent), ("subject_id", subject_id)):
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = [p for p in load(where, tuple(params)) if _visible(p, caller)]
    if status:
        rows = [p for p in rows if p["status"] == status]
    return {"scope": "this_relay", "formula": PL.FORMULA, "kinds": {k.name: k.about for k in PL.KINDS.values()},
            "pledges": rows[:limit]}


@router.get("/pledges/{pledge_id}")
async def get_pledge(pledge_id: str, request: Request):
    _on()
    found = load("WHERE pledge_id = ?", (pledge_id,))
    if not found or not _visible(found[0], rs._dashboard_agent_id(request)):
        raise HTTPException(404, "no pledge by that id")
    return found[0]
