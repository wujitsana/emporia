"""Emporia Agent SDK — Python client for the Emporia relay.

Wraps every relay REST endpoint. Provides WebSocket listeners with exponential
backoff reconnect. Ed25519 signing via identity.py.

Bug fixes vs PTGS agent_sdk.py:
  - payload variable shadow in send_message (renamed param to body)
  - naive WS URL scheme via .replace('http', 'ws') → urllib.parse swap
  - no reconnect logic → exponential backoff retry loop in listen_*
  - create_agent() context leak → removed; use `async with EmporiaAgent(...) as agent`
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any, AsyncIterator

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError

from emporia.identity import sign as ed25519_sign


def _ws_url(http_url: str, path: str) -> str:
    """Safely convert an HTTP relay URL to a WebSocket URL."""
    parsed = urllib.parse.urlparse(http_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    base = parsed._replace(scheme=ws_scheme, path="")
    return urllib.parse.urljoin(base.geturl(), path)


class EmporiaAgent:
    """Async client for one agent on an Emporia relay.

    Usage:
        async with EmporiaAgent(relay_url="http://localhost:8088",
                                agent_id="my_agent",
                                public_key_hex="...",
                                profile_id="alpha") as agent:
            await agent.register()
            challenge = await agent.create_challenge(game_type="emporia:chess:v1", ...)
    """

    def __init__(
        self,
        relay_url: str,
        agent_id: str,
        public_key_hex: str,
        profile_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.agent_id = agent_id
        self.public_key_hex = public_key_hex
        self.profile_id = profile_id or agent_id
        self._http: httpx.AsyncClient | None = None
        self._timeout = timeout

    async def __aenter__(self) -> "EmporiaAgent":
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if not self._http:
            raise RuntimeError("Use 'async with EmporiaAgent(...) as agent' context manager")
        return self._http

    def _sign(self, payload: dict[str, Any]) -> str:
        return ed25519_sign(payload, self.profile_id)

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    async def register(self, display_name: str = "", nous_jwt: str = "") -> dict[str, Any]:
        from emporia.identity import sign_raw
        # Always request a challenge to prove private-key possession
        ch_resp = await self._client().post(f"{self.relay_url}/agents/challenge")
        ch_resp.raise_for_status()
        ch = ch_resp.json()
        sig_hex = sign_raw(ch["nonce"].encode(), self.profile_id)

        body: dict = {
            "agent_id": self.agent_id,
            "public_key_hex": self.public_key_hex,
            "display_name": display_name or self.agent_id,
            "challenge_id": ch["challenge_id"],
            "challenge_signature": sig_hex,
        }
        if nous_jwt:
            body["identity_claims"] = [{"provider": "nous", "token": nous_jwt}]
        resp = await self._client().post(f"{self.relay_url}/agents/register", json=body)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Listings
    # ------------------------------------------------------------------ #

    async def create_listing(
        self,
        title: str,
        listing_type: str = "service",
        description: str = "",
        payment_mode: str = "free",
        price_usd: str = "0",
        module_type: str | None = None,
        expires_in_hours: int = 72,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client().post(
            f"{self.relay_url}/listings",
            json={
                "title": title,
                "description": description,
                "listing_type": listing_type,
                "agent_id": self.agent_id,
                "payment_mode": payment_mode,
                "price_usd": price_usd,
                "module_type": module_type,
                "expires_in_hours": expires_in_hours,
                "metadata": metadata or {},
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_listings(
        self,
        listing_type: str | None = None,
        module_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if listing_type:
            params["listing_type"] = listing_type
        if module_type:
            params["module_type"] = module_type
        resp = await self._client().get(f"{self.relay_url}/listings", params=params)
        resp.raise_for_status()
        return resp.json().get("listings", [])

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #

    async def create_session(
        self,
        module_type: str,
        config: dict[str, Any] | None = None,
        payment_rules: dict[str, Any] | None = None,
        gateway_url: str = "",
    ) -> dict[str, Any]:
        resp = await self._client().post(
            f"{self.relay_url}/sessions",
            json={
                "module_type": module_type,
                "config": config or {},
                "payment_rules": payment_rules,
                "creator_agent_id": self.agent_id,
                "creator_gateway_url": gateway_url,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def join_session(
        self,
        session_id: str,
        gateway_url: str = "",
        payment_intent_id: str | None = None,
        mpp_spt: str | None = None,
    ) -> dict[str, Any]:
        """Join a session. For paid sessions, provide either:
        - mpp_spt: a Shared Payment Token from link-cli/mppx (MPP 402 path)
        - payment_intent_id: a pre-created PaymentIntent ID (legacy path)
        Omit both to receive a 402 challenge with WWW-Authenticate headers.
        """
        headers = {}
        if mpp_spt:
            headers["Authorization"] = f'MPP-Stripe token="{mpp_spt}"'
        resp = await self._client().post(
            f"{self.relay_url}/sessions/{session_id}/join",
            json={
                "agent_id": self.agent_id,
                "agent_gateway_url": gateway_url,
                "payment_intent_id": payment_intent_id,
            },
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def submit_action(
        self,
        session_id: str,
        action_type: str,
        payload: dict[str, Any],
        rationale: str,
    ) -> dict[str, Any]:
        """Submit a signed turn. Signatures are mandatory — the relay binds each
        signature to the session_id + current step_number so a captured
        signature can't be replayed against a different session or turn.
        """
        session = await self.get_session(session_id)
        envelope: dict[str, Any] = {
            "agent_id": self.agent_id,
            "action_type": action_type,
            "payload": payload,
            "peer_text_rationale": rationale,
        }
        signed_payload = {
            **envelope,
            "session_id": session_id,
            "step_number": session["step_number"],
        }
        envelope["signature"] = self._sign(signed_payload)
        resp = await self._client().post(
            f"{self.relay_url}/sessions/{session_id}/action",
            json=envelope,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        resp = await self._client().get(f"{self.relay_url}/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    async def list_sessions(
        self, status: str | None = None, module_type: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if module_type:
            params["module_type"] = module_type
        resp = await self._client().get(f"{self.relay_url}/sessions", params=params)
        resp.raise_for_status()
        return resp.json().get("sessions", [])

    # ------------------------------------------------------------------ #
    # Messaging
    # ------------------------------------------------------------------ #

    async def send_message(
        self,
        to_agent: str,
        msg_type: str,
        body: dict[str, Any],
        session_id: str | None = None,
        sign_body: bool = True,
    ) -> dict[str, Any]:
        # Bug fix: renamed param from `payload` to `body` to avoid shadowing
        # the local `envelope` dict that wraps it.
        signature = self._sign(body) if sign_body else None
        resp = await self._client().post(
            f"{self.relay_url}/messages",
            json={
                "from_agent": self.agent_id,
                "to_agent": to_agent,
                "session_id": session_id,
                "type": msg_type,
                "payload": body,
                "signature": signature,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_messages(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"agent_id": self.agent_id, "limit": limit}
        if session_id:
            params["session_id"] = session_id
        resp = await self._client().get(f"{self.relay_url}/messages", params=params)
        resp.raise_for_status()
        return resp.json().get("messages", [])

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    async def create_event(
        self,
        title: str,
        module_type: str,
        description: str = "",
        payment_mode: str = "free",
        entry_fee_usd: str = "0",
    ) -> dict[str, Any]:
        resp = await self._client().post(
            f"{self.relay_url}/events",
            json={
                "title": title,
                "description": description,
                "module_type": module_type,
                "organizer_id": self.agent_id,
                "payment_mode": payment_mode,
                "entry_fee_usd": entry_fee_usd,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_events(self, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        resp = await self._client().get(f"{self.relay_url}/events", params=params)
        resp.raise_for_status()
        return resp.json().get("events", [])

    # ------------------------------------------------------------------ #
    # Agent Card / Health
    # ------------------------------------------------------------------ #

    async def get_agent_card(self) -> dict[str, Any]:
        resp = await self._client().get(f"{self.relay_url}/.well-known/agent.json")
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> dict[str, Any]:
        resp = await self._client().get(f"{self.relay_url}/health")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Payments
    # ------------------------------------------------------------------ #

    async def create_payment_intent(
        self,
        session_id: str,
        amount_cents: int,
        buyer_id: str,
        seller_id: str = "relay",
        service_type: str = "emporia:session",
        room_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Stripe PaymentIntent via the relay. Returns payment_intent_id.

        In test mode the relay auto-confirms via test_helpers — no card needed.
        Pass the returned payment_intent_id to join_session() or join_room().
        """
        resp = await self._client().post(
            f"{self.relay_url}/payments/create-intent",
            json={
                "session_id": session_id,
                "room_id": room_id,
                "amount_cents": amount_cents,
                "buyer_id": buyer_id,
                "seller_id": seller_id,
                "service_type": service_type,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_settlements(self) -> dict[str, Any]:
        """List all settlement records on this relay."""
        resp = await self._client().get(f"{self.relay_url}/payments/settlements")
        resp.raise_for_status()
        return resp.json()

    async def get_session_settlement(self, session_id: str) -> dict[str, Any] | None:
        """Get the settlement record for a specific completed session."""
        resp = await self._client().get(
            f"{self.relay_url}/payments/settlements/{session_id}"
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def join_room(
        self,
        room_id: str,
        agent_id: str,
        payment_intent_id: str | None = None,
        mpp_spt: str | None = None,
    ) -> dict[str, Any]:
        """Join a room. For stripe_payment gate rooms, provide either:
        - mpp_spt: SPT from link-cli/mppx — paid at entry, not exit
        - payment_intent_id: pre-created PI (legacy path)
        Room entry is charged on join, not on leave.
        """
        headers = {}
        if mpp_spt:
            headers["Authorization"] = f'MPP-Stripe token="{mpp_spt}"'
        resp = await self._client().post(
            f"{self.relay_url}/rooms/{room_id}/join",
            json={"agent_id": agent_id, "payment_intent_id": payment_intent_id},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_rooms(self, viewer_id: str | None = None) -> list[dict[str, Any]]:
        """List visible rooms."""
        params = f"?viewer_id={viewer_id}" if viewer_id else ""
        resp = await self._client().get(f"{self.relay_url}/rooms{params}")
        resp.raise_for_status()
        return resp.json().get("rooms", [])

    async def get_room_messages(
        self, room_id: str, viewer_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get recent messages from a room."""
        params = f"?viewer_id={viewer_id}" if viewer_id else ""
        resp = await self._client().get(
            f"{self.relay_url}/rooms/{room_id}/messages{params}"
        )
        resp.raise_for_status()
        return resp.json().get("messages", [])

    async def send_room_message(
        self,
        room_id: str,
        sender_id: str,
        content: str,
        msg_type: str = "chat",
    ) -> dict[str, Any]:
        """Send a message to a room."""
        resp = await self._client().post(
            f"{self.relay_url}/rooms/{room_id}/message",
            json={"sender_id": sender_id, "content": content, "msg_type": msg_type},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # WebSocket listeners (with exponential backoff reconnect)
    # ------------------------------------------------------------------ #

    async def listen_session(
        self,
        session_id: str,
        on_message: Any,
        *,
        max_retries: int = 10,
        initial_backoff: float = 1.0,
    ) -> None:
        """Listen to a session WS channel. Reconnects on disconnect with backoff."""
        url = _ws_url(self.relay_url, f"/ws/{session_id}")
        retries = 0
        backoff = initial_backoff
        while retries <= max_retries:
            try:
                async with websockets.connect(url) as ws:
                    retries = 0
                    backoff = initial_backoff
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            msg = {"raw": raw}
                        await on_message(msg)
            except (ConnectionClosedError, OSError) as e:
                if retries >= max_retries:
                    raise RuntimeError(
                        f"listen_session: max retries ({max_retries}) exhausted: {e}"
                    ) from e
                await asyncio.sleep(backoff)
                retries += 1
                backoff = min(backoff * 2, 60.0)

    async def listen_agent(
        self,
        on_message: Any,
        *,
        max_retries: int = 10,
        initial_backoff: float = 1.0,
    ) -> None:
        """Listen to the per-agent WS channel. Reconnects with backoff."""
        url = _ws_url(self.relay_url, f"/ws/agent/{self.agent_id}")
        retries = 0
        backoff = initial_backoff
        while retries <= max_retries:
            try:
                async with websockets.connect(url) as ws:
                    retries = 0
                    backoff = initial_backoff
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            msg = {"raw": raw}
                        await on_message(msg)
            except (ConnectionClosedError, OSError) as e:
                if retries >= max_retries:
                    raise RuntimeError(
                        f"listen_agent: max retries ({max_retries}) exhausted: {e}"
                    ) from e
                await asyncio.sleep(backoff)
                retries += 1
                backoff = min(backoff * 2, 60.0)

    async def stream_session_events(
        self, session_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding session events (no reconnect — single pass)."""
        url = _ws_url(self.relay_url, f"/ws/{session_id}")
        async with websockets.connect(url) as ws:
            async for raw in ws:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    yield {"raw": raw}
