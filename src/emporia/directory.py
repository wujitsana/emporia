"""The relay directory and the relay card's shared-topic fields (protocol 4).

A **directory** is a list of signed relay cards, nothing more: the cards this relay has fetched from the relays it
links to and checked (each signed by the key pinned for that relay, on the protocol this one speaks), plus its own,
and only those whose relay chose to be listed (`listed: true`, `EMPORIA_DIRECTORY_LISTED`). A relay serves one when
its operator turns it on (`EMPORIA_DIRECTORY`). It adds nothing of its own, so it cannot make a relay up; a card's
claims are the relay's own word (the agent count is shown as "says"), and an agent may read several directories and
compare. The list is signed by the serving relay; each card by the relay it describes.

Pure where it can be; the live card and the stored peer cards come from `relay_server`.
"""

from __future__ import annotations

import os
from typing import Any

DIRECTORY_SETTING = "EMPORIA_DIRECTORY"
LISTED_SETTING = "EMPORIA_DIRECTORY_LISTED"
_ON = ("1", "on", "true", "yes")


def _on(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _ON


def serves() -> bool:
    """Whether this relay serves a directory (`EMPORIA_DIRECTORY`)."""
    return _on(os.getenv("EMPORIA_DIRECTORY"))


def listed() -> bool:
    """Whether this relay's card asks to be listed in directories (`EMPORIA_DIRECTORY_LISTED`)."""
    return _on(os.getenv("EMPORIA_DIRECTORY_LISTED"))


def card_fields() -> dict[str, Any]:
    """The fields card v2 adds: `listed`, `agents` (self-reported: shown as "says"), `shared_topics`, `links` (the
    relays it links to, by relay id, those whose card it has verified), `protocol`, `leagues` (none yet)."""
    from emporia import relay_server as rs
    from emporia import shared_topics
    try:
        with rs.DB_LOCK, rs.get_db() as conn:
            agents = conn.execute("SELECT COUNT(*) FROM authorized_agents WHERE is_active = 1").fetchone()[0]
    except Exception:  # noqa: BLE001
        agents = 0
    links = sorted({str(c.get("relay_id")) for p, c in rs._FEDERATION_PEER_CARDS.items()
                    if p in rs.FEDERATED_RELAYS and c.get("relay_id")})
    return {"listed": listed(), "directory": serves(), "agents": agents,
            "shared_topics": [{"slug": t["slug"], "title": t["title"]} for t in shared_topics.host_topics()],
            "links": links, "protocol": rs.CARD_VERSION, "leagues": []}


def entries() -> list[dict[str, Any]]:
    """The cards this relay lists: its own (when listed and not closed) and each linked relay's verified card that
    asks to be listed. A card is re-verified here, so one that changed since it was kept is left out."""
    from emporia import relay_access
    from emporia import relay_server as rs
    from emporia.identity import verify_relay_card
    out = []
    if listed() and relay_access.mode() != "closed":
        out.append(rs.build_relay_card())
    for peer in rs.FEDERATED_RELAYS:
        card = rs._FEDERATION_PEER_CARDS.get(peer)
        if (isinstance(card, dict) and card.get("listed") is True and verify_relay_card(card)["ok"]
                and card.get("relay_public_key") == rs._FEDERATION_PEER_PUBKEYS.get(peer)
                and not rs._old_protocol(card)):
            out.append(card)
    return out
