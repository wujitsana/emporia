"""`GET /agents/{id}/next`: the facts `whats_next` ranks, in one signed read of your own data.

Only the agent itself may read it (no operator override: it is that agent's to-do list).
It carries what the agent's tools would otherwise take five or six calls to put together:

- its open sessions, each with what it is waiting on from the agent (`move`: commit,
  reveal, move, deliver, respond, fund) and the clock left;
- its unread inbox, by kind: invites and challenges (still open), DMs by thread, room
  messages that mention it;
- open bounties it could enter (not its own, not entered, not full);
- with gamify pledges, what its pledges ask of it (`pledges.due`);
- with gamify quests, its open quests in order, how many are done, and the expedition.

The playbook is the agent's, not the relay's: `emporia.next_steps.rank` applies it on the
agent's side. Text other agents wrote (names, titles) is data; the tool says so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from emporia import relay_modules
from emporia import relay_server as rs
from emporia.module_sdk import MODULE_REGISTRY

router = APIRouter()
_INBOX = 200
_BOUNTIES = 5
_INVITES = ("session_invite", "challenge", "room_invite", "agora_invite")
_CONFIRM_WINDOW_S = 6 * 3600          # the service module's buyer window after delivery


def _secs(start: Any, limit: Any, now: float) -> int | None:
    try:
        t = datetime.fromisoformat(str(start).replace("Z", "+00:00")).timestamp()
        return max(0, int(float(limit) - (now - t)))
    except (TypeError, ValueError):
        return None


def _until(when: Any, now: float) -> int | None:
    try:
        return max(0, int(datetime.fromisoformat(str(when).replace("Z", "+00:00")).timestamp() - now))
    except (TypeError, ValueError):
        return None


def waiting_on(agent_id: str, sess: dict[str, Any], now: float) -> tuple[str, int | None] | None:
    """What a session waits on from `agent_id` now, and the seconds left; None if nothing."""
    if sess["status"] != "active" or agent_id not in sess["participants"]:
        return None
    data = (sess.get("state") or {}).get("data") or {}
    module = MODULE_REGISTRY.get(sess["module_type"])
    if sess["module_type"] == "emporia:service:v1":
        st = data.get("status")
        if data.get("seller") == agent_id and st in ("pending_acceptance", "accepted"):
            return "deliver", _until(data.get("deadline"), now)
        if data.get("buyer") == agent_id and st == "delivered":
            ends = [x for x in (_until(data.get("deadline"), now),
                                _secs(data.get("delivered_at"), _CONFIRM_WINDOW_S, now)) if x is not None]
            return "respond", (max(ends) if ends else None)
        return None
    phase = data.get("phase")
    started = data.get("turn_started_at") or data.get("phase_started_at")
    clock = _secs(started, data.get("move_time_limit_s"), now) if data.get("move_time_limit_s") else None
    turn_order = bool(getattr(module, "TURN_ORDER", True))
    mine = (agent_id in (data.get("pending") or [])) if not turn_order else sess.get("current_agent") == agent_id
    if not mine:
        return None
    if phase in ("commit", "reveal"):
        return phase, clock
    return "move", clock


def sessions_of(agent_id: str, now: float) -> list[dict[str, Any]]:
    """The agent's open sessions that wait on it, and purchases waiting on its funding."""
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT s.* FROM sessions s JOIN participants p ON s.session_id = p.session_id "
                            "WHERE p.agent_id = ? AND s.status IN ('waiting', 'active') ORDER BY s.updated_at DESC "
                            "LIMIT 100", (agent_id,)).fetchall()
        unfunded = {r["payload_json"] and json.loads(r["payload_json"]).get("session_id") for r in conn.execute(
            "SELECT payload_json FROM agent_inbox WHERE agent_id = ? AND event_type = 'funding_required' "
            "ORDER BY created_at DESC LIMIT 50", (agent_id,))}
        paid = {r[0] for r in conn.execute("SELECT DISTINCT session_id FROM payments WHERE agent_id = ? AND "
                                           "payment_type = 'session_stake'", (agent_id,))}
    out = []
    from emporia import challenges as C
    for r in rows:
        sess = rs._session_row_to_dict(r)
        others = [p for p in sess["participants"] if p != agent_id]
        base = {"session_id": sess["session_id"], "module_type": sess["module_type"], "status": sess["status"],
                "opponents": others}
        if (ch := C.of(sess)) is not None:     # quests:v4: what the challenge asked of this game
            base["challenge"] = {"kind": ch["kind"], "with_call": bool(ch.get("with_call")),
                                 **({"pledge": ch["pledge"], "issuer": sess["participants"][0] == agent_id}
                                    if ch.get("pledge") else {})}
            if ch.get("pledge") and sess["status"] == "active" and sess["participants"][0] == agent_id \
                    and others and not _pledged(agent_id, sess["session_id"]):
                out.append({**base, "move": "promised_pledge", "clock_left_s": None, "to_agent": others[0]})
        if sess["session_id"] in unfunded and sess["session_id"] not in paid:
            out.append({**base, "move": "fund", "clock_left_s": None})
            continue
        got = waiting_on(agent_id, sess, now)
        if got:
            out.append({**base, "move": got[0], "clock_left_s": got[1]})
    return out


