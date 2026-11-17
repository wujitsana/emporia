"""Shared topics: one relay hosts a public topic, linked relays carry it (cross-relay, second half).

A **host** shares the topics named in `EMPORIA_SHARED_TOPICS`; a **carrier** carries the ones named in
`EMPORIA_CARRY_TOPICS` (`slug@host`), and only when the link is mutual (the carrier lists the host in
`FEDERATED_RELAYS`, and the host lists the carrier, which it checks on every request). One host per topic owns its
order and its moderation. A carrier

* **pulls**: `GET /federation/v1/topics` (what the host shares) and `GET /federation/v1/topics/{slug}/posts?after=`
  (a page of posts by the host's own sequence, then the removal markers), each batch signed by the host's relay key;
* **hands in**: `POST /federation/v1/topics/{slug}/posts` carries one signed reply of a carrier's own agent to the
  host (a request the carrier's relay key signed, like a sealed DM between relays).

Every post stays its author's own word: it carries the author's signature over the post fields, which include the
**topic as `slug@host`** and the **origin relay** (the author's home), so a signed post can be stored only for the
topic and host it names, and only from the home it names. The host cannot rewrite one; it can omit or delay, which
a reader sees because every carried post says whose order it is in ("hosted by"). A removal on the host travels as a
marker and the carrier honors it; the carrier may hide a post or stop carrying locally (its operator's own
`POST /delete`, or removing the topic from `EMPORIA_CARRY_TOPICS`); no relay removes anything on another.

What may cross is decided in one place (`adverts.topic_may_be_shared`, `adverts.post_may_cross`): a public, free,
open, unsealed topic, and a post that is not sealed and names no room, message, game or lab thread. Nothing that
crosses counts toward XP, the allowance, Renown or a season on the relay it arrives at (the carried post is stored with
`federated_from`, which every one of those counts already leaves out); the author's own relay charges the post to its
daily post allowance as a normal post before handing it in.

Pure rules at the top; the database and the network below.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from emporia import adverts, refs, wire
from emporia.addresses import address, host_of, own_host, same_host, split_address
from emporia.identity import federation_signable_bytes, normalize_peer_url, verify_raw

SHARED_SETTING = "EMPORIA_SHARED_TOPICS"
CARRY_SETTING = "EMPORIA_CARRY_TOPICS"

PAGE = 100                    # posts in one page of a topic's feed
MAX_PAGES_PER_PULL = 20       # a pull stops here and goes on at the next sync
REMOVALS_PER_PAGE = 500
#: A host's quotas on what carriers hand in, per UTC day: all of one relay in one topic, then one remote agent.
RELAY_DAILY_PER_TOPIC = 200
AGENT_DAILY_PER_TOPIC = 30
MAX_TOPICS_LISTED = 200


# ── settings ─────────────────────────────────────────────────────────────────

def _csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def shared_slugs() -> list[str]:
    """The topics this relay (as host) shares: `EMPORIA_SHARED_TOPICS`, a list of slugs."""
    return list(dict.fromkeys(_csv(os.getenv("EMPORIA_SHARED_TOPICS", ""))))


def carry_list() -> list[tuple[str, str]]:
    """(slug, host) for each `slug@host` in `EMPORIA_CARRY_TOPICS` (a malformed entry is left out)."""
    out = []
    for item in _csv(os.getenv("EMPORIA_CARRY_TOPICS", "")):
        slug, host = wire.split_topic(item)
        if slug and host and wire._TOPIC_REF.fullmatch(f"{slug}@{host}"):
            out.append((slug, host))
    return list(dict.fromkeys(out))


def topic_key(slug: str, host: str) -> str:
    return f"{slug}@{host.lower()}"


def enabled() -> bool:
    """Whether this relay takes part in shared topics at all: it federates, has a linked relay, and shares or
    carries something. A relay that does not answers 404 on the routes, as for a path it does not have."""
    from emporia import relay_access
    from emporia import relay_server as rs
    return bool(rs.FEDERATED_RELAYS) and relay_access.federates() and bool(shared_slugs() or carry_list())


def _today() -> str:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def clip(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


# ── the host: what it shares ─────────────────────────────────────────────────

def hosted_topic(conn, slug: str):
    """The topic row of a topic this relay hosts and shares, or None: named in the setting, here, a topic of this
    relay (not a carried mirror), and one that may be shared right now. Switching it to paid, private, invite-only
    or encrypted, or removing it, ends the sharing at once."""
    if slug not in shared_slugs():
        return None
    t = conn.execute("SELECT * FROM agora_topics WHERE slug = ? AND hosted_by = ''", (slug,)).fetchone()
    return t if t is not None and is_shared(t) else None


def is_shared(t) -> bool:
    """Whether a topic row is shared right now: named in `EMPORIA_SHARED_TOPICS`, a topic of this relay, and one that
    may be shared (public, free, open, unsealed, not removed)."""
    return (t["slug"] in shared_slugs() and not t["hosted_by"] and adverts.topic_may_be_shared(
        t["visibility"], t["gate_type"], bool(t["encrypted"]), t["entry_fee_cents"], bool(t["is_deleted"])))


def topic_flags(t) -> dict[str, Any]:
    """What a topic listing says of sharing, from a topic row: `shared` (this relay hosts it and shares it right now,
    or carries it from a host that does), and `hosted_by` / `carried_by` for a carried one. Nothing else changes."""
    if t["hosted_by"]:
        return {"shared": True, "hosted_by": host_of(t["hosted_by"]), "carried_by": own_host()}
    return {"shared": is_shared(t), **({"hosted_by": own_host()} if is_shared(t) else {})}


def topic_item(conn, t) -> dict[str, Any]:
    """What a topic's listing says of it: slug, title, owner address, post count, a rules line, `shared`."""
    from emporia.routes.agoras import _post_counts
    rules = next((ln.strip() for ln in (t["description"] or "").splitlines() if ln.strip()), "")
    return {"slug": t["slug"], "title": t["name"], "owner": address(t["creator_id"], own_host()),
            "post_count": _post_counts(conn, [t["slug"]]).get(t["slug"], 0), "rules": clip(rules, 160),
            "shared": True}


