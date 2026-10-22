"""A Nous access token that is current when it is used (P114).

Nous access tokens last an hour. The installer wrote one into each profile's
config, so by the next day every agent registered without it — as `key_only`,
however recently its operator had signed in.

Hermes already keeps the login and refreshes it correctly: rotating refresh
tokens under a lock, and saving them back. Refreshing from here instead would
race Hermes for the same refresh token, and a reused one revokes the whole
session. So when the configured token has expired, this asks Hermes for a
current one, for this agent's own profile, and otherwise leaves the old one in
place (the relay keeps an agent's existing verification when a re-register
carries none — P110).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

_EXPIRY_SKEW_S = 60
_HERMES_TIMEOUT_S = 30
_RESOLVE = ("from hermes_cli.auth import resolve_nous_access_token as r; "
            "print(r())")


def expires_at(token: str) -> int | None:
    """The `exp` claim of a JWT, without verifying it — only to decide whether
    to ask for a new one. The relay verifies the token itself."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def is_current(token: str, *, now: float | None = None) -> bool:
    exp = expires_at(token) if token else None
    return exp is not None and exp > (now or time.time()) + _EXPIRY_SKEW_S


def profile_dir() -> Path | None:
    """This agent's Hermes profile: the directory of its own .env (P109), else
    HERMES_HOME."""
    dotenv = os.getenv("EMPORIA_DOTENV", "").strip()
    if dotenv:
        return Path(dotenv).expanduser().parent
    home = os.getenv("HERMES_HOME", "").strip()
    return Path(home).expanduser() if home else None


def _hermes_python() -> tuple[Path, str] | None:
    """Hermes's own interpreter and import path, as its `hermes` launcher sets
    them. None when Hermes is not installed here."""
    launcher = os.getenv("EMPORIA_HERMES_BIN", "").strip() or shutil.which("hermes")
    if not launcher:
        return None
    root = Path(os.path.realpath(launcher)).parent.parent
    python = root / ".venv" / "bin" / "python"
    sites = sorted(root.glob(".venv/lib/python3*/site-packages"))
    if not python.is_file() or not sites:
        return None
    return python, os.pathsep.join([str(root), str(sites[-1])])


def from_hermes(profile: Path | None = None) -> str | None:
    """A current token from Hermes for `profile`, or None if Hermes cannot give
    one (not installed, not signed in, or this profile's login was revoked)."""
    found = _hermes_python()
    if found is None:
        return None
    python, pythonpath = found
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PYTHONPATH"] = pythonpath
    if profile is not None:
        env["HERMES_HOME"] = str(profile)
    try:
        out = subprocess.run([str(python), "-c", _RESOLVE], env=env, capture_output=True,
                             text=True, timeout=_HERMES_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ""
    return token if expires_at(token) is not None else None


def current(configured: str = "") -> str:
    """`configured` while it is still valid; else a fresh one from Hermes; else
    `configured` (possibly expired, possibly empty)."""
    if is_current(configured):
        return configured
    return from_hermes(profile_dir()) or configured
