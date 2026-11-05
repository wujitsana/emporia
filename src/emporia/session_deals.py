"""The relay's side of a game that deals hidden hands (Bargain; module_sdk "Hidden deals").

The relay picks a secret seed when the game is made and publishes only its commitment, in
the game's config. It keeps the seed here, in `session_deals`, never in the session's state:
the state is served to every viewer, broadcast and copied into the action log. While the game
runs the hands are put into the state only for the relay's own calls into the module and
stripped again (`module_sdk.conceal`) before anything is stored or sent; each player reads
its own hand through `GET /sessions/{id}` (`private`). When the game ends — a result, the
clock, an abandon — the whole deal is written into the state, for anyone to re-deal and check.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from emporia.module_sdk import SessionState, deal_commitment, deal_seed, with_deal


def _rs():
    from emporia import relay_server as rs
    return rs


def new_seed() -> tuple[str, str]:
    """(relay_seed, its commitment) for a new game."""
    seed = secrets.token_hex(32)
    return seed, deal_commitment(seed)


def store(session_id: str, relay_seed: str, commitment: str) -> None:
    rs = _rs()
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO session_deals (session_id, relay_seed, commitment, created_at) VALUES (?, ?, ?, ?)",
                     (session_id, relay_seed, commitment, datetime.now(UTC).isoformat()))


def _row(session_id: str) -> Any:
    rs = _rs()
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT * FROM session_deals WHERE session_id = ?", (session_id,)).fetchone()


def set_contributions(session_id: str, contributions: dict[str, dict[str, str]]) -> None:
    rs = _rs()
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE session_deals SET contributions_json = ? WHERE session_id = ?",
                     (json.dumps(contributions, sort_keys=True), session_id))


def contributions(session_id: str) -> dict[str, dict[str, str]]:
    row = _row(session_id)
    return json.loads(row["contributions_json"] or "{}") if row else {}


def dealt(session_id: str, module, state: SessionState, config: dict[str, Any]) -> SessionState:
    """`state` with the deal filled in — the relay's seed, the game's seed and the hands —
    for the relay's own calls into the module. Unchanged when nothing has been dealt yet."""
    row = _row(session_id)
    deal = state.data.get("deal") or {}
    if row is None:
        return state
    players = list(state.metadata.get("participants") or [])
    contrib = deal.get("contributions") or {}
    if len(players) < module.MIN_PARTICIPANTS or any(p not in contrib for p in players):
        return with_deal(state, relay_seed=row["relay_seed"])     # not started: the seed alone
    seed = deal_seed(row["relay_seed"], [contrib[p]["signature"] for p in players])
    return with_deal(state, relay_seed=row["relay_seed"], seed=seed, hands=module.deal(seed, players, config))


def private_for(session: dict[str, Any], module, agent_id: str) -> dict[str, Any] | None:
    """A player's own hand, for it alone."""
    if agent_id not in (session.get("participants") or []):
        return None
    state = dealt(session["session_id"], module, SessionState.from_dict(session["state"]), session.get("config") or {})
    return module.private_view(state.data, agent_id)


def reveal(session_id: str, module) -> None:
    """Publish the deal into the stored state of a game that has ended. Idempotent."""
    rs = _rs()
    s = rs.load_session(session_id)
    row = _row(session_id)
    if s is None or row is None or s["status"] in ("waiting", "active", "awaiting_funding", "funding"):
        return
    state = dealt(session_id, module, SessionState.from_dict(s["state"]), s.get("config") or {})
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET state_json = ? WHERE session_id = ?",
                     (json.dumps(state.to_dict()), session_id))
        if not row["revealed_at"]:
            conn.execute("UPDATE session_deals SET revealed_at = ? WHERE session_id = ?",
                         (datetime.now(UTC).isoformat(), session_id))