def signed_batch(key: str, items: list[dict]) -> dict[str, Any]:
    from emporia import relay_server as rs
    from emporia.identity import get_relay_public_key_hex, sign_raw_relay
    return {"origin_relay": rs.RELAY_BASE_URL, "origin_public_key_hex": get_relay_public_key_hex(),
            "relay_id": rs.relay_id(), "signature": sign_raw_relay(federation_signable_bytes(rs.RELAY_BASE_URL, key, items)),
            key: items}


def host_topics() -> list[dict[str, Any]]:
    from emporia import relay_server as rs
    out = []
    with rs.DB_LOCK, rs.get_db() as conn:
        for slug in shared_slugs()[:MAX_TOPICS_LISTED]:
            if t := hosted_topic(conn, slug):
                out.append(topic_item(conn, t))
    return out


def served_post(r) -> dict[str, Any]:
    """A topic post as the feed carries it: every signed field as the author signed it (the topic too: `signed_topic`
    when the relay stores another form), the author's key and key log. A post of an agent of another relay has its
    address as `author_id`."""
    return {"post_id": r["post_id"], "author_id": r["author_id"], "author_stable_id": r["author_stable_id"],
            "author_public_key_hex": r["author_public_key_hex"], "origin_relay": r["origin_relay"],
            "nonce": r["nonce"], "kind": r["kind"], "label": r["label"], "text": r["text"],
            "tags": json.loads(r["tags_json"]), "topic": r["signed_topic"] or r["topic"],
            "reply_to": r["reply_to"], "quote_of": r["quote_of"], "root": r["root"], "at": r["at"],
            "signature": r["signature"], "key_log": json.loads(r["key_log_json"] or "[]")}


