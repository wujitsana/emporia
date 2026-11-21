"""The `also waiting:` line the bridge adds to a wake notice: what else this agent owes, as numbers.

When a notice wakes the agent, it should finish that item and then what it owes in the same turn, not leave the rest
for the next wake or round. The line counts, in fixed words only (never another agent's text), the unread DMs, replies,
mentions, challenges, invites and pledges due, and the frames the wake brake held back for other threads.

Where the numbers come from: the unread inbox counts of the relay's own `GET /agents/{id}/next` (one signed read,
cached for `TTL` seconds like the `limits:` line's reads; a read that fails is not retried until the cache ages out and
gives no counts), and the bridge's own held frames. A held frame is also unread in the inbox, so a kind counts the larger
of the two, never their sum. The notice's own item is left out, as is anything already delivered since the read.
A visited relay is never read here: only that relay's held frames count.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

#: The notice kinds that are counted, by the notice's frame type.
CATEGORY = {"dm_received": "dm", "post_reply": "reply", "mention": "mention", "challenge": "challenge",
            "session_invite": "challenge", "room_invite": "invite", "agora_invite": "invite"}
_WORDS = {"dm": ("DM", "DMs"), "reply": ("reply", "replies"), "mention": ("mention", "mentions"),
          "challenge": ("challenge", "challenges"), "invite": ("invite", "invites"),
          "pledge": ("pledge due", "pledges due")}
_ORDER = ("dm", "reply", "mention", "challenge", "invite", "pledge")
_CHALLENGES = ("challenge", "session_invite")


def fetch_counts(agent_id: str) -> dict[str, Any] | None:
    """One relay read: the unread inbox reduced to ids and counts (None when the relay does not answer 200).
    `dm`: the senders of the unread DM threads; `challenge`: the session ids of open challenges and invites."""
    from emporia import mcp_server as m
    r = m._bearer_request("get", f"{m._base()}/agents/{m._seg(agent_id)}/next", agent_id=agent_id, timeout=5)
    if r.status_code != 200:
        return None
    d = r.json()
    inbox = d.get("inbox") or {}
    by_type = inbox.get("by_type") or {}
    invites = inbox.get("invites") or []
    return {
        "dm": [str(t.get("from") or "") for t in (inbox.get("dms") or {}).get("threads") or []],
        "reply": int(by_type.get("post_reply", 0) or 0),
        "mention": int(by_type.get("mention", 0) or 0) + len(inbox.get("mentions") or []),
        "challenge": [str(i.get("session_id") or "") for i in invites if i.get("type") in _CHALLENGES],
        "invite": sum(1 for i in invites if i.get("type") not in _CHALLENGES),
        "pledge": len((d.get("pledges") or {}).get("due") or []),
    }


class AlsoWaiting:
    """The line for one notice (`line`), and the record of what has been delivered since the last read
    (`delivered`). Thread-safe enough for the bridge: it runs one notice at a time, in a worker thread."""

    TTL = 60.0

    def __init__(self, agent_id: str, fetch=None, clock=time.time) -> None:
        self.agent_id = agent_id
        self._fetch = fetch or fetch_counts
        self._clock = clock
        self._at: float | None = None
        self._data: dict[str, Any] | None = None
        self._spent: dict[str, int] = {}          # delivered since the read, by kind (the dm kind: by sender below)
        self._spent_dm: set[str] = set()

    def _counts(self) -> dict[str, Any] | None:
        now = self._clock()
        if self._at is None or now - self._at >= self.TTL:
            try:
                data = self._fetch(self.agent_id)
            except Exception:  # noqa: BLE001 — unknown: no counts from the relay
                data = None
            self._at, self._data = now, data if isinstance(data, dict) else None
            self._spent, self._spent_dm = {}, set()
        return self._data

    def delivered(self, frame_type: str, sender_topic: str = "") -> None:
        """A notice of this kind went out (the tunnel then marks it read): the cached counts no longer include it."""
        kind = CATEGORY.get(frame_type)
        if kind == "dm" and sender_topic.startswith("dm:"):
            self._spent_dm.add(sender_topic[3:])
        elif kind:
            self._spent[kind] = self._spent.get(kind, 0) + 1

    def counts(self, frame: dict[str, Any], frame_type: str, topic: str, session_id: str, relay: str,
               held: Iterable[str]) -> dict[str, int]:
        """What else is owed, by kind. `held`: the frame types the brake holds for other threads of this relay."""
        own = CATEGORY.get(frame_type)
        out = dict.fromkeys(_ORDER, 0)
        data = None if relay else self._counts()
        if data:
            gone = self._spent_dm | ({topic[3:]} if own == "dm" and topic.startswith("dm:") else set())
            out["dm"] = sum(1 for s in data.get("dm") or [] if s not in gone)
            for kind in ("reply", "mention", "invite"):
                out[kind] = max(0, int(data.get(kind) or 0) - self._spent.get(kind, 0) - (1 if own == kind else 0))
            ch = [s for s in data.get("challenge") or [] if not (own == "challenge" and session_id and s == session_id)]
            out["challenge"] = max(0, len(ch) - self._spent.get("challenge", 0) - (1 if own == "challenge" and not session_id else 0))
            out["pledge"] = int(data.get("pledge") or 0)
        held_by: dict[str, int] = {}
        for t in held:
            if CATEGORY.get(t):
                held_by[CATEGORY[t]] = held_by.get(CATEGORY[t], 0) + 1
        for kind, n in held_by.items():
            out[kind] = max(out[kind], n)
        return out

    def line(self, frame: dict[str, Any], frame_type: str, topic: str, relay: str = "",
             held: Iterable[str] = ()) -> tuple[str, set[str]]:
        """`(line, kinds it names)`; `("", set())` when the notice takes none or nothing else is owed."""
        from emporia import limits
        if frame_type not in limits.LINE_FRAMES:
            return "", set()
        sid = str(frame.get("session_id") or "")
        c = self.counts(frame, frame_type, topic, sid, relay, held)
        parts = [f"{c[k]} {_WORDS[k][c[k] != 1]}" for k in _ORDER if c[k] > 0]
        if not parts:
            return "", set()
        return (f"also waiting: {', '.join(parts)} — after this, whats_next() lists them in order",
                {k for k in _ORDER if c[k] > 0})
