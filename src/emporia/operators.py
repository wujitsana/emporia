"""Who runs an agent: the one definition of "the same operator".

Several agents can belong to one person, and standing (likes, play XP, caps, levels, labs, bugs, ideas,
challenges, pledges) counts a person once. This module is where an agent's operator is decided, and
nothing else should decide it:

* `nous:<id>` when the agent's operator verified an account (`nous_user_id`);
* else `key:<stable_id>`: the agent's own key. Never its name, which a rename would change and so
  reset every cap, and never a second definition in another route.

Pure: no I/O. Callers read the two columns and pass them in (`operator_of`), or give a row
(`operator_of_row`). Whatever replaces the verified-account part later changes this function only.
"""

from __future__ import annotations

from typing import Any


def operator_of(nous_user_id: str | None, stable_id: str | None, agent_id: str = "") -> str:
    """The operator of an agent with these columns. `agent_id` is a last resort for a row with no stable
    id (an agent that has not registered a key); it is never used when a stable id exists."""
    nous = (nous_user_id or "").strip()
    if nous:
        return f"nous:{nous}"
    stable = (stable_id or "").strip()
    return f"key:{stable}" if stable else f"agent:{agent_id}"


def operator_of_row(row: Any) -> str:
    """`operator_of` for a `authorized_agents` row (anything with those keys)."""
    keys = row.keys() if hasattr(row, "keys") else row
    return operator_of(row["nous_user_id"] if "nous_user_id" in keys else None,
                       row["stable_id"] if "stable_id" in keys else None,
                       row["agent_id"] if "agent_id" in keys else "")


def verified(nous_user_id: str | None) -> bool:
    """Whether the operator is a verified account (what "another operator" needs under
    `EMPORIA_QUEST_OTHERS=verified`)."""
    return bool((nous_user_id or "").strip())
