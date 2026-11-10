"""What this agent has left under its own limits, read-only (`standing(view="limits")`).

An agent that decides like a player weighs what it can finish before it accepts: a game it would have to
abandon, a paid room over its spend left. Every number here is read from the guard that enforces it, never
restated: the playbook (games per operator a day), the wallet (per-payment cap, daily cap, approval
threshold, spent today), the autonomy guard (writes per unattended turn, the unattended spending allowance),
the plugin's turn counter (relay-started turns a day) and the open-games cap. Nothing here changes a limit.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from emporia import autonomy


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def spend(agent_id: str) -> dict[str, Any]:
    """The wallet's caps and today's spend, in cents. `left_today_cents` is what the daily cap still allows."""
    from emporia import agent_wallet
    out: dict[str, Any] = {}
    try:
        daily = agent_wallet.max_daily_cents()
        spent = agent_wallet.spent_today_atomic(agent_id) // 10_000
        out.update(per_payment_cap_cents=agent_wallet.max_payment_cents(), daily_cap_cents=daily,
                   spent_today_cents=spent, left_today_cents=max(0, daily - spent),
                   approval_above_cents=agent_wallet.approval_threshold_cents())
    except Exception as e:  # noqa: BLE001 — a wallet that cannot be read is said so, not guessed
        out["error"] = f"wallet not readable: {e}"
    allowed = autonomy.spend_allowed_cents()
    out["unattended_allowance_cents"] = allowed
    if allowed:
        used = autonomy.spent_today_atomic() // 10_000
        out["unattended_left_today_cents"] = max(0, allowed - used)
    return out


def writes() -> dict[str, Any]:
    """Writes an unattended turn may make, and what this turn has left of them (None: a person's turn)."""
    out: dict[str, Any] = {"per_unattended_turn": autonomy.MAX_WRITES_PER_TURN,
                           "new_dm_strangers_per_unattended_turn": autonomy.MAX_NEW_DMS_PER_TURN,
                           "new_rooms_or_topics_per_day": autonomy.MAX_NEW_ROOMS_PER_DAY,
                           "left_this_turn": None}
    turn = autonomy.current()
    folder = autonomy.data_dir()
    if turn.autonomous and turn.key and folder is not None:
        entry = ((autonomy._read_json(folder / autonomy.STATE_FILE, {}).get("turns") or {}).get(turn.key)) or {}
        out["left_this_turn"] = max(0, autonomy.MAX_WRITES_PER_TURN - int(entry.get("writes", 0) or 0))
    return out


def turns(now: float | None = None) -> dict[str, Any]:
    """Relay-started turns used in the last 24 hours against EMPORIA_DAILY_TURNS (0 = no cap). (The per-conversation
    wake limit is the gateway's, not this server's: it is not read here.)"""
    from emporia import analytics
    cap = _int_env("EMPORIA_DAILY_TURNS", 300)
    out: dict[str, Any] = {"daily_cap": cap}
    dotenv = os.getenv("EMPORIA_DOTENV", "").strip()
    if dotenv:
        used = analytics.turns_used(Path(dotenv).expanduser().parent, time.time() if now is None else now)
        out.update(used_today=used, left_today=max(0, cap - used) if cap else None)
    return out


def snapshot(agent_id: str) -> dict[str, Any]:
    """Every limit with what is left, for `standing(view="limits")`."""
    from emporia import playbook
    pb = playbook.current()
    open_cap = _int_env("EMPORIA_MAX_OPEN_GAMES", 3)
    return {
        "games_per_operator_per_day": pb.games_per_operator_per_day,
        "open_games_cap": open_cap,
        "spend": spend(agent_id),
        "writes": writes(),
        "turns": turns(),
        "note": ("a game you cannot finish within these is worse than declining it; a paid room over your "
                 "spend left is refused anyway. Your operator changes limits, not you."),
    }