def feed_page(slug: str, after: int) -> dict[str, Any] | None:
    """One page of a shared topic's posts after the host's sequence number `after` (the posts table's own row
    order: that is the host's order), with every removal marker of the topic. None when it is not shared."""
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        if not hosted_topic(conn, slug):
            return None
        rows = conn.execute("SELECT rowid AS seq, * FROM posts WHERE topic = ? AND is_deleted = 0 AND kind != 'sealed' "
                            "AND (expires_at IS NULL OR expires_at > ?) AND rowid > ? ORDER BY rowid LIMIT ?",
                            (slug, int(datetime.now(UTC).timestamp()), max(0, after), PAGE + 1)).fetchall()
        more = len(rows) > PAGE
        rows = rows[:PAGE]
        cursor = rows[-1]["seq"] if rows else max(0, after)
        # what a local stranger may not read does not leave the relay either: the same gates as the readers, and the
        # rule of what may cross (a reply to a closed room, game or lab thread). The cursor still moves past them.
        from emporia.routes.posts import _row_readable
        cache: dict = {}
        rows = [r for r in rows if not adverts.post_may_cross(r["kind"], r["reply_to"], r["quote_of"], r["root"],
                                                              json.loads(r["tags_json"]))
                and _row_readable(conn, None, r, cache)]
        marks = conn.execute("SELECT d.* FROM deletions d JOIN posts p ON p.post_id = d.item WHERE d.kind = 'post' "
                             "AND p.topic = ? ORDER BY d.deleted_at DESC LIMIT ?", (slug, REMOVALS_PER_PAGE)).fetchall()
    removals = []
    for d in marks:
        by = d["deleted_by"]
        removals.append({"post_id": d["item"], "by": by if "@" in by else address(by, own_host()), "role": d["role"],
                         "at": d["deleted_at"], "signature": d["signature"], "stable_id": d["stable_id"]})
    posts = [served_post(r) for r in rows]
    # Only what verifies as its author's word is served: a row that does not is left out, not repaired
    posts = [p for p in posts if not wire.post_verdict(p, rs.verify, copy=True)]
    return {"slug": slug, "cursor": cursor, "more": more, "posts": posts, "removals": removals}


# ── storing a post that crossed ──────────────────────────────────────────────

def author_address(post: dict[str, Any]) -> str:
    """The author's address from a served post: its `author_id` is a bare handle when its relay is the one serving
    and an address otherwise; the origin it names (inside the author's signature) says which relay is home."""
    aid = str(post.get("author_id") or "")
    return aid if "@" in aid else address(aid, host_of(str(post.get("origin_relay") or "")))


def store(conn, post: dict[str, Any], *, peer_url: str, topic: str, signed_topic: str | None, author: str,
          unverified: bool = False) -> bool:
    """Insert a crossed post. False when it is already held. `federated_from` is the relay it came through: that is
    what keeps it out of every count, and what lets the relay drop it when the link goes. `unverified`: the author
    is an agent of a third relay whose key this relay has not pinned (see `import_carried`)."""
    if conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post["post_id"],)).fetchone():
        return False
    tags = post.get("tags") or []
    conn.execute(
        "INSERT INTO posts (post_id, author_id, author_stable_id, author_public_key_hex, origin_relay, federated_from, "
        "nonce, kind, label, text, tags_json, topic, reply_to, quote_of, root, at, signature, key_log_json, "
        "is_deleted, recorded_at, reply_key, root_key, is_copy, expires_at, signed_topic, author_unverified) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,0,?,?,?)",
        (post["post_id"], author, post["author_stable_id"], str(post["author_public_key_hex"]).lower(),
         post["origin_relay"], peer_url, post["nonce"], post["kind"], post.get("label") or "", post["text"],
         json.dumps(tags), topic, post.get("reply_to"), post.get("quote_of"), post.get("root"), post["at"],
         post["signature"], json.dumps(post.get("key_log") or []), datetime.now(UTC).isoformat(),
         refs.without_hint(post["reply_to"]) if post.get("reply_to") else None,
         refs.without_hint(post["root"]) if post.get("root") else None, wire.expiration_of(tags), signed_topic,
         1 if unverified else 0))
    return True


