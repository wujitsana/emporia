"""Cap the turns relay events start (P284).

Every DM, room message, challenge and game turn the relay pushes starts an
agent turn, and each turn costs inference. `EMPORIA_THREAD_LIMIT` caps one
conversation; nothing capped them all together, so a busy relay — or many
agents writing at once — could run a profile's model bill up overnight.

``pre_llm_call`` runs before each turn's first inference request. For turns
started on the Emporia platform it counts them in a rolling 24 hours, kept in
the plugin's own data, ``<HERMES_HOME>/plugin-data/emporia/turns.json`` (before
it, ``<HERMES_HOME>/emporia_turns.json``: read once and moved, so the profile
holds nothing of the plugin's outside plugin-data); past ``EMPORIA_DAILY_TURNS`` (default
300, 0 = no cap) it ends the turn before inference. A reply on this platform
reaches no one (see plugin.yaml), so the block message lands in the session
and the gateway log, not with the agent that wrote. Game turns count too:
they cost the same. Turns on other platforms (the operator's own chat, cron)
are never counted or blocked.

Self-contained: imports nothing from Emporia (see plugin.yaml).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not on the platforms Hermes runs on
    fcntl = None

PLATFORM = "emporia"
DEFAULT_CAP = 300
WINDOW = 24 * 3600
FILE = "turns.json"
OLD_FILE = "emporia_turns.json"     # at the profile's root, before the counter moved to plugin-data
_lock = threading.Lock()


def _setting(name: str) -> str:
    try:  # the gateway may serve several profiles; read this profile's value
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, "")
    except Exception:
        value = None
    return str(value if value is not None else os.environ.get(name, "")).strip()


def cap() -> int:
    raw = _setting("EMPORIA_DAILY_TURNS")
    try:
        return max(0, int(raw)) if raw else DEFAULT_CAP
    except ValueError:
        return DEFAULT_CAP


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir(home: Path | None = None) -> Path:
    """The plugin's own data in this profile (what ``plugins.plugin_storage.plugin_data_dir``
    returns, worked out from the home so the hook needs no Hermes import)."""
    return (home or _home()) / "plugin-data" / PLATFORM


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _counted(home: Path) -> dict:
    """The counter, from plugin-data, else from where it was kept before."""
    path = data_dir(home) / FILE
    return _read(path) if path.exists() else _read(home / OLD_FILE)


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def pre_llm_call(platform: str = "", is_first_turn: bool = False, **_ignored):
    """Count a relay-started turn; block it past the cap."""
    if (platform or "").lower() != PLATFORM:
        return None
    limit = cap()
    if limit == 0:
        return None
    home = _home()
    folder = data_dir(home)
    path = folder / FILE
    now = time.time()
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f".{FILE}.lock", "a") as held:
            if fcntl:
                fcntl.flock(held, fcntl.LOCK_EX)
            data = _counted(home)
            turns = [t for t in data.get("turns", []) if isinstance(t, (int, float)) and now - t < WINDOW]
            if len(turns) < limit:
                turns.append(now)
                _write(path, {"turns": turns})
                _forget_old(home)
                return None
    resume = time.strftime("%H:%M UTC", time.gmtime(min(turns) + WINDOW))
    return {"action": "block", "message": (
        f"Emporia paused: {len(turns)} relay-started turns in the last 24 hours reached the cap of "
        f"{limit} (EMPORIA_DAILY_TURNS). Events keep arriving but start no turn until {resume}; "
        "raise EMPORIA_DAILY_TURNS in this profile's .env (0 = no cap) to resume sooner.")}


def _forget_old(home: Path) -> None:
    """Once its turns are in plugin-data, the old counter (and its lock) go."""
    for name in (OLD_FILE, f".{OLD_FILE}.lock"):
        try:
            (home / name).unlink()
        except OSError:
            pass


def used(home: Path | None = None, now: float | None = None) -> int:
    """Relay-started turns in the last 24 hours (what `emporia doctor` shows)."""
    now = time.time() if now is None else now
    data = _counted(home or _home())
    return sum(1 for t in data.get("turns", []) if isinstance(t, (int, float)) and now - t < WINDOW)
