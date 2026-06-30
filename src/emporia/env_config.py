"""Environment variable access for Emporia (EMPORIA_* prefix)."""
from __future__ import annotations

import os


def env(name: str, default: str = "") -> str:
    """Read EMPORIA_<name> or return default."""
    return os.getenv(f"EMPORIA_{name}", default)


def env_int(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = env(name, str(default))
    try:
        return float(raw)
    except ValueError:
        return default