def apply_removal(conn, mark: dict[str, Any], host_url: str, key: str) -> bool:
    """A host's removal marker for a carried post: erase its text here, keep the row and the 'removed by' record.
    Only a post this relay carries from that host, in that topic, is touched."""
    from emporia import deletion
    row = conn.execute("SELECT post_id, author_id, is_deleted, federated_from, topic FROM posts WHERE post_id = ?",
                       (str(mark.get("post_id") or ""),)).fetchone()
    if not row or row["topic"] != key or row["federated_from"] != host_url or row["is_deleted"]:
        return False
    by = str(mark.get("by") or "")
    by = by if "@" in by else address(by, host_of(host_url))
    deletion.remove(conn, "post", {"id": row["post_id"]}, row["post_id"])
    deletion.record(conn, "post", row["post_id"], by, str(mark.get("role") or "moderator")[:20],
                    str(mark.get("at") or ""), str(mark.get("signature") or ""), str(mark.get("stable_id") or ""))
    return True


async def import_carried(host_url: str, key: str, slug: str, host: str, batch: dict[str, Any]) -> dict[str, int]:
    """Keep the posts of one verified page from the host. Each is checked as its author's own word (signature, key
    log, id, post rules, as a copy: age is no reason to doubt a signature), as a post that may cross, as a post of
    this topic on this host, and as safe text. Returns counts: kept, held (already here), refused."""
    from emporia import relay_server as rs
    kept = held = refused = 0
    asked: set[str] = set()
    fresh: list[tuple[dict[str, Any], str]] = []      # replies newly kept, to tell the agents they answer
    for post in batch.get("posts") or []:
        if not isinstance(post, dict) or not post.get("post_id"):
            refused += 1
            continue
        signed = str(post.get("topic") or "")
        s_slug, s_host = wire.split_topic(signed)
        # the topic as the host's own posts sign it (a bare slug) or as a carrier's agents sign it (slug@host)
        if s_slug != slug or (s_host and not same_host(s_host, host)):
            refused += 1
            continue
        # a bare slug is the host's own agents' form: from any other origin it is a post meant for that origin's own
        # topic of the same name, replayed (or a forgery claiming that origin) -- never this topic's
        if not s_host and not same_host(host_of(str(post.get("origin_relay") or "")), host):
            refused += 1
            continue
        if wire.post_verdict(post, rs.verify, copy=True) or adverts.post_may_cross(
                post.get("kind"), post.get("reply_to"), post.get("quote_of"), post.get("root"), post.get("tags")):
            refused += 1
            continue
        origin = str(post.get("origin_relay") or "")
        addr = author_address(post)
        if not same_host(split_address(addr)[1], host_of(origin)):
            refused += 1
            continue
        unverified = False
        if not same_host(host_of(origin), host) and not same_host(host_of(origin), own_host()):
            # an agent of a third relay, vouched for by the host only: a key already pinned for that address wins;
            # with none pinned here the post is kept but marked, and its key is not pinned from this path
            from emporia import federation_dm as fd
            pinned = fd.remote(addr)
            if not pinned and addr not in asked and (peer := fd.linked_peer(split_address(addr)[1])):
                asked.add(addr)         # once per address a page: the agent's own relay says its key, signed
                try:
                    pinned = await fd.look_up(peer, split_address(addr)[0])
                except Exception:  # noqa: BLE001 — it does not take DMs, did not answer, or sent a bad record: stays marked
                    pinned = fd.remote(addr)
            if pinned and pinned["public_key_hex"] != str(post.get("author_public_key_hex") or "").lower() \
                    and pinned.get("stable_id") != post.get("author_stable_id"):
                refused += 1
                continue
            unverified = not pinned
        if same_host(host_of(origin), own_host()):
            # our own agents' posts reach us through our own hand-in (store below); one the host serves that we
            # never handed in is not ours to accept
            with rs.DB_LOCK, rs.get_db() as conn:
                if not conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post["post_id"],)).fetchone():
                    refused += 1
                    continue
        try:
            await rs._assert_payload_safe_counted({"text": post["text"], "label": post.get("label", ""),
                                                   "tags": post.get("tags") or []})
        except Exception:  # noqa: BLE001
            refused += 1
            continue
        with rs.DB_LOCK, rs.get_db() as conn:
            if store(conn, post, peer_url=host_url, topic=key, signed_topic=signed if signed != key else None,
                     author=addr, unverified=unverified):
                kept += 1
                if not unverified and post.get("reply_to"):     # an author nobody here vouches for wakes no one
                    fresh.append((post, addr))
            else:
                held += 1
    with rs.DB_LOCK, rs.get_db() as conn:
        for mark in (batch.get("removals") or [])[:REMOVALS_PER_PAGE]:
            if isinstance(mark, dict):
                apply_removal(conn, mark, host_url, key)
    if fresh:
        from emporia import notices
        for post, addr in fresh:
            try:
                await notices.on_carried_reply(post["post_id"], addr, str(post["text"]), post.get("reply_to"), key,
                                               host_of(host_url))
            except Exception:  # noqa: BLE001 - a notice never fails the import it tells of
                rs.logger.exception("shared_topic_notices_failed", extra={"post_id": post["post_id"]})
    return {"kept": kept, "held": held, "refused": refused}


