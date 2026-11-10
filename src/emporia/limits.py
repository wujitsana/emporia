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


#: Notices that can start a turn: each gets the agent's `limits:` line.
LINE_FRAMES = frozenset({"challenge", "session_invite", "your_turn", "session_started", "dm_received",
                         "room_message", "game_chat", "agora_post_created"})
_CONVERSATIONS = ("dm:", "room:", "agora:")


def _fetch_games_today(agent_id: str, other: str) -> int | None:
    """One relay read: games this agent has played today against `other`'s operator."""
    from emporia import mcp_server as m
    r = m._bearer_request("get", f"{m._base()}/agents/{m._seg(agent_id)}/games-today", agent_id=agent_id,
                          params={"with_agent": other}, timeout=5)
    return int(r.json()["games_today"]) if r.status_code == 200 else None


class LimitsLine:
    """The one `limits:` line the bridge appends to a notice that can start a turn, from this agent's own numbers:
    games against that operator today (the invite carries the count; otherwise one relay read, cached), the spend
    left under its wallet's daily cap (and its approval threshold), writes per turn, and messages in this chat this
    hour (counted here, as the adapter counts them). A part that is not known or does not apply is left out, and
    a read that fails is not retried for TTL seconds. Nothing here is another agent's number."""

    TTL = 60.0

    def __init__(self, agent_id: str, fetch=None, clock=time.time, mono=time.monotonic) -> None:
        self.agent_id = agent_id
        self._fetch = fetch or _fetch_games_today
        self._clock, self._mono = clock, mono
        self._cache: dict[str, tuple[float, Any]] = {}
        self._seen: dict[str, list[float]] = {}

    def _cached(self, key: str, make):
        now = self._clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.TTL:
            return hit[1]
        try:
            value = make()
        except Exception:  # noqa: BLE001 — unknown: the part is left out
            value = None
        self._cache[key] = (now, value)
        return value

    def _games(self, frame: dict[str, Any]) -> str:
        from emporia import playbook
        from emporia.wire import agent_ref
        name = agent_ref(frame.get("from_agent_id") or frame.get("from_agent"), bare_only=True)
        if not name:
            return ""
        played = frame.get("games_with_you_today")
        if not isinstance(played, int) or isinstance(played, bool) or played < 0:
            played = self._cached(f"games:{name}", lambda: self._fetch(self.agent_id, name))
        if played is None:
            return ""
        cap = playbook.current().games_per_operator_per_day
        return f"games vs {name} today {played}/{cap}" if cap else f"games vs {name} today {played}"

    def _spend(self) -> str:
        def read():
            from emporia import agent_wallet
            agent_wallet.address(self.agent_id)     # no wallet, no spend part
            return spend(self.agent_id)
        s = self._cached("spend", read)
        if not isinstance(s, dict) or s.get("left_today_cents") is None:
            return ""
        out = f"spend left ${s['left_today_cents'] / 100:.2f} today"
        if s.get("approval_above_cents") is not None:
            out += f" (approval over ${s['approval_above_cents'] / 100:.2f})"
        return out

    def _chat(self, topic: str, sender: str) -> str:
        from emporia import wake
        limit = wake.thread_limit()
        if limit <= 0 or sender == "emporia:relay" or not topic.startswith(_CONVERSATIONS):
            return ""
        now = self._mono()
        seen = [t for t in self._seen.get(topic, []) if now - t < 3600.0]
        seen.append(now)
        self._seen[topic] = seen
        return f"this chat {len(seen)}/{limit} this hour"

    def line(self, frame: dict[str, Any], frame_type: str, sender: str, topic: str = "", relay: str = "") -> str:
        """The line (without a newline), or "" when the notice takes none or nothing is known. Call once per
        notice that is passed on: it counts the chat."""
        if frame_type not in LINE_FRAMES:
            return ""
        parts = []
        if not relay and frame_type in ("challenge", "session_invite"):
            parts.append(self._games(frame))
        parts.append(self._spend())
        if sender != "emporia:relay" and not sender.startswith("emporia:relay@"):
            parts.append(f"writes {autonomy.MAX_WRITES_PER_TURN} per turn")
        parts.append(self._chat(topic, sender))
        text = " · ".join(p for p in parts if p)
        return f"limits: {text}"[:160] if text else ""


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
