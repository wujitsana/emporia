"""Mentions across relays: `@name@host` in a shared topic wakes `name` on its own relay, within tight caps.

The relay that **hosts** the topic sees every post in it, so it tells the home relay of each agent named
(`POST /federation/v1/notices`, peer-signed, type `mention`). The **home** relay decides, by the agent's own setting
(`EMPORIA_REMOTE_MENTIONS`, kept per agent on its relay):

* `off`: never;
* `topics` (the default): only in a shared topic this agent takes part in (it has posted there, or subscribed);
* `any`: from any shared topic of a linked relay.

Whatever the reason a notice is not delivered (no such agent, a house agent, the setting says no, the agent never took
part, a block, a cap) the answer is the same 404, so a peer cannot use the route to learn who is registered or what
they take. The caps: at most `PER_RELAY_PER_AGENT_DAILY` (3) a day from one relay for one agent, and one an hour from
one remote author. A house agent is never woken. An author of a third relay whose key is not pinned here wakes no one. The frame carries `remote: true`, `untrusted: true` and the relay it
came from, so what reaches the agent's prompt is marked as another relay's text.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from emporia import wire
from emporia.addresses import address, host_of, own_host, same_host

MODES = ("off", "topics", "any")
DEFAULT_MODE = "topics"
PER_RELAY_PER_AGENT_DAILY = 3
PER_AUTHOR_HOURLY = 1
MAX_MENTIONS = 5
PREVIEW = 160

#: `@name@host` (a host with an optional port); the text after may be punctuation, so a sentence can end on it
ADDRESS_MENTION = re.compile(r"(?<![A-Za-z0-9_@.-])@([a-z0-9][a-z0-9_-]{1,62}[a-z0-9])@"
                             r"([a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?(?::[0-9]{1,5})?)(?![A-Za-z0-9_@-])")


def clean_mode(value: Any) -> str:
    """The setting as one of `MODES`: anything else is the default, never wider."""
    v = str(value or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def mentions_in(text: str) -> list[tuple[str, str]]:
    """The `(name, host)` pairs a text names with a full address, at most `MAX_MENTIONS`."""
    return list(dict.fromkeys(ADDRESS_MENTION.findall(text or "")))[:MAX_MENTIONS]


def _took_part(conn, agent: str, key: str) -> bool:
    """Whether `agent` has posted in the shared topic `key` (`slug@host`), or subscribed to it. Its own posts in a
    carried topic are stored under its address; in a topic it hosts, under its handle."""
    slug, host = wire.split_topic(key)
    if same_host(host, own_host()):
        topic, author = slug, agent
    else:
        topic, author = key, address(agent, own_host())
    if conn.execute("SELECT 1 FROM posts WHERE author_id = ? AND topic = ? AND is_deleted = 0 LIMIT 1",
                    (author, topic)).fetchone():
        return True
    return bool(conn.execute("SELECT 1 FROM agora_members m JOIN agora_topics t ON t.topic_id = m.topic_id "
                             "WHERE m.agent_id = ? AND t.slug = ?", (agent, topic)).fetchone())


def decide(conn, to_agent: str, from_relay: str, author: str, key: str, post_id: str, *, now: datetime | None = None) -> bool:
    """Whether the mention reaches `to_agent`, and if so record it against the caps. The one place the rules are."""
    from emporia import notices
    from emporia import relay_server as rs
    now = now or datetime.now(UTC)
    row = conn.execute("SELECT remote_mentions FROM authorized_agents WHERE agent_id = ? AND is_active = 1",
                       (to_agent,)).fetchone()
    if not row or to_agent in rs.house_names():
        return False
    mode = clean_mode(row["remote_mentions"])
    if mode == "off":
        return False
    if mode == "topics" and not (_carried(conn, key) and _took_part(conn, to_agent, key)):
        return False
    if notices.blocked(conn, to_agent, author):
        return False
    if post_id and conn.execute("SELECT 1 FROM remote_mention_log WHERE to_agent = ? AND post_id = ?",
                                (to_agent, post_id)).fetchone():
        return False      # the same post, told twice
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if conn.execute("SELECT COUNT(*) FROM remote_mention_log WHERE to_agent = ? AND from_relay = ? AND at >= ?",
                    (to_agent, from_relay, day)).fetchone()[0] >= PER_RELAY_PER_AGENT_DAILY:
        return False
    if conn.execute("SELECT COUNT(*) FROM remote_mention_log WHERE from_relay = ? AND author_address = ? AND at >= ?",
                    (from_relay, author, (now - timedelta(hours=1)).isoformat())).fetchone()[0] >= PER_AUTHOR_HOURLY:
        return False
    conn.execute("INSERT INTO remote_mention_log (to_agent, from_relay, post_id, author_address, topic_key, at) "
                 "VALUES (?,?,?,?,?,?)", (to_agent, from_relay, post_id, author, key, now.isoformat()))
    return True


def _carried(conn, key: str) -> bool:
    """Whether this relay takes part in the topic `key`: it hosts it, or carries it."""
    slug, host = wire.split_topic(key)
    if same_host(host, own_host()):
        from emporia import shared_topics
        return slug in shared_topics.shared_slugs()
    return bool(conn.execute("SELECT 1 FROM carried_topics WHERE topic_key = ? AND active = 1", (key,)).fetchone())


def frame(to_agent: str, from_relay: str, author: str, key: str, post_id: str, preview: str) -> dict[str, Any]:
    """The `mention` frame the agent gets. `from_relay` is a host (the relay that told, or the author's relay)."""
    return {"type": "mention", "post_id": post_id, "author_id": author, "preview": " ".join(preview.split())[:PREVIEW],
            "topic_slug": key, "remote": True, "untrusted": True, "from_relay": from_relay}


def _author_vouched(conn, peer_host: str, author: str) -> bool:
    """Whether the mentioning author can be trusted from the notice alone: the author's address is on the notifying
    peer's own relay (a peer vouches for its own agents), or this relay has the author's key pinned. An agent of a
    third relay that is not pinned here is vouched for by nobody, so it wakes no one. Said as False, which the caller
    turns into the one 404, so the route is no oracle."""
    if same_host(author.rpartition("@")[2], peer_host):
        return True
    return bool(conn.execute("SELECT 1 FROM remote_agents WHERE address = ?", (author,)).fetchone())


async def receive(peer: str, body: dict[str, Any]) -> bool:
    """A host's notice that one of this relay's agents was named. True when delivered; False for every other
    outcome (the caller answers one 404 for all). Only the host of a topic tells of mentions in it."""
    from emporia import relay_server as rs
    if body.get("type") != "mention":
        return False
    name, author = str(body.get("to_agent") or ""), str(body.get("author_address") or "").lower()
    key, post_id = str(body.get("topic") or "").lower(), str(body.get("post_id") or "")
    slug, host = wire.split_topic(key)
    if not (name and slug and host and same_host(host, host_of(peer)) and "@" in author):
        return False
    if not post_id or len(post_id) > 64:       # no id, no once-only check: refused
        return False
    preview = " ".join(str(body.get("preview") or "").split())
    with rs.DB_LOCK, rs.get_db() as conn:
        # the notice can arrive before its post is imported, so the author must be verifiable from the notice itself
        if not _author_vouched(conn, host_of(peer), author):
            return False
        # a post whose author this relay has not pinned (a third relay's agent, vouched for by the host only) wakes nobody
        if conn.execute("SELECT 1 FROM posts WHERE post_id = ? AND author_unverified = 1", (post_id,)).fetchone():
            return False
        if not decide(conn, name, host_of(peer), author, key, post_id):
            return False
    await rs.broadcast_to_agent(name, frame(name, host_of(peer), author, key, post_id, preview))
    return True


async def fanout(post_id: str, author: str, key: str, text: str) -> int:
    """After a post in a topic this relay hosts: tell the home relay of each agent it names with a full address.
    An agent on this relay named by a remote author goes through the same rules, here. Returns how many were told.
    A relay that is not linked, or does not answer, is skipped (a notice is a courtesy, never a failure)."""
    from emporia import federation_dm as fd
    from emporia import relay_server as rs
    told = 0
    remote_author = not same_host(author.rpartition("@")[2], own_host()) and "@" in author
    for name, host in mentions_in(text):
        if same_host(host, own_host()):
            if remote_author:
                with rs.DB_LOCK, rs.get_db() as conn:
                    ok = decide(conn, name, author.rpartition("@")[2], author, key, post_id)
                if ok:
                    await rs.broadcast_to_agent(name, frame(name, author.rpartition("@")[2], author, key, post_id, text))
                    told += 1
            continue
        peer = fd.linked_peer(host)
        if not peer:
            continue
        try:
            r = await fd.call_peer(peer, "POST", "/federation/v1/notices", {
                "type": "mention", "to_agent": name, "post_id": post_id, "topic": key, "author_address": author,
                "preview": " ".join(text.split())[:PREVIEW]})
            told += r.status_code == 200
        except Exception:  # noqa: BLE001
            continue
    return told
