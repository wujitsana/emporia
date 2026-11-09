"""One status message per live game in the person's own chat (Hermes 0.23 ``ctx.post_message``).

Off unless the person turns it on (``EMPORIA_LIVE_STATUS=on``; `emporia live-status on`, or a setup question).

Two halves. The adapter, which sees each admitted game notice, calls ``record`` with the notice's ``status``
field: fixed fields the bridge took from the relay's own frame (game name, an agent id, a move number, a phase, a
result), validated again here. They go into ``plugin-data/emporia/live_games.json``. A person's own turn (a chat
or the CLI, never a relay-started or rounds turn) then calls ``refresh`` from ``pre_llm_call``: it posts one
message per live game with ``ctx.post_message`` and edits it in place on later turns, ending with the result.

What it can and cannot do, said plainly. Hermes posts only into the conversation that is taking a turn, so the
message appears and is edited when the person next writes to the agent there; it is a snapshot, not a ticker,
and a game's status is as of the last notice the agent was woken for (the wake mode decides which those are:
nothing is recorded for a notice the wake mode did not admit). Where ``post_message`` is missing (an older
Hermes) or refuses (no gateway, no conversation, called on the event loop) it does nothing, silently.

The wording is fixed here. No other agent's free text is ever in it, and neither is any secret.

Self-contained: imports nothing from Emporia.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from pathlib import Path

PLATFORM = "emporia"

SETTING = "EMPORIA_LIVE_STATUS"
_FILE = "live_games.json"
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,95}")
_GAME = re.compile(r"[a-z0-9-]{1,24}")
_AGENT = re.compile(r"[a-z0-9_-]{1,64}(?:@[a-z0-9.:-]{1,80})?")
_PHASES = ("started", "your_turn", "completed", "abandoned")
_RESULTS = {"won": "you won", "lost": "you lost", "ended": "no winner"}
_MAX_LIVE = 5               # at most this many status messages at once
_LIVE_TTL_S = 24 * 3600     # a game with no notice for a day is not shown
_DONE_TTL_S = 3600          # a finished game's last line stays this long
_lock = threading.Lock()
_ctx: Any = None
_handles: dict[tuple[str, str], tuple[Any, str]] = {}      # (session id, game key) -> (handle, text last shown)


def _setting(name: str) -> str:
    try:  # the gateway may serve several profiles; read this profile's value
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, "")
    except Exception:
        value = None
    return str(value if value is not None else os.environ.get(name, "")).strip()


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir() -> Path:
    return _home() / "plugin-data" / PLATFORM


def enabled() -> bool:
    return _setting(SETTING).lower() in ("on", "1", "true", "yes")


def bind(ctx: Any) -> None:
    """Keep the plugin context, whose ``post_message`` posts into the calling turn's conversation."""
    global _ctx
    _ctx = ctx


def _path():
    return data_dir() / _FILE


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean(status: Any) -> dict[str, Any] | None:
    """The status fields that meet the rules, or None: an id, a game name, a phase, a number, a result."""
    if not isinstance(status, dict):
        return None
    key, phase = status.get("key"), status.get("phase")
    if not isinstance(key, str) or not _KEY.fullmatch(key) or phase not in _PHASES:
        return None
    out: dict[str, Any] = {"key": key, "phase": phase}
    game, opp, step, result = status.get("game"), status.get("opponent"), status.get("step"), status.get("result")
    if isinstance(game, str) and _GAME.fullmatch(game):
        out["game"] = game
    if isinstance(opp, str) and _AGENT.fullmatch(opp):
        out["opponent"] = opp
    if isinstance(step, int) and not isinstance(step, bool) and 0 <= step < 10_000:
        out["step"] = step
    if result in _RESULTS:
        out["result"] = result
    return out


def record(status: Any, now: float | None = None) -> bool:
    """Note one admitted game notice's status (when live status is on). True when it was kept."""
    clean = _clean(status)
    if clean is None or not enabled():
        return False
    now = time.time() if now is None else now
    folder = data_dir()
    try:
        with _lock:
            folder.mkdir(parents=True, exist_ok=True)
            games = _load()
            old = games.get(clean["key"]) or {}
            merged = {**{k: old[k] for k in ("game", "opponent", "step") if k in old}, **clean, "at": now}
            games[clean["key"]] = merged
            games = _prune(games, now)
            tmp = _path().with_name(f".{_FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(games))
            os.replace(tmp, _path())
    except OSError:
        return False
    return True


def _prune(games: dict[str, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    keep = {}
    for key, g in games.items():
        done = g.get("phase") in ("completed", "abandoned")
        if isinstance(g, dict) and now - float(g.get("at") or 0) < (_DONE_TTL_S if done else _LIVE_TTL_S):
            keep[key] = g
    newest = sorted(keep, key=lambda k: keep[k].get("at") or 0, reverse=True)[:_MAX_LIVE]
    return {k: keep[k] for k in newest}


def render(game: dict[str, Any]) -> str:
    """The line for one game. Fixed wording around the validated fields."""
    name = game.get("game") or "game"
    opp = f" vs {game['opponent']}" if game.get("opponent") else ""
    phase = game.get("phase")
    if phase == "completed":
        tail = "finished, " + _RESULTS.get(game.get("result"), "no winner")
    elif phase == "abandoned":
        tail = "ended, abandoned"
    elif phase == "your_turn":
        tail = (f"move {game['step']}, " if "step" in game else "") + "your turn"
    else:
        tail = "started"
    return f"Emporia: {name}{opp} · {tail}"


def refresh(session_id: str) -> int:
    """Post or edit one message per live game in this turn's conversation. Returns how many it touched;
    never raises."""
    ctx = _ctx
    if ctx is None or not getattr(ctx, "post_message", None) or not enabled():
        return 0
    touched = 0
    try:
        games = _prune(_load(), time.time())
        for key, game in sorted(games.items()):
            text = render(game)
            slot = (session_id, key)
            known = _handles.get(slot)
            if known is None:
                handle = ctx.post_message(text)
                if not getattr(handle, "ok", False):
                    return touched          # no conversation, no gateway, older Hermes: nothing more to try
                _handles[slot] = (handle, text)
            elif known[1] != text:
                result = known[0].edit(text)
                if not (isinstance(result, dict) and result.get("ok")):
                    continue
                _handles[slot] = (known[0], text)
            else:
                continue
            touched += 1
        for slot in [s for s in _handles if s[0] == session_id and s[1] not in games]:
            _handles.pop(slot, None)
    except Exception:  # noqa: BLE001 — a status line must never break a turn
        return touched
    return touched


def pre_llm_call(platform: str = "", session_id: str = "", parent_session_id: str = "", user_message: Any = None,
                 **_ignored: Any) -> None:
    """A person's own turn: bring the status lines up to date. Not a relay-started turn (its conversation is
    Emporia's own), not a rounds turn, not a sub-agent's."""
    if not session_id or parent_session_id or (platform or "").lower() == PLATFORM or not enabled():
        return None
    if str(user_message or "").lstrip().startswith("[emporia-rounds "):     # a rounds turn (rounds_marker.py)
        return None
    refresh(session_id)
    return None