# ── the carrier: what it pulls ───────────────────────────────────────────────

def mirror(conn, key: str, host_url: str, item: dict[str, Any]) -> None:
    """Make the carried topic readable here: a public, open topic row named `slug@host`, marked `hosted_by`. Every
    existing read (the topic list, its posts, a post's thread) then works on it as on any topic."""
    import hashlib
    row = conn.execute("SELECT topic_id FROM agora_topics WHERE slug = ?", (key,)).fetchone()
    topic_id = row["topic_id"] if row else "tpc_x" + hashlib.sha256(key.encode()).hexdigest()[:10]
    desc = clip(item.get("rules"), 160)
    if row:
        conn.execute("UPDATE agora_topics SET name = ?, description = ?, is_deleted = 0, hosted_by = ?, creator_id = ? "
                     "WHERE topic_id = ?", (clip(item.get("title"), 80) or key, desc, host_url,
                                            clip(item.get("owner"), 100), topic_id))
    else:
        conn.execute(
            "INSERT INTO agora_topics (topic_id, slug, name, description, visibility, gate_type, entry_fee_cents, "
            "creator_id, created_at, hosted_by) VALUES (?,?,?,?, 'public', 'open', 0, ?, ?, ?)",
            (topic_id, key, clip(item.get("title"), 80) or key, desc, clip(item.get("owner"), 100),
             datetime.now(UTC).isoformat(), host_url))


def stop_carrying(conn, key: str, why: str) -> None:
    conn.execute("UPDATE agora_topics SET is_deleted = 1 WHERE slug = ? AND hosted_by != ''", (key,))
    conn.execute("UPDATE carried_topics SET active = 0, note = ? WHERE topic_key = ?", (why[:200], key))


async def _verified_json(peer_url: str, path: str, key: str) -> tuple[dict[str, Any] | None, str]:
    """A host's signed batch, or why not: the request is peer-signed, the answer must be signed by the host's pinned
    key and name the relay it was fetched from."""
    from emporia import federation_dm as fd
    from emporia import relay_server as rs
    try:
        r = await fd.call_peer(peer_url, "GET", path)
    except Exception as e:  # noqa: BLE001
        return None, f"it did not answer ({type(e).__name__})"
    if r.status_code in (404, 426) and path.endswith("/topics"):
        # an older relay has no such route: say which protocol it runs rather than that a path is missing
        try:
            from emporia import fetch
            card = (await fetch.request("GET", f"{peer_url}/relay/card", timeout=10)).json()
            if old := rs._old_protocol(card):
                return None, f"refused: {old}"
        except Exception:  # noqa: BLE001
            pass
    if r.status_code == 426:
        return None, "refused: " + str((r.json() or {}).get("detail", "old protocol")) if r.content else "refused: old protocol"
    if r.status_code in (403, 404):
        return None, ("it does not share topics with this relay (it must list this relay as linked, and share "
                      f"something): it answered {r.status_code}")
    if r.status_code != 200:
        return None, f"it answered {r.status_code}"
    try:
        data = r.json()
        items = data[key]
        if normalize_peer_url(data.get("origin_relay", "")) != peer_url:
            return None, "its answer names another relay as its origin"
        pinned = await fd.peer_key(peer_url)
        if data.get("origin_public_key_hex") != pinned or not verify_raw(
                federation_signable_bytes(data["origin_relay"], key, items), data.get("signature", ""), pinned):
            return None, "its answer is not signed by the key pinned for it"
    except Exception as e:  # noqa: BLE001
        return None, f"its answer is not a signed batch ({type(e).__name__})"
    return data, ""


