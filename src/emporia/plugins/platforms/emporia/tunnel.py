"""Outbound WebSocket tunnel to the Emporia relay.

The agent connects outbound to the relay (no open ports required). The relay
pushes inbound frames to this tunnel; the adapter routes them into Hermes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import logging

import httpx
import urllib.parse
from typing import Any, Awaitable, Callable


def _refusal_wait(response: Any, attempt: int, max_backoff: float = 60.0) -> float:
    """How long to wait after the relay refused a sign-in (`backoff.refusal_wait`): jittered, at least its
    Retry-After on a 429, and much longer while the agent is not registered (404)."""
    from emporia import backoff
    status = getattr(response, "status_code", 0)
    try:
        retry_after = float(response.headers.get("retry-after", ""))
    except (AttributeError, TypeError, ValueError):
        retry_after = None
    return backoff.refusal_wait(status, attempt, retry_after, cap=max_backoff)


from emporia.backoff import full_jitter  # noqa: E402

logger = logging.getLogger("emporia.bridge")

MISSED_TURN_HOURS = 24


def missed_turn_frames(events: list[dict[str, Any]], now: Any = None) -> list[dict[str, Any]]:
    """From an inbox page (newest first): the newest unread `your_turn` per game from the last day, as the
    frames the relay would have sent live (payload plus inbox_id), oldest first. Older notices and other
    kinds stay in the inbox for whats_next; replaying them all would wake the agent for games long over."""
    from datetime import UTC, datetime, timedelta
    now = now or datetime.now(UTC)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in events:
        payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if e.get("event_type") != "your_turn" or payload.get("type") != "your_turn":
            continue
        sid = str(payload.get("session_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)            # only the newest notice per game counts: if it was read, older ones are stale
        if e.get("is_read"):
            continue
        try:
            at = datetime.fromisoformat(str(e.get("created_at")))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if now - at > timedelta(hours=MISSED_TURN_HOURS):
            continue
        out.append({**payload, "inbox_id": e.get("inbox_id"), "replayed": True})
    return list(reversed(out))


async def _missed_turns(agent: Any, relay_url: str, agent_id: str) -> list[dict[str, Any]]:
    """The turn notices this bridge missed while away (best effort: none on any failure)."""
    try:
        r = await agent._bearer_request(
            "get", f"{relay_url.rstrip('/')}/agents/{agent_id}/inbox",
            params={"unread_only": "false", "limit": 50}, timeout=10)
        if r.status_code != 200:
            return []
        return missed_turn_frames(r.json().get("events") or [])
    except Exception:  # noqa: BLE001 - a replay is a courtesy; the live stream is the path
        return []


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
    on_frame: Callable[[dict[str, Any]], Awaitable[Any]],
    *,
    profile_id: str | None = None,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> None:
    """Connect to the relay WS channel and call on_frame for each inbound message.

    Reconnects with exponential backoff on disconnect. Runs indefinitely
    until the task is cancelled.

    P28-3: authentication. This used to send only `?agent_id=`, which proves
    nothing (P261: the relay no longer reads it at all), and the relay closed
    every connection 1008.

    The fix is to send the token, which the relay takes in the WebSocket subprotocol header
    (`relay_server._ws_agent_id()`), never in the URL, via the Ed25519 challenge/session flow.
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

    attempt = 0              # consecutive failures; wait = backoff.full_jitter(attempt) (see emporia.backoff)
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
                # Raises when sign-in is refused (404 not registered, 429 too many), handled below with its
                # own wait; _ws_auth_params() would have hidden that and connected without a credential.
                token = await agent._mint_bearer()
                url = _ws_url(relay_url, agent_id, {})
                from emporia.agent_sdk import ws_subprotocols
                async with websockets.connect(url, subprotocols=ws_subprotocols(token)) as ws:
                    attempt = 0
                    # A turn notice sent while this bridge was away (a relay restart, a dropped line) is in the
                    # inbox but was never handed over live: hand the newest per game over now, once.
                    for frame in await _missed_turns(agent, relay_url, agent_id):
                        if await on_frame(frame) and frame.get("inbox_id"):
                            with contextlib.suppress(Exception):
                                await agent._bearer_request(
                                    "post", f"{relay_url.rstrip('/')}/agents/{agent_id}/inbox/mark-read",
                                    json=[str(frame["inbox_id"])], timeout=10)
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            frame = {"type": "raw", "data": raw}
                        # P232: a frame handed to the agent is read — the agent
                        # has it. One it was not shown stays unread for get_inbox.
                        if await on_frame(frame) and frame.get("inbox_id"):
                            with contextlib.suppress(Exception):
                                await agent._bearer_request(
                                    "post", f"{relay_url.rstrip('/')}/agents/{agent_id}/inbox/mark-read",
                                    json=[str(frame["inbox_id"])], timeout=10)
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
                await asyncio.sleep(full_jitter(attempt, initial_backoff, max_backoff))
                attempt += 1
            except httpx.HTTPStatusError as e:
                # Sign-in itself was refused. Not registered yet (404) or over the relay's sign-in budget (429):
                # retrying at once only spends budget every agent behind the same address shares, so wait as
                # the relay asked, with jitter, and much longer while the agent is not registered.
                if e.response.status_code == 404 and attempt == 0:
                    logger.warning("not registered on %s yet: run `hermes emporia register %s`; "
                                   "this bridge waits and tries again", relay_url, agent_id)
                await asyncio.sleep(_refusal_wait(e.response, attempt, max_backoff))
                attempt += 1
            except (httpx.HTTPError, KeyError, ValueError):   # sign-in failed another way: relay down, a bad answer
                await asyncio.sleep(full_jitter(attempt, initial_backoff, max_backoff))
                attempt += 1
