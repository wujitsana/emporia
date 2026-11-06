"""What a room or topic may be named as to other relays.

Adverts (a creator's signed advert, served to linked relays and pulled by them) are gone: a room or topic
is found from other relays by a pointer post (`emporia:post:v2`, kind `pointer`, a `ref` tag naming it).
What a pointer may name is decided here, once: a public open room, a paid room (however it is listed), a
public topic that is not invite-only. A private invite-only room or topic is never named to anyone who is
not in it.

`own_room_adverts` and `elsewhere` stay only as empty answers, for the room routes that still call them
until room messages are next reworked; nothing is stored or pulled any more.
"""

from __future__ import annotations

from typing import Any


def room_may_be_advertised(room_type: str, gate_type: str) -> bool:
    """A public open room, or a paid one — which is `private` because only those
    who paid are in it, but whose terms its creator may choose to show. An
    invite-only room is never shown."""
    return gate_type == "paid" or (room_type == "public" and gate_type == "open")


def topic_may_be_advertised(visibility: str, gate_type: str) -> bool:
    return visibility == "public" and gate_type != "invite"


def own_room_adverts(room_ids: set[str] | None = None) -> list[tuple[Any, dict[str, Any]]]:
    return []


def elsewhere(kind: str) -> list[dict[str, Any]]:
    return []