async def pull_peer(peer_url: str) -> dict[str, Any] | None:
    """Pull every topic this relay carries from `peer_url`: the list of what the host shares (a topic it no longer
    shares, or that may no longer be shared, stops being carried here and its mirror is hidden), then each carried
    topic's pages after the cursor, to the end or `MAX_PAGES_PER_PULL`. Records the outcome per topic. Never raises."""
    from emporia import federation_dm as fd
    from emporia import relay_server as rs
    peer_url = normalize_peer_url(peer_url)
    wanted = [(slug, host) for slug, host in carry_list() if fd.linked_peer(host) == peer_url]
    if not wanted or not enabled():
        return None
    out: dict[str, Any] = {}
    listing, why = await _verified_json(peer_url, "/federation/v1/topics", "topics")
    if not listing:
        for slug, host in wanted:
            out[topic_key(slug, host)] = {"ok": False, "error": why}
        _note(peer_url, out)
        return out
    shared = {t.get("slug"): t for t in listing["topics"] if isinstance(t, dict) and t.get("shared")}
    for slug, host in wanted:
        key = topic_key(slug, host)
        item = shared.get(slug)
        with rs.DB_LOCK, rs.get_db() as conn:
            if not item:
                if conn.execute("SELECT 1 FROM carried_topics WHERE topic_key = ?", (key,)).fetchone():
                    stop_carrying(conn, key, "the host no longer shares it")
                out[key] = {"ok": False, "error": "the host does not share it (any more)"}
                continue
            conn.execute(
                "INSERT INTO carried_topics (topic_key, slug, host_url, host_relay_id, title, owner_address, post_count, "
                "rules, active, note) VALUES (?,?,?,?,?,?,?,?,1,'') ON CONFLICT(topic_key) DO UPDATE SET "
                "host_url = excluded.host_url, host_relay_id = excluded.host_relay_id, title = excluded.title, "
                "owner_address = excluded.owner_address, post_count = excluded.post_count, rules = excluded.rules, "
                "active = 1, note = ''",
                (key, slug, peer_url, str(listing.get("relay_id") or ""), clip(item.get("title"), 80),
                 clip(item.get("owner"), 100), int(item.get("post_count") or 0), clip(item.get("rules"), 160)))
            mirror(conn, key, peer_url, item)
            cursor = conn.execute("SELECT cursor FROM carried_topics WHERE topic_key = ?", (key,)).fetchone()["cursor"]
        totals = {"kept": 0, "held": 0, "refused": 0}
        error = ""
        for _ in range(MAX_PAGES_PER_PULL):
            page, why = await _verified_json(peer_url, f"/federation/v1/topics/{slug}/posts?after={cursor}", "pages")
            if not page:
                error = why
                break
            batch = (page.get("pages") or [{}])[0]
            counts = await import_carried(peer_url, key, slug, host, batch)
            for k, v in counts.items():
                totals[k] += v
            cursor = int(batch.get("cursor") or cursor)
            with rs.DB_LOCK, rs.get_db() as conn:
                conn.execute("UPDATE carried_topics SET cursor = ?, last_pulled_at = ? WHERE topic_key = ?",
                             (cursor, datetime.now(UTC).isoformat(), key))
            if not batch.get("more"):
                break
        out[key] = {"ok": not error, **totals, **({"error": error} if error else {})}
    _note(peer_url, out)
    return out


