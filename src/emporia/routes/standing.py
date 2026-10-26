"""Leaderboard (P72-3).

The table an agent can lose a place on. Results already existed; nothing here
writes anything.

One query per table, not per agent — the N+1 shape N3-3 had to fix once already
on the neighbouring agent list.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.standing import compute_standings, contest_sql, game_module_types, wins_sql

router = APIRouter()


@router.get("/leaderboard")
async def leaderboard(limit: int = 25, module_type: str | None = None):
    """Standings as far as **this relay** knows.

    `scope` is in the response on purpose. Settlements are local: a peer's
    results would reach us as signed attestations, which does not exist yet, so
    this table is this relay's own contests and says so. Presenting a local
    table as a network ranking is the same class of claim as rendering a failed
    check as an empty one.
    """
    limit = rs._clamp_limit(limit)
    # P134: games only. Every settlement row used to count — a room's paid
    # entry is a settlement naming its creator, and a service sale names its
    # seller — so selling things ranked an agent as a winner.
    modules = game_module_types()
    wins_from, wins_params = wins_sql(modules, only=module_type)
    contest, contest_params = contest_sql(modules, only=module_type)
    with rs.DB_LOCK, rs.get_db() as conn:
        participations = [dict(r) for r in conn.execute(
            "SELECT p.agent_id AS agent_id, s.module_type AS module_type "
            "FROM participants p JOIN sessions s ON s.session_id = p.session_id "
            f"WHERE {contest}", contest_params).fetchall()]
        settlements = [dict(r) for r in conn.execute(
            "SELECT st.winner_id AS winner_id, st.status AS status, "
            "st.winner_payout_cents AS winner_payout_cents, "
            f"st.transfer_status AS transfer_status FROM {wins_from}", wins_params).fetchall()]

    standings = compute_standings(settlements, participations)
    return {
        "scope": "this_relay",
        "relay_url": rs.RELAY_BASE_URL,
        "module_type": module_type,
        "count": len(standings),
        "standings": [s.to_dict() for s in standings[:limit]],
    }


def _result_of(s: dict) -> dict:
    """One finished game, as a reader (or an opponent studying a player) needs
    it: who played, who won and how, the stake, and the game's final record —
    for Blotto every round's allocations, for chess the position and moves."""
    try:
        module = get_interaction_module(s["module_type"])
        _, outcome = module.is_terminal(SessionState.from_dict(s["state"]))
    except ValueError:
        outcome = {}  # a module this relay no longer loads: list the game, not its verdict
    rules = s.get("payment_rules") or {}
    return {
        "session_id": s["session_id"],
        "module_type": s["module_type"],
        "participants": s["participants"],
        "winner": outcome.get("winner"),
        "outcome_type": outcome.get("outcome_type"),
        "termination": outcome.get("termination"),
        "stake_per_participant": rules.get("stake_per_participant"),
        "currency": rules.get("currency"),
        "config": s.get("config") or {},
        "paid_by": rs.session_paid_by(s["session_id"]),  # P153
        "record": (s.get("state") or {}).get("data") or {},
        "ended_at": s["updated_at"],
    }


@router.get("/results")
async def results(request: Request, module_type: str | None = None,
                  agent_id: str | None = None, limit: int = 20):
    """Finished games, newest first (P134) — the record behind the leaderboard,
    and how an agent reads an opponent before playing them. Only games this
    caller may see (public, or ones they played in)."""
    limit = rs._clamp_limit(limit)
    contest, params = contest_sql(game_module_types(), only=module_type)
    q = f"SELECT * FROM sessions s WHERE s.status = 'completed' AND {contest}"
    if agent_id:
        q += " AND participants_json LIKE ?"
        params.append(f'%"{agent_id}"%')
    q += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit * 4)  # some may be hidden from this caller
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(q, params).fetchall()
    caller = rs._dashboard_agent_id(request)
    out = []
    for r in rows:
        s = rs._session_row_to_dict(r)
        if agent_id and agent_id not in s["participants"]:
            continue
        if not rs._session_viewer_allowed(s, caller):
            continue
        out.append(_result_of(s))
        if len(out) >= limit:
            break
    return {"scope": "this_relay", "module_type": module_type, "agent_id": agent_id,
            "results": out}


_RECORD_LIMIT = 1000


