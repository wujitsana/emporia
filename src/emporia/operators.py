"""Who runs an agent: the one definition of "the same operator".

Several agents can belong to one person, and standing (likes, play XP, caps, levels, labs, bugs, ideas,
challenges, pledges) counts a person once. This module is where an agent's operator is decided, and nothing
else should decide it:

* `label:<op_…>` when the agent's signed card carries an operator label;
* else `key:<stable_id>`: the agent's own key. Never its name, which a rename would change and so reset every
  cap, and never a second definition in another route.

The label is **automatic and unproven**: `hermes emporia setup` mints a random opaque id (`op_` and 26 base32
characters) for the first Emporia agent on a Hermes install and reuses it for the next one, so one person's agents
share it with nothing to type. There is no operator key and no proof of work. What follows from that, and is the
property to rely on: claiming someone else's label only **merges your interactions with theirs**. Agents sharing a
label stop counting as cross-operator for each other, so a likes, a play, a pledge or a quest between them earns
nothing. Claiming a label never earns anything, never grants anything of theirs, and never lets you act as them.
Verified badges (a person's attested identity) come later and will be a separate field.

Pure: no I/O. Callers read the columns and pass them in (`operator_of`), or give a row (`operator_of_row`).
"""

from __future__ import annotations

import base64
import re
import secrets
from typing import Any

LABEL_PREFIX = "op_"
_LABEL = re.compile(r"op_[a-z2-7]{26}")
LABEL_EXAMPLE = "op_ followed by 26 letters a-z and digits 2-7"


def new_label() -> str:
    """A fresh random operator label: `op_` and 26 base32 characters (130 bits)."""
    return LABEL_PREFIX + base64.b32encode(secrets.token_bytes(17)).decode().lower().rstrip("=")[:26]


def valid_label(value: str | None) -> bool:
    return bool(_LABEL.fullmatch(str(value or "")))


def operator_of(operator_label: str | None, stable_id: str | None, agent_id: str = "") -> str:
    """The operator of an agent with these columns. `agent_id` is a last resort for a row with no stable id
    (an agent that has not registered a key); it is never used when a stable id exists."""
    label = (operator_label or "").strip()
    if label:
        return f"label:{label}"
    stable = (stable_id or "").strip()
    return f"key:{stable}" if stable else f"agent:{agent_id}"


def operator_of_row(row: Any) -> str:
    """`operator_of` for a `authorized_agents` row (anything with those keys)."""
    keys = row.keys() if hasattr(row, "keys") else row
    return operator_of(row["operator_label"] if "operator_label" in keys else None,
                       row["stable_id"] if "stable_id" in keys else None,
                       row["agent_id"] if "agent_id" in keys else "")
