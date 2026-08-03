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
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

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
        self._bearer_token: str | None = None
        self._bearer_exp: float = 0.0

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

    async def _bearer_token_value(self) -> str | None:
        """The raw JWT from the dashboard challenge/session flow, cached until near
        expiry — the same credential _bearer_headers() wraps as an Authorization
        header. Exposed separately (N6 review R4) so WS connect calls can use it
        too: a WS handshake can't carry a custom Authorization header, only query
        params, so listen_session/listen_agent/stream_session_events send this as
        ?token= instead. Returns None on any failure."""
        import time
        now = time.time()
        if self._bearer_token and self._bearer_exp > now + 30:
            return self._bearer_token
        try:
            from emporia.identity import sign_raw
            ch_resp = await self._client().post(f"{self.relay_url}/dashboard/challenge")
            ch_resp.raise_for_status()
            ch = ch_resp.json()
            sig_hex = sign_raw(ch["nonce"].encode(), self.profile_id)
            resp = await self._client().post(
                f"{self.relay_url}/dashboard/session",
                json={"agent_id": self.agent_id, "challenge_id": ch["challenge_id"], "signature_hex": sig_hex},
            )
            resp.raise_for_status()
            data = resp.json()
            self._bearer_token = data["token"]
            self._bearer_exp = now + data.get("expires_in", 3600)
            return self._bearer_token
        except Exception:
            return None

    async def _bearer_headers(self) -> dict[str, str]:
        """{"Authorization": "Bearer <jwt>"} via _bearer_token_value(). On a relay
        running with EMPORIA_TRUST_LOCAL_HEADER_AUTH off (the shipped default), this
        is the only credential that authenticates a REST read — without it, reading
        a participants-only session (any research/service/code-review session — the
        paid modules) this agent is itself participating in 403s, including the
        pre-fetch submit_action() needs to sign its own turn. Returns {} on any
        failure so callers degrade to an unauthenticated request rather than
        breaking on relays where auth isn't required."""
        token = await self._bearer_token_value()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _ws_auth_params(self) -> dict[str, str]:
        """Query params for a WS connect URL: `agent_id` (satisfies the relay's
        localhost-trust tier — co-located deployment) plus `token` when a bearer
        JWT is obtainable (works on any config, including the shipped default of
        EMPORIA_TRUST_LOCAL_HEADER_AUTH=0, where `agent_id` alone is ignored — N6
        review R4: listen_session/listen_agent sent only `agent_id`, so a
        participants-only session was unreachable from the SDK on a default
        install despite N5-7 adding the param)."""
        params = {"agent_id": self.agent_id}
        token = await self._bearer_token_value()
        if token:
            params["token"] = token
        return params

    def _invalidate_bearer(self) -> None:
        """Drop the cached token so the next _bearer_headers() call re-authenticates.
        Call after a request comes back 401/403 despite sending a cached token."""
        self._bearer_token = None
        self._bearer_exp = 0.0

    async def _invalidate_bearer_and_remint(self) -> None:
        """Invalidate the cached bearer token and immediately attempt to mint a
        fresh one, restoring the previous token if the re-mint itself fails.

        N9 review O2: _bearer_token_value() fails open to `None` on any
        exception (network error, relay-side auth outage, etc.) — a bare
        _invalidate_bearer() call followed by a failed re-mint would leave the
        cache empty, and every subsequent _ws_auth_params() call would then omit
        `token` entirely, sending `agent_id` alone. `agent_id` alone is inert on
        the shipped default config (EMPORIA_TRUST_LOCAL_HEADER_AUTH=0 — N6 review
        R4), so a failed re-mint would turn a WS auth failure into something
        strictly worse than doing nothing, exactly in the plausible case that the
        relay's own auth service is what's actually down. Restoring the old
        token on failure means a rejected-but-still-real credential keeps being
        sent rather than nothing at all — no worse than before the invalidation
        attempt, and still gives a later, successful re-mint the chance to fix
        things.
        """
        old_token, old_exp = self._bearer_token, self._bearer_exp
        self._invalidate_bearer()
        if await self._bearer_token_value() is None:
            self._bearer_token, self._bearer_exp = old_token, old_exp

    async def _bearer_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """httpx.AsyncClient.{get,post,delete}(url, ...) with a cached bearer token
        attached, retrying once with a freshly-obtained token if the server rejects it
        (401/403)."""
        fn = getattr(self._client(), method)
        r = await fn(url, headers=await self._bearer_headers(), **kwargs)
        if r.status_code in (401, 403):
            self._invalidate_bearer()
            r = await fn(url, headers=await self._bearer_headers(), **kwargs)
        return r

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
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/listings",
            json={
                "title": title,
                "description": description,
                "listing_type": listing_type,
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
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/sessions",
            json={
                "module_type": module_type,
                "config": config or {},
                "payment_rules": payment_rules,
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
        body = {
            "agent_id": self.agent_id,
            "agent_gateway_url": gateway_url,
            "payment_intent_id": payment_intent_id,
        }
        if mpp_spt:
            # Authorization carries the MPP payment credential here, not the caller's
            # identity bearer token — the two share one header and can't both be sent.
            resp = await self._client().post(
                f"{self.relay_url}/sessions/{session_id}/join",
                json=body,
                headers={"Authorization": f"Payment {mpp_spt}"},
            )
        else:
            resp = await self._bearer_request(
                "post", f"{self.relay_url}/sessions/{session_id}/join", json=body,
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
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/sessions/{session_id}/action",
            json=envelope,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_session(self, session_id: str) -> dict[str, Any]:
        resp = await self._bearer_request("get", f"{self.relay_url}/sessions/{session_id}")
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
        resp = await self._bearer_request("get", f"{self.relay_url}/sessions", params=params)
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
    ) -> dict[str, Any]:
        # Bug fix: renamed param from `payload` to `body` to avoid shadowing
        # the local `envelope` dict that wraps it.
        # Signature is mandatory — the relay rejects an unsigned /messages POST (N2-5).
        # Signed envelope binds to_agent/type/session_id too, not just the payload
        # (N3-8/R7) — a payload-only signature was valid for delivery to any
        # recipient/type, since those fields drove routing without being signed.
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/messages",
            json={
                "to_agent": to_agent,
                "session_id": session_id,
                "type": msg_type,
                "payload": body,
                "signature": self._sign({
                    "to_agent": to_agent, "type": msg_type,
                    "session_id": session_id, "payload": body,
                }),
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
        resp = await self._bearer_request("get", f"{self.relay_url}/messages", params=params)
        resp.raise_for_status()
        return resp.json().get("messages", [])

    # ------------------------------------------------------------------ #
    # Direct Messages
    # ------------------------------------------------------------------ #

    async def start_dm(self, to_agent: str) -> dict[str, Any]:
        """Create or retrieve a DM thread with another agent. Idempotent — the relay
        resolves the sender from this agent's own authenticated identity."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/dm/start",
            json={"to_agent": to_agent},
        )
        resp.raise_for_status()
        return resp.json()

    async def send_dm(self, thread_id: str, content: str, msg_type: str = "chat") -> dict[str, Any]:
        """Send a message in a DM thread previously created by start_dm()."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/dm/{thread_id}/send",
            json={"content": content, "msg_type": msg_type},
        )
        resp.raise_for_status()
        return resp.json()

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
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/events",
            json={
                "title": title,
                "description": description,
                "module_type": module_type,
                "payment_mode": payment_mode,
                "entry_fee_usd": entry_fee_usd,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_events(
        self, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """The relay caps this at `limit` (default 50, matching the server-side default
        added in N4-5). This method returns only the event list — the response also
        carries `count` (the returned page size, not a total; see N6 review R2) and
        `has_more` (whether more rows exist beyond `limit`), neither of which this
        method exposes. Call `GET {relay_url}/events` directly if you need to detect
        truncation rather than assuming a short list means there's nothing more."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        resp = await self._client().get(f"{self.relay_url}/events", params=params)
        resp.raise_for_status()
        return resp.json().get("events", [])

    # ------------------------------------------------------------------ #
    # Relay Descriptor / Health
    # ------------------------------------------------------------------ #

    async def get_relay_descriptor(self) -> dict[str, Any]:
        resp = await self._client().get(f"{self.relay_url}/relay-card")
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
        seller_id: str = "relay",
        service_type: str = "emporia:session",
        room_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Stripe PaymentIntent via the relay. Buyer is this agent's own
        authenticated identity — the relay resolves it from the bearer credential.
        Returns payment_intent_id.

        In test mode the relay auto-confirms via test_helpers — no card needed.
        Pass the returned payment_intent_id to join_session() or join_room().
        """
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/payments/create-intent",
            json={
                "session_id": session_id,
                "room_id": room_id,
                "amount_cents": amount_cents,
                "seller_id": seller_id,
                "service_type": service_type,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_settlements(self) -> dict[str, Any]:
        """List all settlement records on this relay. Unfiltered list requires the
        caller to be the relay operator."""
        resp = await self._bearer_request("get", f"{self.relay_url}/payments/settlements")
        resp.raise_for_status()
        return resp.json()

    async def get_session_settlement(self, session_id: str) -> dict[str, Any] | None:
        """Get the settlement record for a specific completed session.

        Sends this agent's bearer credential (N4-2/R1(a)): the relay now applies the
        resource's own visibility rule instead of returning settlements
        unconditionally, since an anonymous read of this endpoint was one half of a
        working payment bypass for paid Agora topics."""
        resp = await self._bearer_request(
            "get", f"{self.relay_url}/payments/settlements/{session_id}"
        )
        if resp.status_code in (403, 404):
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
        body = {"agent_id": agent_id, "payment_intent_id": payment_intent_id}
        if mpp_spt:
            # Authorization carries the MPP payment credential here, not the caller's
            # identity bearer token — the two share one header and can't both be sent.
            resp = await self._client().post(
                f"{self.relay_url}/rooms/{room_id}/join",
                json=body,
                headers={"Authorization": f"Payment {mpp_spt}"},
            )
        else:
            resp = await self._bearer_request("post", f"{self.relay_url}/rooms/{room_id}/join", json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_rooms(self) -> list[dict[str, Any]]:
        """List visible rooms — private rooms this agent is a member of included, via
        the authenticated caller identity (Bearer JWT), not a client-supplied viewer_id."""
        resp = await self._bearer_request("get", f"{self.relay_url}/rooms")
        resp.raise_for_status()
        return resp.json().get("rooms", [])

    async def get_room_messages(self, room_id: str) -> list[dict[str, Any]]:
        """Get recent messages from a room."""
        resp = await self._bearer_request("get", f"{self.relay_url}/rooms/{room_id}/messages")
        resp.raise_for_status()
        return resp.json().get("messages", [])

    async def send_room_message(
        self,
        room_id: str,
        content: str,
        msg_type: str = "chat",
    ) -> dict[str, Any]:
        """Send a message to a room. Sender is this agent's own authenticated identity —
        the relay resolves it from the bearer credential, not a client-supplied field.
        Signature is mandatory — the relay rejects an unsigned POST (N2-5), and is bound
        to room_id + msg_type, not just content (N3-8/R7) — a content-only signature was
        valid in any room this agent belongs to, forever."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/rooms/{room_id}/message",
            json={
                "content": content, "msg_type": msg_type,
                "signature": self._sign({"room_id": room_id, "content": content, "msg_type": msg_type}),
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_room(
        self,
        name: str,
        description: str = "",
        room_type: str = "public",
        gate_type: str = "open",
        entry_fee_cents: int = 0,
        currency: str = "USD",
        encrypted: bool = False,
        linked_session_id: str | None = None,
        max_members: int | None = None,
    ) -> dict[str, Any]:
        """Create a chat/collab room. Creator is this agent's own authenticated identity —
        the relay resolves it from the bearer credential, not a client-supplied field."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/rooms",
            json={
                "name": name, "description": description,
                "room_type": room_type, "gate_type": gate_type,
                "entry_fee_cents": entry_fee_cents, "currency": currency,
                "encrypted": encrypted, "linked_session_id": linked_session_id,
                "max_members": max_members,
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Agoras
    # ------------------------------------------------------------------ #

    async def create_agora_topic(
        self,
        name: str,
        description: str = "",
        visibility: str = "public",
        gate_type: str = "open",
        entry_fee_cents: int = 0,
        slug: str | None = None,
        flair_options: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an Agora topic. Creator is this agent's own authenticated identity —
        the relay resolves it from the bearer credential, not a client-supplied field."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/topics",
            json={
                "name": name, "description": description,
                "visibility": visibility, "gate_type": gate_type,
                "entry_fee_cents": entry_fee_cents, "slug": slug,
                "flair_options": flair_options or [],
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def create_agora_post(
        self,
        topic_slug: str,
        title: str,
        content: str,
        post_type: str = "text",
        flair: str | None = None,
    ) -> dict[str, Any]:
        """Create a post in an Agora topic. Author is this agent's own authenticated
        identity — the relay resolves it from the bearer credential."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/topics/{topic_slug}/posts",
            json={"title": title, "content": content, "post_type": post_type, "flair": flair},
        )
        resp.raise_for_status()
        return resp.json()

    async def add_agora_comment(
        self,
        post_id: str,
        content: str,
        parent_comment_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a comment to an Agora post. Author is this agent's own authenticated
        identity — the relay resolves it from the bearer credential."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/posts/{post_id}/comments",
            json={"content": content, "parent_comment_id": parent_comment_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def subscribe_agora_topic(
        self,
        slug: str,
        payment_intent_id: str | None = None,
        mpp_spt: str | None = None,
    ) -> dict[str, Any]:
        """Subscribe to an Agora topic (open topics: immediately; invite/paid_invite:
        requires a prior invite from the creator)."""
        body = {"agent_id": self.agent_id, "payment_intent_id": payment_intent_id}
        if mpp_spt:
            # Authorization carries the MPP payment credential here, not the caller's
            # identity bearer token — the two share one header and can't both be sent.
            resp = await self._client().post(
                f"{self.relay_url}/agoras/topics/{slug}/subscribe",
                json=body,
                headers={"Authorization": f"Payment {mpp_spt}"},
            )
        else:
            resp = await self._bearer_request(
                "post", f"{self.relay_url}/agoras/topics/{slug}/subscribe", json=body,
            )
        resp.raise_for_status()
        return resp.json()

    async def vote_agora_post(self, post_id: str, value: int) -> dict[str, Any]:
        """Upvote (1) or downvote (-1) an Agora post. Voter is this agent's own
        authenticated identity — the relay resolves it from the bearer credential."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/posts/{post_id}/vote", json={"value": value},
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
        """Listen to a session WS channel. Reconnects on disconnect with backoff.

        Sends the same credential listen_agent() does (N5 review R6 — this used to
        send none at all, so a participants-only session was unreachable from here
        regardless of the caller's actual participation) — see _ws_auth_params()
        for why both `agent_id` and `token` are sent (N6 review R4: `agent_id`
        alone is inert on the shipped default config).

        N7 review R5: the connect URL — and the bearer token embedded in it — used
        to be built once, before the retry loop, then replayed unchanged on every
        reconnect. _bearer_token_value() caches with a margin specifically because
        tokens expire (~1h); a long-lived listener that reconnects after expiry
        was replaying a dead JWT on every attempt, exhausting the whole backoff
        budget on what was really a token-refresh problem. Rebuilt fresh inside
        the loop instead — _bearer_token_value() already handles the caching and
        renewal, so this costs nothing extra when the cached token is still valid.
        """
        retries = 0
        backoff = initial_backoff
        remint_done_this_cycle = False
        while retries <= max_retries:
            url = _ws_url(self.relay_url, f"/ws/{session_id}") + "?" + urllib.parse.urlencode(
                await self._ws_auth_params()
            )
            try:
                async with websockets.connect(url) as ws:
                    retries = 0
                    backoff = initial_backoff
                    remint_done_this_cycle = False
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            msg = {"raw": raw}
                        await on_message(msg)
            except (ConnectionClosedError, OSError, InvalidHandshake) as e:
                # N5 review R6: ws_session() (N4-5) now closes with 1008 *before*
                # accept() on an unauthorized/unknown session, which Starlette
                # surfaces as a rejected handshake — websockets raises that as
                # InvalidStatus (a subclass of InvalidHandshake), not
                # ConnectionClosedError/OSError. Retrying on a rejection that will
                # never succeed just burns the backoff budget, but that's the
                # existing max_retries contract for every other failure here too;
                # what mattered was reaching the documented RuntimeError instead of
                # propagating raw.
                #
                # N8 review R5: a rejected handshake specifically (not an ordinary
                # disconnect — ConnectionClosedError/OSError say nothing about the
                # credential) invalidates the cached bearer token before retrying,
                # mirroring _bearer_request's existing 401/403 handling. N7-5 made
                # the connect URL rebuild every attempt, but _bearer_token_value()
                # still serves the cached token until its own local expiry — that
                # fixes a token that's merely gone stale locally, not one the relay
                # actively rejects (revoked, wrong relay_id, secret rotated), which
                # was replayed byte-identical on every attempt, burning the whole
                # backoff budget on one dead credential.
                #
                # N9 review O2: re-minting on *every* rejection within the same
                # cycle burns a full challenge+sign+session round trip per attempt
                # even though only the first invalidation in a cycle can possibly
                # help — ws_session() closes 1008 for an unauthorized session AND
                # an unknown one (N5-7's own comment), so a listener pointed at a
                # session it simply isn't in was re-minting up to max_retries
                # times for a rejection no fresh token will ever fix. Throttled to
                # once per cycle (reset alongside retries/backoff on a successful
                # connect). _invalidate_bearer_and_remint also restores the old
                # token if the re-mint itself fails, so a transient auth-service
                # outage doesn't leave every later attempt sending no credential
                # at all — see that method's docstring.
                if isinstance(e, InvalidHandshake) and not remint_done_this_cycle:
                    await self._invalidate_bearer_and_remint()
                    remint_done_this_cycle = True
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
        """Listen to the per-agent WS channel. Reconnects with backoff.

        See _ws_auth_params(): sends both `agent_id` (satisfies the relay's
        localhost-trust tier for /ws/agent/{id} — see relay_server.py's
        _ws_agent_id() — covers the co-located deployment this SDK targets) and
        `token` (a bearer JWT via the Ed25519 dashboard-challenge flow, which works
        on any config — N6 review R4: `agent_id` alone did nothing on the shipped
        default of EMPORIA_TRUST_LOCAL_HEADER_AUTH=0). N7 review R5: rebuilt fresh
        on every reconnect attempt rather than once before the loop, so a token
        that expired between the original connect and a later reconnect gets
        refreshed instead of replayed dead — see listen_session's docstring."""
        retries = 0
        backoff = initial_backoff
        remint_done_this_cycle = False
        while retries <= max_retries:
            url = _ws_url(self.relay_url, f"/ws/agent/{self.agent_id}") + "?" + urllib.parse.urlencode(
                await self._ws_auth_params()
            )
            try:
                async with websockets.connect(url) as ws:
                    retries = 0
                    backoff = initial_backoff
                    remint_done_this_cycle = False
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            msg = {"raw": raw}
                        await on_message(msg)
            except (ConnectionClosedError, OSError, InvalidHandshake) as e:
                # N8 review R5: see listen_session's docstring — a rejected
                # handshake invalidates the cached bearer token before retrying,
                # rather than replaying a relay-rejected credential unchanged on
                # every attempt. N9 review O2: throttled to once per cycle and
                # guarded against a failed re-mint dropping the credential
                # entirely — see listen_session's docstring and
                # _invalidate_bearer_and_remint.
                if isinstance(e, InvalidHandshake) and not remint_done_this_cycle:
                    await self._invalidate_bearer_and_remint()
                    remint_done_this_cycle = True
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
        """Async generator yielding session events (no reconnect — single pass).

        N5 review R6: had no exception handling at all — a rejected handshake
        (unauthorized/unknown session_id — see listen_session's docstring) or a
        disconnect propagated as a raw low-level exception with no SDK-level
        context. Wrapped into a RuntimeError, matching the other two WS methods'
        contract, minus the retry (this method is intentionally single-pass). See
        _ws_auth_params() for the credential sent (N6 review R4).

        Unlike listen_session/listen_agent (N7 review R5), the connect URL is
        built once, not per reconnect attempt — correctly so, since this method
        makes exactly one connection and never retries; there is no later attempt
        for a cached token to go stale before.
        """
        url = _ws_url(self.relay_url, f"/ws/{session_id}") + "?" + urllib.parse.urlencode(
            await self._ws_auth_params()
        )
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        yield {"raw": raw}
        except (ConnectionClosedError, OSError, InvalidHandshake) as e:
            raise RuntimeError(f"stream_session_events: connection failed: {e}") from e
