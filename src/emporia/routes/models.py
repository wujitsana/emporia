"""Declared models: the end-of-game reveal, a game's models, and results by declared model.

A player that declared a model committed to it with each move (`model_commit`); here it reveals what it
committed, signed, once the game is over. The relay checks each reveal against the commit it stored and the record
shows the result: `revealed`, `mismatch` (a reveal that does not match its commit), `missing` (committed, not
revealed) or `none` (declared nothing). The outcome of a game never depends on any of it, and every model name here
is *declared*, never verified. See `emporia.model_commit`.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import model_commit as mc
from emporia import relay_server as rs
from emporia import wire

router = APIRouter()
_ENDED = ("completed", "abandoned")


class ModelRevealRequest(BaseModel):
    reveals: list[dict[str, Any]]
    at: str
    signature: str


def _player_standing(conn, session_id: str, stable_id: str) -> tuple[str, str, int, int]:
    """(status, model, steps committed, steps revealed ok) of one player on one game."""
    commits = {r["step"]: r["model_commit"] for r in conn.execute(
        "SELECT step, model_commit FROM model_commits WHERE session_id = ? AND stable_id = ?",
        (session_id, stable_id)).fetchall()}
    revealed = conn.execute(
        "SELECT step, model, status FROM model_reveals WHERE session_id = ? AND stable_id = ?",
        (session_id, stable_id)).fetchall()
    checked = {r["step"]: ("ok" if r["status"] == "ok" else r["status"]) for r in revealed}
    status = mc.standing(commits, checked)
    model = mc.most_revealed({r["step"]: r["model"] for r in revealed if r["status"] == "ok"}) if status == mc.REVEALED else ""
    return status, model, len(commits), sum(1 for v in checked.values() if v == "ok")


@router.post("/sessions/{session_id}/model-reveal")
async def reveal_models(session_id: str, req: ModelRevealRequest, request: Request):
    """A player's signed reveal of the models it committed with its moves, once the game is over."""
    caller = rs._dashboard_agent_id(request)
    if not caller:
        raise HTTPException(401, "Authentication required")
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if caller not in s["participants"]:
        raise HTTPException(403, "Only a player in this game reveals its models")
    if s["status"] not in _ENDED:
        raise HTTPException(409, "a game's models are revealed when it is over, not before")
    if why := mc.reveals_problem(req.reveals):
        raise HTTPException(400, why)
    from emporia.routes.agents import _signed_at
    _signed_at(req.at)
    stable = rs.AGENT_REGISTRY.get_stable_id(caller) or ""
    if not rs.AGENT_REGISTRY.verify_signature(
            caller, wire.signed_model_reveal_payload(session_id, stable, req.reveals, req.at), req.signature):
        raise HTTPException(403, "the reveal's signature does not verify against this agent's key")
    rs._enforce_agent_rate_limit(caller)
    with rs.DB_LOCK, rs.get_db() as conn:
        commits = {r["step"]: r["model_commit"] for r in conn.execute(
            "SELECT step, model_commit FROM model_commits WHERE session_id = ? AND stable_id = ?",
            (session_id, stable)).fetchall()}
        checked = mc.check_reveals(commits, req.reveals)
        for r in req.reveals:
            if checked[int(r["step"])] == "unknown":
                continue
            conn.execute("INSERT OR IGNORE INTO model_reveals (session_id, step, stable_id, model, salt, status, at, "
                         "signature) VALUES (?,?,?,?,?,?,?,?)",
                         (session_id, int(r["step"]), stable, r["model"], r["salt"], checked[int(r["step"])],
                          req.at, req.signature))
        status, model, committed, ok = _player_standing(conn, session_id, stable)
    return {"session_id": session_id, "status": status, "model": model or None, "committed": committed,
            "revealed": ok, "steps": {str(k): v for k, v in checked.items()}, "declared": True}


@router.get("/sessions/{session_id}/models")
async def game_models(session_id: str, request: Request):
    """What each player of a game declared, as the record shows it. While the game is on, only whether a model was
    committed; once it is over, the revealed models, and any reveal missing or not matching, said so."""
    s = rs.load_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if not rs._session_viewer_allowed(s, rs._dashboard_agent_id(request)):
        raise HTTPException(403, "Not a participant in this session")
    ended = s["status"] in _ENDED
    players: dict[str, Any] = {}
    with rs.DB_LOCK, rs.get_db() as conn:
        for agent in s["participants"]:
            stable = rs.AGENT_REGISTRY.get_stable_id(agent) or ""
            status, model, committed, ok = _player_standing(conn, session_id, stable)
            if not ended:
                status = "committed" if committed else mc.NONE
            players[agent] = {"status": status, "model": model or None, "committed_steps": committed,
                              "revealed_steps": ok, "declared": True}
    return {"session_id": session_id, "ended": ended, "players": players, "declared": True,
            "note": "model names are declared by the players and never verified; a missing or mismatched reveal "
                    "never changes the outcome"}


def results_by_model(games: list[dict[str, Any]], model_of: dict[tuple[str, str], str]) -> list[dict[str, Any]]:
    """Wins, losses and draws per declared model over finished public games. `games` are `_craft_games()` rows;
    `model_of[(session_id, agent)]` the model that player revealed (matching its commits). A player with no
    revealed model is not counted. A game with a winner is a win for that player and a loss for the others; a game
    with none and not abandoned is a draw; in an abandoned game only the one who walked away is counted (a loss)."""
    tally: dict[str, Counter] = {}
    for g in games:
        for agent in g.get("participants") or []:
            model = model_of.get((g["session_id"], agent))
            if not model:
                continue
            t = tally.setdefault(model, Counter())
            if g.get("abandoned_by"):
                if g["abandoned_by"] == agent:
                    t["games"] += 1
                    t["losses"] += 1
                continue
            t["games"] += 1
            if g.get("winner") == agent:
                t["wins"] += 1
            elif g.get("winner"):
                t["losses"] += 1
            else:
                t["draws"] += 1
    return [{"model": m, "games": t["games"], "wins": t["wins"], "losses": t["losses"], "draws": t["draws"]}
            for m, t in sorted(tally.items(), key=lambda kv: (-kv[1]["games"], kv[0]))]


def clients_count(conn) -> dict[str, int]:
    """Active agents by the client on their signed card (declared); `unknown` for one that sent none."""
    rows = conn.execute("SELECT client_name, COUNT(*) AS n FROM authorized_agents WHERE is_active = 1 "
                        "GROUP BY client_name").fetchall()
    return {(r["client_name"] or "unknown"): r["n"] for r in rows}


@router.get("/stats/models")
async def models_stats():
    """Results by declared model over this relay's finished public games, and the agents by client. Counts only:
    no names, nothing private. Every model and client here is declared by the agent, never verified."""
    from emporia.routes import standing as st
    games = st._craft_games()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT r.session_id, a.agent_id, r.step, r.model FROM model_reveals r "
            "JOIN authorized_agents a ON a.stable_id = r.stable_id WHERE r.status = 'ok'").fetchall()
        clients = clients_count(conn)
    by_game: dict[tuple[str, str], dict[int, str]] = {}
    for r in rows:
        by_game.setdefault((r["session_id"], r["agent_id"]), {})[r["step"]] = r["model"]
    model_of = {k: mc.most_revealed(v) for k, v in by_game.items()}
    return {"scope": "finished public games on this relay", "declared": True,
            "models": results_by_model(games, model_of), "clients": clients,
            "generated_at": datetime.now(UTC).isoformat()}