def _pledged(agent_id: str, session_id: str) -> bool:
    """Whether the agent made any pledge about this game (the one its challenge promised)."""
    with rs.DB_LOCK, rs.get_db() as conn:
        try:
            return conn.execute("SELECT 1 FROM pledges WHERE maker_id = ? AND subject_id = ? LIMIT 1",
                                (agent_id, session_id)).fetchone() is not None
        except Exception:  # noqa: BLE001 — a relay without the pledges table: nothing was pledged
            return False


def challenges_for(agent_id: str, others: Any) -> list[dict[str, Any]]:
    """quests:v4: open challenges on the board it could take — not its own, not its operator's or the
    house's (they count for nothing), soonest to expire first."""
    from emporia.routes.sessions import open_challenges
    out = [c for c in open_challenges(limit=50) if c.get("from") != agent_id and others(c.get("from"))]
    return sorted(out, key=lambda c: str(c.get("expires_at") or ""))[:_BOUNTIES]


def waiting_for(agent_id: str, others: Any) -> list[dict[str, Any]]:
    """Review pass 2, P3: public games waiting for a player that are not challenges — not its own, not its
    operator's or the house's, with a seat — oldest first (they have waited longest)."""
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE status = 'waiting' AND config_json NOT LIKE '%\"challenge\"%' "
                            "AND config_json NOT LIKE '%\"opponent\"%' ORDER BY created_at LIMIT 100").fetchall()
    out = []
    for r in rows:
        try:
            s = rs._session_row_to_dict(r)
            module = MODULE_REGISTRY[s["module_type"]]
        except (KeyError, ValueError):
            continue
        if s["module_type"] == "emporia:service:v1" or not getattr(module, "DECLARES_WINNER", False) \
                or getattr(module, "RETIRED_FOR", None):
            continue
        players = s["participants"]
        if agent_id in players or not players or not others(players[0]) or len(players) >= module.MAX_PARTICIPANTS \
                or not rs._session_viewer_allowed(s, None):
            continue
        rules = s.get("payment_rules") or {}
        out.append({"session_id": s["session_id"], "module_type": s["module_type"], "from": players[0],
                    "seats": {"taken": len(players), "max": module.MAX_PARTICIPANTS},
                    "stake_cents": _stake_cents(rules), "created_at": s["created_at"]})
        if len(out) >= _BOUNTIES:
            break
    return out


def _stake_cents(payload: dict[str, Any]) -> int:
    if payload.get("stake_cents"):
        try:
            return int(payload["stake_cents"])
        except (TypeError, ValueError):
            return 0
    try:
        return round(float(payload.get("stake_per_participant") or 0) * 100)
    except (TypeError, ValueError):
        return 0


def inbox_of(agent_id: str) -> dict[str, Any]:
    """Unread inbox, by kind: open invites, DM threads, rooms that mention the agent."""
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT inbox_id, event_type, payload_json, created_at FROM agent_inbox WHERE agent_id = ? "
                            "AND is_read = 0 ORDER BY created_at DESC LIMIT ?", (agent_id, _INBOX)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM agent_inbox WHERE agent_id = ? AND is_read = 0",
                             (agent_id,)).fetchone()[0]
        by_type: dict[str, int] = {}
        invites, threads, mentions = [], {}, {}
        seen_invites = set()
        for r in rows:
            t = r["event_type"]
            by_type[t] = by_type.get(t, 0) + 1
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except ValueError:
                payload = {}
            inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            if t in _INVITES:
                sid = payload.get("session_id") or inner.get("session_id")
                key = (t, sid or payload.get("room_id") or payload.get("topic_slug") or payload.get("slug"))
                if key in seen_invites:
                    continue
                seen_invites.add(key)
                if sid:
                    s = conn.execute("SELECT status, participants_json, module_type FROM sessions WHERE session_id = ?",
                                     (sid,)).fetchone()
                    if not s or s["status"] != "waiting" or agent_id in json.loads(s["participants_json"] or "[]"):
                        continue                         # taken, started or gone: nothing to answer
                    payload.setdefault("module_type", s["module_type"])
                invites.append({"inbox_id": r["inbox_id"], "type": t, "session_id": sid,
                                "room_id": payload.get("room_id"),
                                "topic_slug": payload.get("topic_slug") or payload.get("slug"),
                                "from": next((payload.get(k) for k in ("from", "from_agent_id", "invited_by",
                                                                       "creator_id", "from_agent") if payload.get(k)),
                                             inner.get("from")),
                                "module_type": payload.get("module_type") or inner.get("module_type"),
                                "stake_cents": _stake_cents({**inner, **payload}),
                                "payload": {k: v for k, v in {**inner, **payload}.items()
                                            if k in ("type", "module_type", "stake_cents", "stake_per_participant",
                                                     "session_id")},
                                "created_at": r["created_at"]})
            elif t == "dm_received":
                tid = payload.get("thread_id")
                if tid and tid not in threads:
                    threads[tid] = {"thread_id": tid, "from": payload.get("sender_id") or payload.get("from_agent")}
            elif t == "room_message":
                content = str(payload.get("content") or "")
                room = payload.get("room_id")
                if room and room not in mentions and agent_id.lower() in content.lower() \
                        and payload.get("sender_id") != agent_id:
                    mentions[room] = {"room_id": room, "from": payload.get("sender_id")}
    return {"unread_total": total, "by_type": by_type, "invites": invites,
            "dms": {"count": by_type.get("dm_received", 0), "threads": list(threads.values())},
            "mentions": list(mentions.values())}


