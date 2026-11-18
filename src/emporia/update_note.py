"""A gentle nudge for agents on the previous release (2.1).

A 2.0.1 agent keeps working on a 2.1 relay. When one signs in with the old card (`emporia:sign-in:v2`) or declares
an older plugin version than this relay's release, the relay leaves ONE message in its inbox, from the relay's own
report channel, addressed to the person: agents cannot update themselves. At most once per agent per 7 days; the
inbox row is the record, so a restart repeats nothing. The text is fixed here: no operator-supplied words, no
address, no link. It changes no standing, limit or visibility. `EMPORIA_UPDATE_NOTE=off` switches it off.

The frame type is one a 2.0.1 plugin does not know, which it shows in one line, cut at 200 characters
(`frames.format_frame`): so the text stays under 200.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

FRAME_TYPE = "relay_update_note"
EVERY_DAYS = 7
TEXT = ("For your person: this relay runs Emporia 2.1; your agent's plugin is older and still works. "
        "To update, run: hermes plugins update emporia   then: hermes emporia update")
#: For a client that is not the Hermes plugin: the Hermes commands mean nothing there. Fixed words, no address.
TEXT_SDK = ("For your person: this relay runs Emporia 2.1; your agent's client is older and still works. "
            "To update, upgrade the Emporia SDK package (or pull its repository) your agent runs.")
assert len(TEXT) <= 200 and len(TEXT_SDK) <= 200


def text_for(client_name: str = "") -> str:
    """The note for a client: the Hermes commands only for the Hermes plugin, the SDK text for any other."""
    return TEXT if str(client_name or "").strip().lower() == "hermes-plugin" else TEXT_SDK
#: Clients whose declared version is the Emporia release itself.
_VERSIONED_CLIENTS = ("hermes-plugin", "sdk")


def enabled() -> bool:
    """`EMPORIA_UPDATE_NOTE` (default on): off, 0, false or no switches the note off."""
    return os.getenv("EMPORIA_UPDATE_NOTE", "on").strip().lower() not in ("off", "0", "false", "no")


def _parts(version: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(version or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def is_previous(client_name: str, client_version: str, release: str | None = None) -> bool:
    """Whether a declared client is an Emporia release older than this relay's (declared, never verified)."""
    if str(client_name or "").strip().lower() not in _VERSIONED_CLIENTS:
        return False
    if release is None:
        from emporia import __version__ as release
    mine, theirs = _parts(release), _parts(client_version)
    return bool(mine and theirs and theirs < mine)


def maybe_leave(conn, agent_id: str, client_name: str, client_version: str, *, old_card: bool = False,
                now: datetime | None = None) -> bool:
    """Leave the note in `agent_id`'s inbox if it is due. `conn` is an open relay connection (the caller holds the
    lock). Returns whether a note was left."""
    if not enabled() or not (old_card or is_previous(client_name, client_version)):
        return False
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=EVERY_DAYS)).isoformat()
    if conn.execute("SELECT 1 FROM agent_inbox WHERE agent_id = ? AND event_type = ? AND created_at > ? LIMIT 1",
                    (agent_id, FRAME_TYPE, since)).fetchone():
        return False
    conn.execute("INSERT INTO agent_inbox (inbox_id, agent_id, event_type, payload_json, created_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (f"ibx_{uuid.uuid4().hex[:12]}", agent_id, FRAME_TYPE,
                  json.dumps({"type": FRAME_TYPE, "message": text_for(client_name)}), now.isoformat()))
    return True


def previous_count(conn) -> int:
    """Active agents whose declared client is the previous release."""
    rows = conn.execute("SELECT client_name, client_version FROM authorized_agents WHERE is_active = 1").fetchall()
    return sum(1 for r in rows if is_previous(r["client_name"], r["client_version"]))


def versions_count(conn) -> dict[str, int]:
    """Active agents by declared client version (`unknown` for none)."""
    rows = conn.execute("SELECT client_version, COUNT(*) AS n FROM authorized_agents WHERE is_active = 1 "
                        "GROUP BY client_version").fetchall()
    return {(r["client_version"] or "unknown"): r["n"] for r in rows}
