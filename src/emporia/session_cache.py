"""A signed-in session token kept on disk between process starts.

Every process of an agent (the MCP server of each chat turn, the gateway bridge, the SDK) used to sign in
again on start: a challenge and a session request each, and on a busy host a burst of them. The relay's token
is good for an hour, so a process that starts a minute after another can reuse the first one's.

The rule: the token is kept in the agent's own key folder (`identity.KEY_DIR`, which is per agent profile and
already holds its keys), in `sessions/<agent>-<relay hash>.json`, mode 0600 from the instant it exists, the
folder 0700. It is reused until `REUSE_MARGIN` seconds before it expires, dropped when the relay refuses it
(`forget`), and never logged or printed. One file per (agent, relay), so a token for one relay is never sent
to another. A corrupt or unreadable file is treated as no file.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

REUSE_MARGIN = 120.0     # seconds: a token with less than this left is replaced by a new sign-in


def _path(relay_url: str, agent_id: str) -> Path:
    from emporia import identity
    from emporia.wire import relay_origin
    try:
        origin = relay_origin(relay_url)
    except ValueError:
        origin = str(relay_url).rstrip("/").lower()
    tag = hashlib.sha256(origin.encode()).hexdigest()[:10]
    return identity.KEY_DIR / "sessions" / f"{identity._keyfile_stem(agent_id)}-{tag}.json"


def load(relay_url: str, agent_id: str, now: float | None = None) -> tuple[str, float, str] | None:
    """(token, expires_at, stable_id) when a kept token is still good for `REUSE_MARGIN` seconds, else None."""
    now = time.time() if now is None else now
    try:
        data = json.loads(_path(relay_url, agent_id).read_text())
        token, exp = data["token"], float(data["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(token, str) or not token or exp <= now + REUSE_MARGIN:
        return None
    return token, exp, str(data.get("stable_id") or "")


def store(relay_url: str, agent_id: str, token: str, expires_at: float, stable_id: str = "") -> None:
    """Keep a token (best effort: a failure to write means the next process signs in, nothing worse)."""
    from emporia import identity
    try:
        path = _path(relay_url, agent_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity._write_secret_atomically(path, json.dumps({"token": token, "expires_at": expires_at, "stable_id": stable_id}).encode())
    except OSError:
        return


def forget(relay_url: str, agent_id: str) -> None:
    """Drop the kept token (the relay refused it, or the agent signed out)."""
    try:
        _path(relay_url, agent_id).unlink(missing_ok=True)
    except OSError:
        return


__all__ = ["REUSE_MARGIN", "forget", "load", "store"]
