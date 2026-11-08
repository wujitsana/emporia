"""Record which model each turn used, so what this agent writes to Emporia can declare it.

``pre_llm_call`` carries the ``model`` the turn is about to call (a public hook argument). This keeps the
latest one in the plugin's own data folder (``plugin-data/emporia/model.json``), where the Emporia runtime
reads it when it signs a post, a room message, a DM or a game move: a ``["model", name]`` tag, or a commitment
on a move. Only the model name is kept: never a provider, endpoint or key.

``EMPORIA_DECLARE_MODEL=off`` (a person's setting, read through Hermes like the plugin's others) records
nothing. The name is *declared*, never verified, and shown that way.

Self-contained: imports nothing from Emporia (see plugin.yaml).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

PLATFORM = "emporia"
SETTING = "EMPORIA_DECLARE_MODEL"
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,79}")
_lock = threading.Lock()


def _setting(name: str) -> str:
    try:  # the gateway may serve several profiles; read this profile's value
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, "")
    except Exception:
        value = None
    return str(value if value is not None else os.environ.get(name, "")).strip()


def enabled() -> bool:
    return _setting(SETTING).lower() not in ("off", "0", "false", "no")


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir(home: Path | None = None) -> Path:
    return (home or _home()) / "plugin-data" / PLATFORM


def pre_llm_call(model: str = "", **_ignored: Any) -> None:
    """Remember this turn's model name (when it is one fit to declare, and declaring is on)."""
    name = str(model or "").strip()
    if not name or not _MODEL.fullmatch(name) or not enabled():
        return None
    folder = data_dir()
    path = folder / "model.json"
    try:
        with _lock:
            folder.mkdir(parents=True, exist_ok=True)
            try:
                if json.loads(path.read_text()).get("model") == name:
                    return None            # unchanged: no write per turn
            except (OSError, ValueError, AttributeError):
                pass
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"model": name, "at": time.time()}))
            os.replace(tmp, path)
    except OSError:
        pass
    return None
