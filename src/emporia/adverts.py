"""Rooms and Agora topics found from other relays (P247, P249).

An owner signs an advert for a room or topic; the relay that hosts it serves the
advert at `/federation/v1/rooms` and `/topics`, and a linked relay pulls those
feeds and keeps the ones that check out. The room or topic never moves: others
visit the hosting relay to join, and pay there, so that relay keeps its fee.

What may be advertised is decided here, once: a public open room, a paid room
(however it is listed), a public topic that is not invite-only. A private
invite-only room is never advertised or listed to anyone who is not in it.
"""

from __future__ import annotations

import json
from typing import Any


def room_may_be_advertised(room_type: str, gate_type: str) -> bool:
    """A public open room, or a paid one — which is `private` because only those
    who paid are in it, but whose terms its creator may choose to show. An
    invite-only room is never shown."""
    return gate_type == "paid" or (room_type == "public" and gate_type == "open")


def topic_may_be_advertised(visibility: str, gate_type: str) -> bool:
    return visibility == "public" and gate_type != "invite"


def own_room_adverts(room_ids: set[str] | None = None) -> list[tuple[Any, dict[str, Any]]]:
    """(room, feed item) for each room here whose creator's advert still matches
    what is stored — so a changed or removed room drops out on its own. Limited
    to `room_ids` when given."""
    from emporia import relay_server as rs
    from emporia import rooms as rooms_db
    from emporia.addresses import address, fingerprint
    from emporia.identity import verify
    from emporia.wire import ROOM_ADVERT_FIELDS, room_advert_verdict
    host = rs._own_host()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM room_adverts ORDER BY advertised_at DESC LIMIT ?",
                            (rs._MAX_LIMIT,)).fetchall()
        logs = {sid: rs.agent_key_log(conn, sid) for sid in {a["stable_id"] for a in rows}}
    out: list[tuple[Any, dict[str, Any]]] = []
    for a in rows:
        if room_ids is not None and a["room_id"] not in room_ids:
            continue
        room = rooms_db.get_room(a["room_id"], db_path=rs.DATABASE_PATH)
        if not room or not room_may_be_advertised(room.room_type, room.gate_type):
            continue
        item = {"room_id": room.room_id, **{f: getattr(room, f) for f in ROOM_ADVERT_FIELDS},
                "join_url": f"{rs.RELAY_BASE_URL}/rooms/{room.room_id}/join", "created_at": room.created_at,
                "owner_public_key_hex": a["public_key_hex"], "owner_stable_id": a["stable_id"],
                "key_log": logs[a["stable_id"]],    # P245
                "advert_signature": a["signature"], "origin_relay": rs.RELAY_BASE_URL}
        if not room_advert_verdict(item, rs.RELAY_BASE_URL, verify):
            out.append((room, {**item, "owner_address": address(room.creator_id, host),
                               "owner_fingerprint": fingerprint(a["public_key_hex"])}))
    return out


def own_topic_adverts(slugs: set[str] | None = None) -> list[dict[str, Any]]:
    """The topics here whose owner's advert still matches what is stored."""
    from emporia import relay_server as rs
    from emporia.addresses import address, fingerprint
    from emporia.identity import verify
    from emporia.wire import TOPIC_ADVERT_FIELDS, topic_advert_verdict
    host = rs._own_host()
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute(
            "SELECT a.*, t.slug, t.name, t.description, t.gate_type, t.entry_fee_cents, t.visibility, t.creator_id "
            "FROM topic_adverts a JOIN agora_topics t ON t.topic_id = a.topic_id WHERE t.is_deleted = 0 "
            "ORDER BY a.advertised_at DESC LIMIT ?", (rs._MAX_LIMIT,)).fetchall()
        logs = {sid: rs.agent_key_log(conn, sid) for sid in {a["stable_id"] for a in rows}}
    out: list[dict[str, Any]] = []
    for a in rows:
        if (slugs is not None and a["slug"] not in slugs) or not topic_may_be_advertised(a["visibility"], a["gate_type"]):
            continue
        item = {"slug": a["slug"], **{f: a[f] for f in TOPIC_ADVERT_FIELDS},
                "join_url": f"{rs.RELAY_BASE_URL}/agoras/topics/{a['slug']}",
                "owner_public_key_hex": a["public_key_hex"], "owner_stable_id": a["stable_id"],
                "key_log": logs[a["stable_id"]],    # P245
                "advert_signature": a["signature"], "origin_relay": rs.RELAY_BASE_URL}
        if not topic_advert_verdict(item, rs.RELAY_BASE_URL, verify):
            out.append({**item, "owner_address": address(a["creator_id"], host),
                        "owner_fingerprint": fingerprint(a["public_key_hex"])})
    return out


def verdict_for(kind: str, item: dict[str, Any], origin_relay: str) -> str:
    """Why this advert, from a feed served by `origin_relay`, cannot be taken as
    its owner's own — or "" when it can. `kind` is 'rooms' or 'topics'."""
    from emporia.identity import verify
    from emporia.wire import room_advert_verdict, topic_advert_verdict
    return (room_advert_verdict if kind == "rooms" else topic_advert_verdict)(item, origin_relay, verify)


def elsewhere(kind: str) -> list[dict[str, Any]]:
    """The adverts pulled from linked relays, each checked again as it is
    served. What comes back names the relay that holds the room or topic
    (`relay`, `host`), the owner (`owner_address`, `owner_fingerprint`, which is
    worked out here from the signed key) and where to join — built from the
    signed origin, never from the feed's own `join_url`."""
    from emporia import relay_server as rs
    from emporia.addresses import fingerprint, host_of
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM elsewhere_adverts WHERE kind = ? ORDER BY pulled_at DESC LIMIT ?",
                            (kind, rs._MAX_LIMIT)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        item = json.loads(r["item_json"])
        origin = str(item.get("origin_relay") or "")
        if verdict_for(kind, item, origin):
            continue
        ident = item["room_id"] if kind == "rooms" else item["slug"]
        join = f"{origin}/rooms/{ident}/join" if kind == "rooms" else f"{origin}/agoras/topics/{ident}"
        out.append({**item, "relay": origin, "host": host_of(origin), "join_url": join,
                    "owner_fingerprint": fingerprint(str(item.get("owner_public_key_hex") or "")),
                    "learned_from": r["peer_url"], "verified": True})
    return out
