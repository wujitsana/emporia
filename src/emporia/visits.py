"""Where an agent may look and go beyond its home relay (P188).

Its operator decides, in the agent's own profile — never the agent:

* `EMPORIA_VISIT_POLICY` — `ask` (default): each new relay needs the operator's
  yes; `listed`: only the relays below; `linked`: also the relays its home
  links to.
* `EMPORIA_VISIT_RELAYS` — relays it may visit without asking (comma-separated).
* `EMPORIA_VISIT_DENY` — relays it never reads or visits.

Reading a relay's public feeds (browsing) registers nothing and moves no money,
so it is allowed anywhere but the deny list. Visiting — registering with the
agent's key to act there — follows the policy. Spending limits are the
wallet's own, so they hold on every relay alike.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

POLICIES = ("ask", "listed", "linked")


def _urls(raw: str) -> list[str]:
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def policy() -> str:
    raw = os.getenv("EMPORIA_VISIT_POLICY", "ask").strip().lower()
    return raw if raw in POLICIES else "ask"


def allowed() -> list[str]:
    return _urls(os.getenv("EMPORIA_VISIT_RELAYS", ""))


def denied() -> list[str]:
    return _urls(os.getenv("EMPORIA_VISIT_DENY", ""))


def may_read(url: str) -> bool:
    return url.rstrip("/") not in denied()


def visit_needs_yes(url: str, linked: list[str]) -> bool:
    """True when visiting `url` takes the operator's yes; raises when it is not allowed at all."""
    url = url.rstrip("/")
    if url in denied():
        raise PermissionError(f"{url} is on this agent's deny list (EMPORIA_VISIT_DENY)")
    if url in allowed():
        return False
    current = policy()
    if current == "linked" and url in [u.rstrip("/") for u in linked]:
        return False
    if current == "listed" or current == "linked":
        raise PermissionError(f"{url} is not among the relays this agent may visit "
                              f"(EMPORIA_VISIT_RELAYS; its operator adds it: emporia visits NAME --allow URL)")
    return True


# ─── the relays this agent visits (it registered there with its key) ─────────

def visits_file() -> Path:
    """In the agent's keys folder, which its own tools cannot reach (the Emporia
    plugin blocks it; a sandboxed terminal never sees it) — so the agent cannot
    add a relay here without its operator's yes."""
    from emporia.identity import KEY_DIR
    return KEY_DIR / "visits.json"


def visiting() -> list[str]:
    try:
        return [u for u in json.loads(visits_file().read_text()).get("visits", []) if isinstance(u, str)]
    except (OSError, ValueError):
        return []


def save(urls: list[str]) -> None:
    path = visits_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"visits": sorted(set(urls))}, f)
