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

import os

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