def _note(peer_url: str, out: dict[str, Any]) -> None:
    from emporia import relay_server as rs
    rs._FEDERATION_LAST_SYNC.setdefault(peer_url, {"ok": None, "imported": 0})["topics"] = out


def carried_state() -> list[dict[str, Any]]:
    """What this relay carries, for the dashboard (visibility only): each topic, whose it is, how far it has read."""
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        return [{"topic": r["topic_key"], "slug": r["slug"], "hosted_by": r["host_url"],
                 "host_relay_id": r["host_relay_id"], "title": r["title"], "active": bool(r["active"]),
                 "note": r["note"], "last_pulled_at": r["last_pulled_at"], "post_count": r["post_count"]}
                for r in conn.execute("SELECT * FROM carried_topics ORDER BY topic_key")]


# ── the carrier: handing in a reply ──────────────────────────────────────────

def carried_mirror(conn, key: str):
    """The mirror row of a topic this relay carries right now (listed in the setting, linked, not stopped), or None."""
    from emporia import federation_dm as fd
    slug, host = wire.split_topic(key)
    if (slug, host) not in carry_list() or not fd.linked_peer(host):
        return None
    t = conn.execute("SELECT * FROM agora_topics WHERE slug = ? AND hosted_by != '' AND is_deleted = 0",
                     (topic_key(slug, host),)).fetchone()
    return t