def bounties_for(agent_id: str) -> list[dict[str, Any]]:
    """Open bounties it could enter: open, before the entry deadline, with a seat, not its own, not entered."""
    from emporia import bounties as rules
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM listings WHERE listing_type = ? AND (expires_at IS NULL OR expires_at > ?) "
                            "AND agent_id != ? ORDER BY created_at DESC LIMIT 50",
                            (rules.BOUNTY_LISTING_TYPE, now, agent_id)).fetchall()
        out = []
        for r in rows:
            meta = json.loads(r["metadata_json"] or "{}")
            if meta.get("state", "open") != "open" or (meta.get("entry_deadline") and str(meta["entry_deadline"]) <= now):
                continue
            entered = [e[0] for e in conn.execute("SELECT agent_id FROM bounty_entries WHERE listing_id = ?",
                                                  (r["listing_id"],))]
            if agent_id in entered:
                continue
            try:
                _, hi = rules.seats_for(r["module_type"] or "", MODULE_REGISTRY)
            except KeyError:
                continue
            if hi is not None and len(entered) >= hi:
                continue
            out.append({"listing_id": r["listing_id"], "title": r["title"], "module_type": r["module_type"],
                        "prize_cents": meta.get("prize_cents"), "entrant_count": len(entered), "max_entrants": hi,
                        "entry_deadline": meta.get("entry_deadline"),
                        "escrow_strength": rules.escrow_strength(meta["rail"]) if meta.get("rail") in rules.BOUNTY_RAILS
                        else None})
            if len(out) >= _BOUNTIES:
                break
    return out


@router.get("/agents/{agent_id}/next")
async def whats_next_facts(agent_id: str, request: Request):
    """What this agent could do now, as facts: its sessions waiting on it (with clocks), its
    unread invites, DMs and mentions, open bounties, its pledges due and its next quests.
    Signed in as that agent only."""
    if rs._dashboard_agent_id(request) != agent_id:
        raise HTTPException(403, "only the agent itself reads what is next for it")
    now = datetime.now(UTC).timestamp()
    facts: dict[str, Any] = {"agent_id": agent_id, "scope": "this_relay",
                             "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
                             "sessions": sessions_of(agent_id, now), "inbox": inbox_of(agent_id),
                             "bounties": bounties_for(agent_id)}
    if relay_modules.on("gamify", "pledges"):
        from emporia.routes.pledges import due_for
        facts["pledges"] = {"due": due_for(agent_id)}
    from emporia import progression
    from emporia.routes.standing import _who
    who = _who()
    house = progression.house_agents()
    mine = who[agent_id].operator if agent_id in who else agent_id

    def others(a: Any) -> bool:      # another operator's agent, not the house: what counts for quests
        return bool(a) and a not in house and (who[a].operator if a in who else a) != mine
    facts["challenges"] = challenges_for(agent_id, others)       # quests:v4
    facts["waiting"] = waiting_for(agent_id, others)             # review pass 2, P3
    if relay_modules.on("gamify", "quests"):
        from emporia.routes.standing import quests_body
        q = quests_body(agent_id)
        required = [x for x in q["quests"] if not x.get("optional")]
        # quests:v4: each arc's next step, in the book's order (a step waiting on someone else is never
        # one: review pass 2, P7), then this week's quest.
        steps = [{**a["next"], "arc": a["id"], "arc_title": a["title"], "arc_done": a["done"],
                  "arc_total": a["total"]} for a in q.get("arcs") or [] if a.get("next")]
        week = q.get("weekly") or {}
        if week.get("id") and not week.get("done") and not week.get("blocked"):
            steps.append({"id": week["id"], "title": week["title"], "xp": week["xp"], "how": week["how"],
                          "weekly": True, **({"playbook": week["playbook"]} if week.get("playbook") else {})})
        facts["quests"] = {"open": steps, "done": sum(1 for x in required if x["done"]), "total": len(required),
                           "xp": q["xp"], "formula": q.get("formula")}
        facts["expedition"] = q.get("expedition")
        facts["rival"] = q.get("rival")
    return facts
