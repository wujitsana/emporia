"""Outbound WebSocket tunnel to the Emporia relay.

The agent connects outbound to the relay (no open ports required). The relay
pushes inbound frames to this tunnel; the adapter routes them into Hermes.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any, Awaitable, Callable


def _ws_url(relay_url: str, agent_id: str) -> str:
    parsed = urllib.parse.urlparse(relay_url.rstrip("/"))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    # agent_id as a query param satisfies the relay's localhost-trust tier for /ws/agent/{id}
    # (see relay_server.py's _ws_agent_id()) — covers the co-located deployment this tunnel
    # is built for. A relay this agent doesn't co-locate with has no credential path here yet
    # (the Ed25519 dashboard-challenge flow was built for a human-mediated browser dashboard,
    # not this tunnel) — tracked as a fast-follow, not silently worked around.
    query = urllib.parse.urlencode({"agent_id": agent_id})
    return parsed._replace(scheme=ws_scheme).geturl() + f"/ws/agent/{agent_id}?{query}"


async def maintain_relay_tunnel(
    relay_url: str,
    agent_id: str,
    on_frame: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> None:
    """Connect to the relay WS channel and call on_frame for each inbound message.

    Reconnects with exponential backoff on disconnect. Runs indefinitely
    until the task is cancelled.
    """
    import websockets
    from websockets.exceptions import ConnectionClosedError

    url = _ws_url(relay_url, agent_id)
    backoff = initial_backoff
    while True:
        try:
            async with websockets.connect(url) as ws:
                backoff = initial_backoff
                async for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        frame = {"type": "raw", "data": raw}
                    await on_frame(frame)
        except (ConnectionClosedError, OSError):
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            return