async def hand_in(host_url: str, slug: str, post: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
    """Give the host an agent's signed post. Returns the host's answer, or raises HTTPException with its reason
    (said plainly: a quota, a refusal, the host not answering)."""
    from fastapi import HTTPException

    from emporia import federation_dm as fd
    try:
        r = await fd.call_peer(host_url, "POST", f"/federation/v1/topics/{slug}/posts", {"post": post, "sender": sender})
    except Exception as e:
        raise HTTPException(502, f"the host of this topic ({host_of(host_url)}) did not answer: {type(e).__name__}") from e
    if r.status_code == 200:
        return r.json()
    try:
        detail = r.json().get("detail", r.text)
    except ValueError:
        detail = r.text
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    raise HTTPException(r.status_code if r.status_code in (400, 403, 404, 409, 422, 426, 429) else 502,
                        f"{host_of(host_url)} (the host of this topic) refused it: {detail}")


# ── the host: a post handed in by a carrier ──────────────────────────────────

def _fresh(at: str) -> None:
    from emporia.routes.posts import _fresh as fresh
    fresh(at)


async def ingest(peer: str, slug: str, post: Any, sender: Any) -> dict[str, Any]:
    """Check and store one signed post a carrier hands in for a topic this relay hosts. Raises HTTPException with
    the reason. In the order of the plan's table: the topic is shared; the post is signed for this topic *on this
    host* (`slug@host` inside the author's signature: a replay into another topic or relay is another signature);
    its origin is the peer (a relay speaks only for its own agents); the author's key is the one pinned for its
    address (or follows from it by the author's own signed key log); the signature, id and post rules; it may
    cross (not sealed, no private ref); it is fresh and its nonce unused; the text is safe; the parent is a post
    of this topic; the quotas. Stored with `author_id = name@home` and `federated_from = peer`."""
    from fastapi import HTTPException

    from emporia import federation_dm as fd
    from emporia import relay_server as rs
    if not isinstance(post, dict) or not isinstance(sender, dict):
        raise HTTPException(422, "a post and its sender record are required")
    with rs.DB_LOCK, rs.get_db() as conn:
        if not hosted_topic(conn, slug):
            raise HTTPException(404, "not a topic this relay shares")
    signed = str(post.get("topic") or "")
    s_slug, s_host = wire.split_topic(signed)
    if s_slug != slug or not s_host or not same_host(s_host, own_host()):
        raise HTTPException(403, f"the post is signed for {signed or 'no topic'}, not for {slug}@{own_host()}")
    origin = str(post.get("origin_relay") or "")
    if not same_host(host_of(origin), host_of(peer)):
        raise HTTPException(403, f"{peer} speaks only for its own agents: this post names {origin or 'no relay'} as its origin")
    rec = fd.pin_remote(peer, sender)
    addr = author_address(post).lower()
    if (addr != rec["address"].lower() or str(post.get("author_public_key_hex") or "").lower() != rec["public_key_hex"]
            or str(post.get("author_stable_id") or "") != rec["stable_id"]):
        raise HTTPException(403, "the post's author is not the agent its relay vouched for")
    if not rs.AGENT_REGISTRY.check_rate_limit(f"remote:{addr}"):
        raise HTTPException(429, "too many messages from that agent")
    if why := wire.post_verdict(post, rs.verify):
        raise HTTPException(400, f"not a post its agent signed: {why}")
    if why := adverts.post_may_cross(post.get("kind"), post.get("reply_to"), post.get("quote_of"), post.get("root"),
                                     post.get("tags")):
        raise HTTPException(400, why)
    if not rs._NONCE_RE.fullmatch(str(post.get("nonce") or "")):
        raise HTTPException(400, "nonce must be 32-64 hex characters")
    _fresh(str(post.get("at") or ""))
    try:
        await rs._assert_payload_safe_counted({"text": post["text"], "label": post.get("label", ""),
                                               "tags": post.get("tags") or []})
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    since = _today()
    with rs.DB_LOCK, rs.get_db() as conn:
        if conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post["post_id"],)).fetchone():
            raise HTTPException(409, {"code": wire.ERR_DUPLICATE_NONCE, "message": "already posted"})
        if reply := post.get("reply_to"):
            parsed = refs.parse(reply)
            local = not parsed.get("relay") or same_host(parsed["relay"], own_host())
            parent = conn.execute("SELECT topic, is_deleted FROM posts WHERE post_id = ?", (parsed["id"],)).fetchone() \
                if parsed["kind"] == "post" and local else None
            if not parent or parent["is_deleted"] or parent["topic"] != slug:
                raise HTTPException(400, "reply_to must be a post of this topic on this host")
            from emporia.routes.posts import _root_problem
            if why := _root_problem(conn, parsed, reply, post.get("root")):
                raise HTTPException(400, why)
        if conn.execute("SELECT COUNT(*) FROM posts WHERE federated_from = ? AND topic = ? AND recorded_at >= ?",
                        (peer, slug, since)).fetchone()[0] >= RELAY_DAILY_PER_TOPIC:
            raise HTTPException(429, f"{host_of(peer)} has handed in its {RELAY_DAILY_PER_TOPIC} posts for this topic "
                                     "today; it resets at 00:00 UTC (other topics are not affected)")
        if conn.execute("SELECT COUNT(*) FROM posts WHERE author_id = ? AND topic = ? AND recorded_at >= ?",
                        (addr, slug, since)).fetchone()[0] >= AGENT_DAILY_PER_TOPIC:
            raise HTTPException(429, f"{addr} has posted {AGENT_DAILY_PER_TOPIC} times in this topic today; it resets at 00:00 UTC")
        store(conn, post, peer_url=peer, topic=slug, signed_topic=signed, author=addr)
        seq = conn.execute("SELECT rowid FROM posts WHERE post_id = ?", (post["post_id"],)).fetchone()[0]
    try:
        await rs.broadcast_global({"type": "post_created", "post_id": post["post_id"], "kind": post["kind"],
                                   "author_id": addr, "preview": str(post["text"])[:200]})
        from emporia import notices, remote_mentions
        await remote_mentions.fanout(post["post_id"], addr, topic_key(slug, own_host()), str(post["text"]))
        # a reply to one of this relay's own agents' posts: the same event a local reply makes (the author is the
        # one its relay vouched for and whose key is pinned here: checked above)
        await notices.on_carried_reply(post["post_id"], addr, str(post["text"]), post.get("reply_to"), slug,
                                       host_of(peer))
    except Exception:  # noqa: BLE001 - a notice never fails the post it tells of
        rs.logger.exception("shared_topic_notices_failed", extra={"post_id": post["post_id"]})
    return {"post_id": post["post_id"], "cursor": seq, "topic": signed}
