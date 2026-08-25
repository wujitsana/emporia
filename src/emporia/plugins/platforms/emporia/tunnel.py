"""Outbound WebSocket tunnel to the Emporia relay.

The agent connects outbound to the relay (no open ports required). The relay
pushes inbound frames to this tunnel; the adapter routes them into Hermes.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any, Awaitable, Callable


def _ws_url(relay_url: str, agent_id: str, params: dict[str, str]) -> str:
    """Build the /ws/agent/{id} connect URL carrying `params` as the query string.

    P28-3: `params` is passed in rather than derived here, because the credential
    it carries has to be minted fresh per connect attempt — see
    `maintain_relay_tunnel()`.
    """
    parsed = urllib.parse.urlparse(relay_url.rstrip("/"))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urllib.parse.urlencode(params)
    return parsed._replace(scheme=ws_scheme).geturl() + f"/ws/agent/{agent_id}?{query}"


async def maintain_relay_tunnel(
    relay_url: str,
    agent_id: str,
    on_frame: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    profile_id: str | None = None,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> None:
    """Connect to the relay WS channel and call on_frame for each inbound message.

    Reconnects with exponential backoff on disconnect. Runs indefinitely
    until the task is cancelled.

    P28-3: authentication. This used to send only `?agent_id=`, which the relay
    honours solely for hosts in `_HEADER_AUTH_TRUSTED_HOSTS` — and that set is
    *empty* unless the operator sets `EMPORIA_TRUST_LOCAL_HEADER_AUTH=1`, which
    is not the shipped default. So the tunnel authenticated nowhere on a default
    install, co-located or not, and the relay closed every connection 1008. The
    old comment here framed it as a non-co-located-only gap; it wasn't.

    The fix is to send `?token=` as well, which the relay has always accepted
    (`relay_server._ws_agent_id()`), via the Ed25519 challenge/session flow.
    `agent_sdk.EmporiaAgent` already owns that flow — caching, refresh-before-
    expiry, invalidate-and-remint — and this reuses it rather than restating it,
    because a second hand-written copy of a credential path is precisely the
    drift this codebase keeps finding (the SDK fixed this same defect in N6
    review R4 and the tunnel never got it).

    Two properties the SDK learned the hard way and this now shares: the URL is
    rebuilt on *every* attempt, so a reconnect after a long outage mints a fresh
    token instead of replaying an expired one (N7 review R5); and a rejected
    handshake invalidates the cached credential before retrying, rather than
    replaying a token the relay has already refused (N8 review R5).
    """
    import websockets
    from websockets.exceptions import ConnectionClosedError, InvalidHandshake

    from emporia.agent_sdk import EmporiaAgent

    backoff = initial_backoff
    async with EmporiaAgent(
        relay_url=relay_url,
        agent_id=agent_id,
        # Only ever used to mint a session token here; registration (the one
        # thing that needs the public key) is not this component's job.
        public_key_hex="",
        profile_id=profile_id or agent_id,
    ) as agent:
        while True:
            try:
                url = _ws_url(relay_url, agent_id, await agent._ws_auth_params())
                async with websockets.connect(url) as ws:
                    backoff = initial_backoff
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            frame = {"type": "raw", "data": raw}
                        await on_frame(frame)
            except asyncio.CancelledError:
                return
            except (ConnectionClosedError, OSError, InvalidHandshake) as e:
                # A rejected *handshake* means the credential was refused, so
                # re-minting is the only thing that can change the outcome.
                #
                # P29-1: attempted on EVERY rejected handshake, throttled by the
                # exponential backoff below rather than by a once-per-cycle flag.
                # The SDK's listen_agent() uses such a flag safely because it has
                # a max_retries budget and eventually raises, which bounds the
                # flag's life. This loop runs forever by design, so a flag
                # cleared only by a *successful* connect is never cleared at all
                # once the first re-mint fails — a blip on /dashboard/challenge,
                # precisely the transient this path exists to recover from —
                # leaving the tunnel replaying a refused credential for the life
                # of the process. Backoff already caps the worst case at one
                # auth attempt per max_backoff, which is not a loop worth
                # trading permanent death for.
                if isinstance(e, InvalidHandshake):
                    await agent._invalidate_bearer_and_remint()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
