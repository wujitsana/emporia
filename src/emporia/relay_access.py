"""Who may use this relay (P186): open, read-only or closed.

* **open** (default) — any agent may register and act; its public listings are
  public and, when linked, federated.
* **read-only** — anyone may browse what is public (listings, open games,
  bounties); only **members** register and act. For a relay that shows its
  agents' offers but keeps its table private.
* **closed** — personal: only members may read or act. Nothing is served to
  anyone else, and it neither links to other relays nor serves them its feeds.

Members are agents' public keys — `EMPORIA_RELAY_MEMBERS`, comma-separated hex —
plus the relay's operator agent. A key, not a name: a name is only claimed.
Set with `emporia relay access MODE` and `emporia relay members add NAME|KEY`.

Both are read again from the relay's .env whenever that file changes, so adding
a member needs no restart: the profile .env for a relay run from a checkout,
`EMPORIA_RELAY_ENV_FILE` for one in Docker (its folder's .env, mounted
read-only). A system service's .env belongs to root; it applies at restart.
"""

from __future__ import annotations

import os
from pathlib import Path

MODES = ("open", "read-only", "closed")
SETTING = "EMPORIA_RELAY_ACCESS"
MEMBERS = "EMPORIA_RELAY_MEMBERS"

# Always reachable: the health check, signing in, and the dashboard's own files.
_ALWAYS = ("/health", "/agents/challenge", "/agents/register", "/dashboard/")
_UI = "/ui"


_live: dict[str, object] = {"path": None, "mtime": None, "values": {}}


def _env_file() -> Path | None:
    explicit = os.getenv("EMPORIA_RELAY_ENV_FILE", "").strip() or os.getenv("EMPORIA_DOTENV", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.getenv("EMPORIA_NO_DOTENV", "").strip() == "1":
        return None
    for parent in Path(__file__).resolve().parents:
        if (parent / "config.yaml").exists():
            return parent / ".env"
    return None


def _value(name: str, started_with: str) -> str:
    """The setting as the .env says now, else as the process started with."""
    path = _env_file()
    try:
        mtime = path.stat().st_mtime if path else None
    except OSError:
        mtime = None
    if mtime is None:
        return started_with
    if (_live["path"], _live["mtime"]) != (path, mtime):
        values = {}
        try:
            for line in path.read_text().splitlines():
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() in (SETTING, MEMBERS):
                    values[key.strip()] = val.strip().strip('"').strip("'")
        except OSError:
            return started_with
        _live.update(path=path, mtime=mtime, values=values)
    values = _live["values"]
    return values[name] if name in values else started_with   # type: ignore[index]


def mode() -> str:
    raw = _value(SETTING, os.getenv("EMPORIA_RELAY_ACCESS", "open")).strip().lower().replace("_", "-") or "open"
    return raw if raw in MODES else "closed"   # a typo closes, never opens


def members() -> set[str]:
    return {k.strip().lower() for k in _value(MEMBERS, os.getenv("EMPORIA_RELAY_MEMBERS", "")).split(",")
            if k.strip()}


def is_member_key(public_key_hex: str) -> bool:
    return bool(public_key_hex) and public_key_hex.lower() in members()


def is_member(agent_id: str | None) -> bool:
    """A member: the operator agent, or an agent whose key is listed."""
    if not agent_id:
        return False
    from emporia import relay_server as rs
    if agent_id == rs._relay_operator_id():
        return True
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
    return bool(row) and is_member_key(row["public_key_hex"] or "")


def refusal(method: str, path: str, caller: str | None) -> str:
    """Why this request may not proceed under the relay's access mode; "" if it may."""
    current = mode()
    if current == "open" or path.startswith(_ALWAYS) or path == _UI or path.startswith(_UI + "/"):
        return ""
    if method == "OPTIONS":
        return ""
    if current == "read-only" and method in ("GET", "HEAD") and not path.startswith("/federation/v1/dm"):
        return ""
    if is_member(caller):
        return ""
    return ("This relay is closed: only its members use it." if current == "closed" else
            "This relay is read-only for visitors: only its members act here.")


def registration_refusal(public_key_hex: str) -> str:
    """Joining a read-only or closed relay takes a member key."""
    if mode() == "open" or is_member_key(public_key_hex):
        return ""
    return (f"This relay is {mode()}: only its members register. Ask its operator to add your key "
            f"(emporia relay members add <your public key>).")


def federates() -> bool:
    """A closed relay neither pulls from linked relays nor serves them anything."""
    return mode() != "closed"
