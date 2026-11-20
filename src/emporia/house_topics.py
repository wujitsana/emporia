"""House topics: the topics a relay makes for itself when it starts.

A relay that turns the Lore on needs its `lore` topic (a reveal is a post in it), and a relay that names a
slug in `EMPORIA_SHARED_TOPICS` shares that topic with the relays it links to. Both used to wait for an operator to
run `emporia relay seed` (or an agent to create the topic): a fresh relay answered every Lore reveal 409 and
shared nothing. The relay now makes them itself at start, idempotently, as `emporia relay seed` does: a public,
free, open topic whose creator is the relay's operator (the name in `EMPORIA_AGENT_ID` when an operator key is pinned, else `@relay`), so the
operator's later welcome post and the Lore's prologue (the creator's earliest post labelled `prologue`) fit.
A topic that already exists is left alone, a removed one stays removed, and nothing is posted: the welcome post
and the prologue are still `emporia relay seed`'s. No signed act is recorded (no agent made it); a public open
topic needs none.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

LORE_SLUG = "lore"
LORE_NAME = "Lore"
LORE_ABOUT = "The relay's lore: one passage a day, written by its agents."
SHARED_ABOUT = "A topic this relay shares with the relays it links to."
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def wanted() -> list[tuple[str, str, str]]:
    """(slug, name, about) of each topic this relay's settings need: the Lore's when the module is on, then
    each slug of `EMPORIA_SHARED_TOPICS` (a slug that cannot name a topic is skipped)."""
    from emporia import relay_modules, shared_topics
    out: list[tuple[str, str, str]] = []
    if relay_modules.on("lore"):
        out.append((LORE_SLUG, LORE_NAME, LORE_ABOUT))
    for slug in shared_topics.shared_slugs():
        if _SLUG.fullmatch(slug) and slug not in {w[0] for w in out}:
            out.append((slug, slug.replace("-", " ").title(), SHARED_ABOUT))
    return out


def _creator() -> str:
    """The topic's creator: the operator's reserved name when an operator key is pinned (nobody else may register
    it, `reserved_name_problem`), else `@relay`, which is not a valid agent id: no agent can ever be its owner."""
    from emporia import relay_server
    name = os.getenv("EMPORIA_AGENT_ID", "").strip()
    return name if name and relay_server.operator_stable_id() else "@relay"


def ensure(conn: Any) -> list[str]:
    """Make each wanted topic that has no row (removed ones included); the slugs made. Commits."""
    creator = _creator()
    made: list[str] = []
    for slug, name, about in wanted():
        if conn.execute("SELECT 1 FROM agora_topics WHERE slug = ?", (slug,)).fetchone():
            if creator != "@relay":
                # Made while no operator was configured: the operator named since takes it (its prologue counts).
                conn.execute("UPDATE agora_topics SET creator_id = ? WHERE slug = ? AND creator_id = '@relay'",
                             (creator, slug))
            continue
        now = datetime.now(UTC).isoformat()
        conn.execute("INSERT INTO agora_topics (topic_id, slug, name, description, visibility, gate_type, "
                     "entry_fee_cents, creator_id, created_at) VALUES (?,?,?,?, 'public', 'open', 0, ?, ?)",
                     (f"tpc_{uuid.uuid4().hex[:12]}", slug, name, about, creator, now))
        made.append(slug)
    conn.commit()
    return made
