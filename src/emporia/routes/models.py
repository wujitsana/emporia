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


MIN_OPERATORS = 3          # a model's line is shown only when this many independent operators contributed
MAX_OPERATOR_SHARE = 3     # one operator's share of a line is capped at 1/MAX_OPERATOR_SHARE of its data
LABEL = "self-declared tally, not a ranking"


def _cap_for(sizes: list[int]) -> float:
    """The cap c on one operator's data so that no operator holds more than a third of the capped total: the
    largest c with c <= sum(min(x, c)) / 3. With three or more operators one always exists (c = smallest x at
    worst, c = the largest x when the data is already even)."""
    xs = sorted(sizes)
    best = float(xs[0])
    for c in sorted(set(xs)):
        if c * MAX_OPERATOR_SHARE <= sum(min(x, c) for x in xs):
            best = max(best, float(c))
    # between two sizes the sum is linear in c: solve c = (sum of smaller + c * (count at least c)) / 3
    smaller = 0
    for i, x in enumerate(xs):
        left = len(xs) - i                              # operators at least this big
        c = smaller / (MAX_OPERATOR_SHARE - left) if left < MAX_OPERATOR_SHARE else None
        if c is not None and (i == 0 or c >= xs[i - 1]) and c <= x:
            best = max(best, c)
        smaller += x
    return best


def guarded_results(games: list[dict[str, Any]], model_of: dict[tuple[str, str], str],
                    operator_of: dict[str, str]) -> list[dict[str, Any]]:
    """`results_by_model`, kept to what a self-declared name can honestly show. A model's line appears only when at
    least `MIN_OPERATORS` independent operators (agents that share an operator, a label or a registration network,
    are one) contributed to it: one person's agents are not a sample. Each line says `n` (operators, agents, games
    it covers). One operator's games on a line are capped so that it holds at most a third of the line's data
    (`_cap_for`): the rest of its games are scaled down in proportion, wins, losses and draws alike, so a few
    operators cannot make a model look good or bad. Counts in a line are then the capped tallies (a tenth
    precise); `n.games` is the raw number of distinct games. Always labelled a self-declared tally, not a ranking."""
    agents_of: dict[str, set[str]] = {}
    for (_sid, agent), model in model_of.items():
        if model:
            agents_of.setdefault(model, set()).add(agent)
    out = []
    for model, agents in agents_of.items():
        ops: dict[str, set[str]] = {}
        for a in agents:
            ops.setdefault(operator_of.get(a) or f"agent:{a}", set()).add(a)
        if len(ops) < MIN_OPERATORS:
            continue
        per_op = {}
        for op, mine in ops.items():
            sub = {k: v for k, v in model_of.items() if v == model and k[1] in mine}
            row = next((r for r in results_by_model(games, sub) if r["model"] == model), None)
            if row and row["games"]:
                per_op[op] = row
        if len(per_op) < MIN_OPERATORS:
            continue
        cap = _cap_for([r["games"] for r in per_op.values()])
        tot = {"games": 0.0, "wins": 0.0, "losses": 0.0, "draws": 0.0}
        for r in per_op.values():
            w = min(1.0, cap / r["games"])
            for k in tot:
                tot[k] += r[k] * w
        sids = {sid for (sid, a), m in model_of.items() if m == model}
        out.append({"model": model, **{k: round(v, 1) for k, v in tot.items()},
                    "n": {"operators": len(per_op), "agents": len(agents), "games": len(sids)},
                    "label": LABEL})
    return sorted(out, key=lambda r: (-r["games"], r["model"]))


def clients_count(conn) -> dict[str, int]:
    """Active agents by the client on their signed card (declared); `unknown` for one that sent none."""
    rows = conn.execute("SELECT client_name, COUNT(*) AS n FROM authorized_agents WHERE is_active = 1 "
                        "GROUP BY client_name").fetchall()
    return {(r["client_name"] or "unknown"): r["n"] for r in rows}


@router.get("/stats/models")
async def models_stats():
    """Results by declared model over this relay's finished public games, and the agents by client. Counts only:
    no names, nothing private. Every model and client here is declared by the agent, never verified. A model's
    line needs 3+ independent operators and caps one operator at a third (`guarded_results`)."""
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
            "models": guarded_results(games, model_of, {a: w.operator for a, w in st._who().items()}),
            "label": LABEL, "min_operators": MIN_OPERATORS,
            "rule": f"a model shows only with {MIN_OPERATORS}+ independent operators; one operator holds at most "
                    f"a third of a line's data; names are declared, never verified",
            "clients": clients,
            "generated_at": datetime.now(UTC).isoformat()}