@router.get("/agents/{agent_id}/record")
async def counterparty_record(agent_id: str):
    """P182: an agent's record on this relay, to read before staking against it,
    joining its game, or buying from it: games finished, won, lost on time and
    abandoned; purchases as a buyer (confirmed, disputed, left to auto-confirm);
    sales as a seller (paid, disputed, never delivered). Counts only — evidence,
    not a verdict, and only what this relay saw."""
    with rs.DB_LOCK, rs.get_db() as conn:
        reg = conn.execute("SELECT registered_at, trust_level, public_key_hex, carried_records_json "
                           "FROM authorized_agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if reg is None:
            raise HTTPException(404, f"no agent {agent_id!r} on this relay")
        rows = conn.execute(
            "SELECT * FROM sessions WHERE participants_json LIKE ? AND status IN ('completed', 'abandoned') "
            "ORDER BY updated_at DESC LIMIT ?", (f'%"{agent_id}"%', _RECORD_LIMIT)).fetchall()
    games = {"finished": 0, "won": 0, "lost": 0, "drawn": 0, "lost_on_time": 0, "abandoned": 0}
    buyer = {"purchases": 0, "confirmed": 0, "disputed": 0, "left_to_auto_confirm": 0,
             "not_delivered_to_them": 0, "cancelled": 0}
    seller = {"sales": 0, "paid": 0, "disputed": 0, "never_delivered": 0, "cancelled": 0}
    for row in rows:
        sess = rs._session_row_to_dict(row)
        if agent_id not in sess["participants"]:
            continue
        try:
            module = get_interaction_module(sess["module_type"])
            _, out = module.is_terminal(SessionState.from_dict(sess["state"]))
        except ValueError:
            continue
        if sess["module_type"] == "emporia:service:v1":
            data = (sess.get("state") or {}).get("data") or {}
            side, rec = ("buyer", buyer) if data.get("buyer") == agent_id else ("seller", seller)
            rec["purchases" if side == "buyer" else "sales"] += 1
            st = out.get("status") if sess["status"] == "completed" else "cancelled"
            if side == "buyer":
                key = {"confirmed": "confirmed", "disputed": "disputed",
                       "timeout_auto_confirmed": "left_to_auto_confirm",
                       "timeout_no_delivery": "not_delivered_to_them"}.get(st, "cancelled")
            else:
                key = {"confirmed": "paid", "timeout_auto_confirmed": "paid", "disputed": "disputed",
                       "timeout_no_delivery": "never_delivered"}.get(st, "cancelled")
            rec[key] += 1
            continue
        if sess["module_type"] not in game_module_types():
            continue
        if sess["status"] == "abandoned":
            games["abandoned"] += 1
            continue
        games["finished"] += 1
        winner = out.get("winner")
        if winner == agent_id:
            games["won"] += 1
        elif winner:
            games["lost"] += 1
            if out.get("termination") == "timeout":
                games["lost_on_time"] += 1
        else:
            games["drawn"] += 1
    from datetime import UTC, datetime

    from emporia.addresses import address, fingerprint
    from emporia.identity import federation_signable_bytes, get_relay_public_key_hex, sign_raw_relay
    record = {
        "scope": "this_relay",
        "agent_id": agent_id,
        # P189: whose record — by address and key, so it can be read and
        # trusted (or not) on another relay — and when this relay said so.
        "address": address(agent_id, rs._own_host()),
        "public_key_hex": reg["public_key_hex"],
        "fingerprint": fingerprint(reg["public_key_hex"] or ""),
        "as_of": datetime.now(UTC).isoformat(),
        "registered_at": reg["registered_at"] if reg else None,
        "trust_level": reg["trust_level"] if reg else None,
        "games": games,
        "as_buyer": buyer,
        "as_seller": seller,
        "counted": min(len(rows), _RECORD_LIMIT),
        "note": "Evidence, not a verdict: counts of what this relay saw, newest 1000 sessions.",
    }
    # P189: signed by this relay, so another relay or agent can show it as
    # "this relay says…" and check it was not changed on the way.
    from emporia import moves
    # P190: other relays' words, carried by an agent that moved here — each as
    # its relay signed it, for a reader to check and weigh, outside this
    # relay's own signature.
    elsewhere = json.loads(reg["carried_records_json"] or "[]")
    return {**record, "relay": rs.RELAY_BASE_URL, "relay_public_key": get_relay_public_key_hex(),
            "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, "record", [record])),
            "elsewhere": elsewhere, "moved_to": moves.moved_to(agent_id)}
