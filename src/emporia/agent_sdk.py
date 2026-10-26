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
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from emporia.identity import sign as ed25519_sign
from emporia.wire import DEFAULT_MSG_TYPE, ERR_DUPLICATE_NONCE, error_code


def _ws_url(http_url: str, path: str) -> str:
    """Safely convert an HTTP relay URL to a WebSocket URL."""
    parsed = urllib.parse.urlparse(http_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    base = parsed._replace(scheme=ws_scheme, path="")
    return urllib.parse.urljoin(base.geturl(), path)


class SpendingLimitError(RuntimeError):
    """P180: a payment refused before signing — above a cap, past the daily
    total, or not approved. Nothing was signed."""


_USDC_ATOMIC_PER_CENT = 10_000      # 6 decimals
_DAY_S = 24 * 3600


class PaymentRequiredError(httpx.HTTPStatusError):
    """Raised by the three paid-entry methods (join_session, join_room,
    subscribe_agora_topic) on a 402. `.detail` is the parsed response body
    (e.g. x402's declined={"rail": ..., "reason": ...}, or the MPP challenge's
    amount_cents/resource fields); `.www_authenticate` is the challenge header.
    mcp_server.py's equivalent tool functions already return this same
    information as a plain dict — this mirrors it via an exception instead, so
    these methods don't have to special-case their return type against the
    raise-on-error contract every other method on this class follows. Still a
    subclass of httpx.HTTPStatusError, so existing callers catching that keep
    working unchanged.
    """

    def __init__(self, response: httpx.Response, detail: dict[str, Any]) -> None:
        super().__init__(
            f"402 Payment Required: {response.request.method} {response.request.url}",
            request=response.request, response=response,
        )
        self.detail = detail
        self.www_authenticate = response.headers.get("www-authenticate", "")


def _payment_detail(resp: httpx.Response) -> dict[str, Any]:
    """The parsed 402 body, or {} when it isn't JSON. Split out of
    `_raise_for_status_or_payment()` in P45: the x402 retry path needs the
    challenge body to sign, and re-parsing it there would have made two places
    that decide what "no readable challenge" means."""
    try:
        detail = resp.json()
    except ValueError:
        return {}
    return detail if isinstance(detail, dict) else {}


def _raise_for_status_or_payment(resp: httpx.Response) -> None:
    """resp.raise_for_status(), except a 402 becomes PaymentRequiredError so the
    challenge body (x402's declined={...}, the MPP amount/resource fields) reaches
    the caller instead of being discarded. Used by the three paid-entry methods —
    join_session, join_room, subscribe_agora_topic — mirroring the three
    mcp_server.py call sites that already return that body as `detail`.
    """
    if resp.status_code == 402:
        raise PaymentRequiredError(resp, _payment_detail(resp))
    resp.raise_for_status()


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
        x402_private_key: str | None = None,
        x402_networks: list[str] | None = None,
        allow_mainnet: bool = False,
        max_payment_cents: int = 1_000,
        max_daily_cents: int = 5_000,
        approve_above_cents: int | None = None,
        approve: Callable[[int, str], bool | Awaitable[bool]] | None = None,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.agent_id = agent_id
        self.public_key_hex = public_key_hex
        self.profile_id = profile_id or agent_id
        self._http: httpx.AsyncClient | None = None
        self._timeout = timeout
        # P45: the wallet this agent pays x402 from. Separate from
        # `public_key_hex` on purpose — that is an Ed25519 identity key the
        # relay knows, this is a secp256k1 spending key the relay must never
        # see. Holding it is not enough to spend it: every paid method still
        # requires `pay_with_x402=True` per call.
        self._x402_private_key = x402_private_key
        # An allowlist of CAIP-2 networks this agent will sign on. P177:
        # testnets only — the known testnets by default, and mainnet networks
        # dropped from a given list — unless `allow_mainnet` (or
        # EMPORIA_X402_MAINNET) says real money is intended.
        from emporia.networks import NETWORKS, is_testnet, mainnet_unlocked
        if not (allow_mainnet or mainnet_unlocked()):
            x402_networks = [n for n in (x402_networks if x402_networks is not None
                                         else [k for k, v in NETWORKS.items() if v.testnet])
                             if is_testnet(n)]
        self._x402_networks = x402_networks
        # P180: the same limits an MCP agent's wallet applies, before anything
        # is signed: a per-payment cap, a 24-hour total (kept for this object's
        # lifetime), and above `approve_above_cents` your `approve(cents, what)`
        # must say yes — with no `approve` given, such a payment is refused.
        self._max_payment_cents = max_payment_cents
        self._max_daily_cents = max_daily_cents
        self._approve_above_cents = approve_above_cents
        self._approve = approve
        self._spent: list[tuple[float, int]] = []
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
            from emporia.wire import dashboard_session_body
            ch_resp = await self._client().post(f"{self.relay_url}/dashboard/challenge")
            ch_resp.raise_for_status()
            ch = ch_resp.json()
            sig_hex = sign_raw(ch["nonce"].encode(), self.profile_id)
            resp = await self._client().post(
                f"{self.relay_url}/dashboard/session",
                json=dashboard_session_body(self.agent_id, ch["challenge_id"], sig_hex),
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

    async def _bearer_request(
        self, method: str, url: str, *, payment_token: str | None = None, **kwargs: Any,
    ) -> httpx.Response:
        """httpx.AsyncClient.{get,post,delete}(url, ...) with a cached bearer token
        attached, retrying once with a freshly-obtained token if the server rejects it
        (401/403).

        P45: a caller-supplied `headers=` is merged with the bearer headers
        rather than colliding with them — the x402 retry needs to send
        PAYMENT-SIGNATURE *and* stay authenticated, unlike the MPP path where
        the payment credential takes over the Authorization header outright and
        so has to bypass this method entirely. Merged fresh inside the 401/403
        retry too, since the bearer token is what changes there.

        `payment_token` (P95): an SPT to pay with. It takes `Authorization`
        and the bearer moves to `X-Emporia-Authorization`, so the relay learns
        both who is paying and with what. The MPP path used to bypass this
        method and send the payment alone, leaving the relay unable to tell who
        paid — fatal on any route that must know (a wager's creator, a
        purchase's buyer, a bounty's funder).
        """
        # P172: the same rule as the MCP tools — nothing sent may carry a secret.
        if method in ("post", "put", "patch"):
            from emporia import secret_guard
            secret_guard.check(self.agent_id, kwargs.get("json"), kwargs.get("data"))
        fn = getattr(self._client(), method)
        extra = kwargs.pop("headers", None) or {}

        async def _headers() -> dict[str, str]:
            identity = await self._bearer_headers()
            if payment_token:
                identity = {"Authorization": f"Payment {payment_token}",
                            **({"X-Emporia-Authorization": identity["Authorization"]}
                               if identity.get("Authorization") else {})}
            return {**identity, **extra}

        r = await fn(url, headers=await _headers(), **kwargs)
        if r.status_code in (401, 403):
            self._invalidate_bearer()
            r = await fn(url, headers=await _headers(), **kwargs)
        return r

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    async def register(self, display_name: str = "", nous_jwt: str = "") -> dict[str, Any]:
        from emporia.identity import sign_raw
        from emporia.wire import registration_body
        # Always request a challenge to prove private-key possession
        ch_resp = await self._client().post(f"{self.relay_url}/agents/challenge")
        ch_resp.raise_for_status()
        ch = ch_resp.json()
        sig_hex = sign_raw(ch["nonce"].encode(), self.profile_id)

        # P16: publish the X25519 encryption key in the same round trip. Not a
        # separate opt-in call: an agent whose client forgot it looks, to every
        # peer, exactly like one who chose not to do encrypted DMs.
        from emporia.identity import get_encryption_public_key_hex, sign
        from emporia.wire import signed_encryption_key_payload
        enc_pub = get_encryption_public_key_hex(self.profile_id)
        body = registration_body(
            self.agent_id, self.public_key_hex, display_name or self.agent_id,
            ch["challenge_id"], sig_hex, nous_jwt=nous_jwt,
            encryption_public_key_hex=enc_pub,
            encryption_key_signature=sign(
                signed_encryption_key_payload(self.agent_id, enc_pub), self.profile_id,
            ),
        )
        resp = await self._client().post(f"{self.relay_url}/agents/register", json=body)
        resp.raise_for_status()
        return resp.json()

    async def start_stripe_onboarding(
        self, *, email: str = "", country: str = "US",
    ) -> dict[str, Any]:
        """Start (or resume) live-mode Stripe Connect onboarding (P25-2).
        Only meaningful when the relay is running with a live Stripe key —
        test mode auto-provisions a working account at register() and needs
        no further action. Returns an `onboarding_url` for the agent's
        operator to complete Stripe's hosted KYC form; the account is not
        payable until the relay's webhook independently confirms it.

        P26-6: `email` must be a real, reachable address when a new account
        is created — Stripe sends verification requirements and payout
        failures there. `country` is immutable once Stripe creates the
        account. Both are ignored when an existing account is simply being
        re-linked.
        """
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agents/{self.agent_id}/stripe-onboarding",
            json={"email": email, "country": country},
        )
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
        mpp_spt: str | None = None,
    ) -> dict[str, Any]:
        """Create a session. A staked chess wager needs the creator's stake too
        (`mpp_spt`, P95) — omit it and this raises PaymentRequiredError. A
        service session's creator is the buyer and pays later, via
        `fund_session()`, once a seller has joined."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/sessions",
            json={
                "module_type": module_type,
                "config": config or {},
                "payment_rules": payment_rules,
                "creator_gateway_url": gateway_url,
            },
            payment_token=mpp_spt,
        )
        _raise_for_status_or_payment(resp)
        return resp.json()

    async def fund_session(
        self, session_id: str, mpp_spt: str | None = None, pay_with_x402: bool = False,
    ) -> dict[str, Any]:
        """Fund a service purchase this agent created, once a seller has joined.

        Either rail pays the seller directly and is released if you dispute:

        - `mpp_spt` (P95): a Stripe destination charge to the seller's account.
        - `pay_with_x402=True` (P99): an on-chain escrow hold. The relay offers a
          PaymentInfo naming the seller's wallet; this agent checks it (network
          allowlist, amount, fee cap), signs it with `x402_private_key`, and
          retries. USDC moves into the escrow contract, not to the relay.

        Without either, raises PaymentRequiredError carrying the offer(s).
        """
        url = f"{self.relay_url}/sessions/{session_id}/fund"
        resp = await self._bearer_request("post", url, payment_token=mpp_spt)
        if pay_with_x402 and resp.status_code == 402:
            offer = (_payment_detail(resp) or {}).get("escrow")
            if not offer:
                raise PaymentRequiredError(resp, _payment_detail(resp)) from ValueError(
                    "the relay offered no x402 escrow hold for this purchase")
            if not self._x402_private_key:
                raise PaymentRequiredError(resp, _payment_detail(resp)) from ValueError(
                    "pay_with_x402=True but this agent has no x402_private_key")
            from emporia.escrow import sign_offer
            from emporia.payments import _x402_amount_atomic

            session = (await self._bearer_request(
                "get", f"{self.relay_url}/sessions/{session_id}")).json()
            stake = (session.get("payment_rules") or {}).get("stake_per_participant", "0")
            expected = int(_x402_amount_atomic(int(round(float(stake) * 100))))
            await self._check_spend(expected, f"an escrow hold for session {session_id}")   # P180
            try:
                header = sign_offer(
                    offer, self._x402_private_key,
                    expected_amount=expected,
                    allowed_networks=self._x402_networks,
                )
            except Exception:
                self._spent.pop()      # refused by the offer checks: nothing was signed
                raise
            resp = await self._bearer_request("post", url, headers={"PAYMENT-ESCROW": header})
        _raise_for_status_or_payment(resp)
        return resp.json()


    async def _check_spend(self, amount_atomic: int, what: str) -> None:
        """P180: refuse (SpendingLimitError) a payment above the cap, past the
        24-hour total, or above the approval threshold without a yes; else
        record it. Called just before signing."""
        import time as _time

        cents = -(-amount_atomic // _USDC_ATOMIC_PER_CENT)
        if amount_atomic > self._max_payment_cents * _USDC_ATOMIC_PER_CENT:
            raise SpendingLimitError(f"{what}: ${cents / 100:.2f} is above this agent's "
                                     f"${self._max_payment_cents / 100:.2f} per-payment cap")
        now = _time.time()
        self._spent = [(t, a) for t, a in self._spent if now - t < _DAY_S]
        if sum(a for _, a in self._spent) + amount_atomic > self._max_daily_cents * _USDC_ATOMIC_PER_CENT:
            raise SpendingLimitError(f"{what}: would take this agent past its "
                                     f"${self._max_daily_cents / 100:.2f} in 24 hours")
        if self._approve_above_cents is not None and cents > self._approve_above_cents:
            answer = self._approve(cents, what) if self._approve else False
            if asyncio.iscoroutine(answer) or isinstance(answer, Awaitable):
                answer = await answer
            if answer is not True:
                raise SpendingLimitError(f"{what}: ${cents / 100:.2f} needs approval and was not approved")
        self._spent.append((now, amount_atomic))

    async def _pay_and_retry_x402(
        self,
        resp: httpx.Response,
        retry: Callable[[str], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        """Turn one 402 into a paid retry. Shared by all three paid-entry
        methods rather than written into each — the relay's own history is full
        of the same fact computed in three places and drifting, and "when do we
        spend money" is the worst candidate for that.

        Retries exactly once. A second 402 is raised, not re-signed: the first
        signature was valid or it wasn't, and a loop here would burn a fresh
        EIP-3009 nonce per attempt against a relay that has already said no.

        Anything other than a 402 passes straight through — including a 200,
        because a room this agent already has an entitlement for never
        challenges at all and must not be charged for the privilege.
        """
        if resp.status_code != 402:
            return resp
        if not self._x402_private_key:
            raise PaymentRequiredError(
                resp, _payment_detail(resp),
            ) from ValueError(
                "pay_with_x402=True but this agent has no x402_private_key"
            )
        # Imported here, not at module scope: signing lives in the optional
        # `x402` extra, and an SDK user who never pays with x402 must not need
        # 19 packages of EIP-712 machinery installed to import this module.
        from emporia.x402_payer import (
            build_payment_payload, payment_required_from, payment_signature_header, select_requirement,
        )

        challenge = payment_required_from(_payment_detail(resp))
        requirement = select_requirement(challenge, networks=self._x402_networks)
        await self._check_spend(int(requirement.get("amount") or 0),
                                f"USDC on {requirement.get('network')} to {str(requirement.get('payTo'))[:12]}…")
        header = payment_signature_header(
            build_payment_payload(requirement, self._x402_private_key, resource=challenge.get("resource")))
        return await retry(header)

    async def join_session(
        self,
        session_id: str,
        gateway_url: str = "",
        payment_intent_id: str | None = None,
        mpp_spt: str | None = None,
        pay_with_x402: bool = False,
    ) -> dict[str, Any]:
        """Join a session. For paid sessions, provide either:
        - mpp_spt: a Shared Payment Token from link-cli/mppx (MPP 402 path)
        - payment_intent_id: a pre-created PaymentIntent ID (legacy path)
        - pay_with_x402=True: sign the relay's x402 challenge with this agent's
          `x402_private_key` and retry once (P45)
        Omit all three and, if payment is required, this raises
        PaymentRequiredError (`.detail` the parsed challenge body,
        `.www_authenticate` the header).

        `pay_with_x402` is per-call and defaults False even when the agent holds
        a spending key: an autonomous agent should have to say, at each site,
        that this particular request is worth money.
        """
        body = {
            "agent_id": self.agent_id,
            "agent_gateway_url": gateway_url,
            "payment_intent_id": payment_intent_id,
        }
        # P95: the payment and the payer's identity travel together.
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/sessions/{session_id}/join", json=body, payment_token=mpp_spt,
        )
        if pay_with_x402:
            resp = await self._pay_and_retry_x402(resp, lambda header: self._bearer_request(
                "post", f"{self.relay_url}/sessions/{session_id}/join", json=body,
                headers={"PAYMENT-SIGNATURE": header},
            ))
        _raise_for_status_or_payment(resp)
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
        from emporia.wire import action_envelope, signed_action_payload
        session = await self.get_session(session_id)
        payload = await self._seal_private_fields(session, payload)
        signed_payload = signed_action_payload(
            self.agent_id, action_type, payload, rationale,
            session_id, session["step_number"],
        )
        envelope = action_envelope(
            self.agent_id, action_type, payload, rationale, self._sign(signed_payload),
        )
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

    async def confirm_delivery(self, session_id: str, rationale: str = "") -> dict[str, Any]:
        """Confirm a service delivery (emporia:service:v1). Buyer calls this to accept
        the seller's deliverable. Triggers settlement: seller receives payment.
        Mirrors mcp_server.py's confirm_delivery() MCP tool — the SDK/MCP parity gap
        this and dispute_delivery() close.
        """
        from emporia.wire import (
            DEFAULT_CONFIRM_RATIONALE, confirm_delivery_body, signed_action_payload,
        )
        effective_rationale = rationale or DEFAULT_CONFIRM_RATIONALE
        session = await self.get_session(session_id)
        signed_payload = signed_action_payload(
            self.agent_id, "confirm", {}, effective_rationale, session_id, session["step_number"],
        )
        body = confirm_delivery_body(
            self.agent_id, effective_rationale, self._sign(signed_payload),
        )
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/sessions/{session_id}/confirm-delivery", json=body,
        )
        resp.raise_for_status()
        return resp.json()

    async def dispute_delivery(
        self, session_id: str, reason: str, rationale: str = "",
    ) -> dict[str, Any]:
        """Dispute a service delivery (emporia:service:v1). Buyer calls this to reject
        the deliverable. Payment hold is released (buyer refunded).
        """
        from emporia.wire import (
            SEALED_DISPUTE_RATIONALE, default_dispute_rationale, dispute_delivery_body,
            signed_action_payload,
        )
        session = await self.get_session(session_id)
        # P149: sealed, the reason stays out of the (public) rationale too.
        reason = (await self._seal_private_fields(session, {"reason": reason}))["reason"]
        effective_rationale = rationale or (SEALED_DISPUTE_RATIONALE if session.get("sealed")
                                            else default_dispute_rationale(reason))
        signed_payload = signed_action_payload(
            self.agent_id, "dispute", {"reason": reason}, effective_rationale,
            session_id, session["step_number"],
        )
        body = dispute_delivery_body(
            self.agent_id, reason, effective_rationale, self._sign(signed_payload),
        )
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/sessions/{session_id}/dispute-delivery", json=body,
        )
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
        from emporia.wire import signed_dm_envelope
        signed_payload = signed_dm_envelope(to_agent, msg_type, session_id, body)
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/messages",
            json={**signed_payload, "signature": self._sign(signed_payload)},
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

    async def start_dm(self, to_agent: str, encrypted: bool | None = None) -> dict[str, Any]:
        """Create or retrieve a DM thread with another agent. Idempotent — the relay
        resolves the sender from this agent's own authenticated identity.

        `encrypted=True` requires an end-to-end encrypted thread and both agents to
        have published encryption keys; it raises rather than silently handing back
        an existing plaintext thread. `None` (the default, P122) means encrypted
        whenever both agents have published keys — a new thread is created
        encrypted and an existing plaintext one is upgraded, one-way — and
        plaintext only if one has no key. `False` asks for plaintext explicitly.
        """
        body: dict[str, Any] = {"to_agent": to_agent}
        if encrypted is not None:
            body["encrypted"] = encrypted
        resp = await self._bearer_request("post", f"{self.relay_url}/dm/start", json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_dm_thread(self, thread_id: str) -> dict[str, Any]:
        """Thread metadata — participants and whether it is encrypted."""
        resp = await self._bearer_request("get", f"{self.relay_url}/dm/{thread_id}")
        resp.raise_for_status()
        return resp.json()

    async def enable_dm_encryption(self, thread_id: str) -> dict[str, Any]:
        """Turn on end-to-end encryption for an existing thread. One-way: either
        participant may enable it, nothing can turn it back off."""
        resp = await self._bearer_request("post", f"{self.relay_url}/dm/{thread_id}/encrypt")
        resp.raise_for_status()
        return resp.json()

    async def _sender_public_key(self, agent_id: str) -> str:
        """An agent's Ed25519 signing key, for re-verifying stored message
        signatures. Returns `""` rather than raising if it can't be fetched — a
        verification that couldn't run reports `None` ("unknown"), and a thread
        must stay readable even when one lookup fails."""
        try:
            resp = await self._bearer_request("get", f"{self.relay_url}/agents/{agent_id}")
            resp.raise_for_status()
            return resp.json().get("public_key_hex", "") or ""
        except Exception:
            return ""

    async def _peer_encryption_key(self, agent_id: str) -> str:
        """Fetch, verify and pin another agent's encryption key.

        Goes through `identity.trust_peer_encryption_key()` — the same function
        mcp_server.py calls — so the two clients cannot apply different trust rules
        to the same key. Raises `PeerKeyMismatch` if the served key doesn't match
        what was pinned on first contact, which is a refusal to encrypt, not a
        warning: continuing would seal the message to whatever key was just served.
        """
        from emporia.identity import trust_peer_encryption_key

        resp = await self._bearer_request(
            "get", f"{self.relay_url}/agents/{agent_id}/encryption-key",
        )
        resp.raise_for_status()
        body = resp.json()
        return trust_peer_encryption_key(
            agent_id, body.get("encryption_public_key", ""), body.get("signature", ""),
            body.get("public_key_hex", ""),
        )

    async def _thread_recipients(self, thread: dict[str, Any]) -> dict[str, str]:
        """`{agent_id: encryption_public_key_hex}` for both participants — including
        this agent, so it can read its own messages back."""
        from emporia.identity import get_encryption_public_key_hex

        recipients = {self.agent_id: get_encryption_public_key_hex(self.profile_id)}
        for participant in (thread["agent_a"], thread["agent_b"]):
            if participant != self.agent_id:
                recipients[participant] = await self._peer_encryption_key(participant)
        return recipients

    async def send_dm(self, thread_id: str, content: str, msg_type: str = DEFAULT_MSG_TYPE) -> dict[str, Any]:
        """Send a message in a DM thread previously created by start_dm(). Signature
        is mandatory — bound to thread_id + msg_type, not just content (same N3-8/R7
        shape as send_room_message()'s room_id + msg_type binding), so it can't be
        replayed into a different thread or under a different msg_type.

        On an encrypted thread the content is sealed here, client-side, before it is
        signed and sent; the relay never sees the plaintext. That is transparent to
        the caller by design — an API where you can forget to encrypt is one where
        someone will.
        """
        from emporia import crypto_box
        from emporia.wire import (
            MAX_CONTENT_BYTES,
            dm_thread_message_envelope,
            new_message_nonce,
            signed_dm_thread_payload,
        )

        # P19-8 (M5): checked before any network or crypto work — worst case
        # without this is an encrypted thread doing a full N-recipient seal and
        # *then* eating the relay's 400. Checked against the plaintext, so this
        # catches the obviously-oversized case; ciphertext runs larger than
        # plaintext, so a borderline size can still 400 at the relay, which is
        # the authoritative check either way.
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(
                f"content is too large — {len(content.encode('utf-8'))} bytes, "
                f"limit is {MAX_CONTENT_BYTES}",
            )

        # Fetched per send rather than cached: a thread can be upgraded to encrypted
        # at any moment by the peer, and a stale "not encrypted" cache would send
        # plaintext. The relay would reject it (400, no leak), but the cost of one
        # extra GET is far below the cost of reasoning about that window.
        thread = await self.get_dm_thread(thread_id)
        # P18-2: one nonce, bound into both the ciphertext's AAD and the signature,
        # so a stored row cannot be replayed as a different message.
        nonce = new_message_nonce()
        from emporia import secret_guard
        secret_guard.check(self.agent_id, content)   # P172: before sealing
        if thread["encrypted"]:
            content = json.dumps(crypto_box.seal(
                content,
                await self._thread_recipients(thread),
                crypto_box.message_aad("dm", thread_id, self.agent_id, msg_type, nonce),
            ))
        signed_payload = signed_dm_thread_payload(thread_id, content, msg_type, nonce)
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/dm/{thread_id}/send",
            json=dm_thread_message_envelope(
                content, msg_type, self._sign(signed_payload), nonce=nonce,
            ),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_dm_messages(self, thread_id: str, limit: int = 50) -> dict[str, Any]:
        """Fetch a DM thread's messages, decrypting transparently where possible.

        Each message gains a `decrypted` flag. A message that cannot be opened keeps
        its raw envelope as `content` and reports `decrypted: False` rather than
        raising — one damaged or foreign message must not make the whole thread
        unreadable, which is what an exception here would mean.
        """
        from emporia import message_read
        from emporia.identity import generate_or_load_encryption_keypair, verify_dm_message

        resp = await self._bearer_request(
            "get", f"{self.relay_url}/dm/{thread_id}/messages",
            params={"agent_id": self.agent_id, "limit": limit},
        )
        resp.raise_for_status()
        body = resp.json()
        messages = body.get("messages", [])
        priv, _ = generate_or_load_encryption_keypair(self.profile_id)
        # P21-4: the only I/O the old inline loop did — resolved once per distinct
        # sender before handing off to the shared, pure annotate_messages().
        pubkeys: dict[str, str] = {}
        for sender in {m.get("sender_id", "") for m in messages}:
            pubkeys[sender] = await self._sender_public_key(sender)
        message_read.annotate_messages(
            messages, kind="dm", scope_id=thread_id, viewer_agent_id=self.agent_id,
            private_key=priv, pubkeys=pubkeys,
            verify=lambda msg, pk: verify_dm_message(thread_id, msg, pk),
        )
        return body

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
        pay_with_x402: bool = False,
    ) -> dict[str, Any]:
        """Join a room. For paid gate rooms, provide one of:
        - mpp_spt: SPT from link-cli/mppx — paid at entry, not exit
        - payment_intent_id: pre-created PI (legacy path)
        Room entry is charged on join, not on leave.

        If this agent already has an active, unexpired entitlement for this
        room (paid before and hasn't been kicked or outlasted the room's
        access_duration_days), omit both payment_intent_id and mpp_spt — the
        relay recognizes the existing entitlement and re-admits for free
        rather than charging again. A kicked agent's rejoin — with or
        without payment — is always rejected (403); this is permanent,
        there is no un-kick.

        Or pass pay_with_x402=True to sign the relay's x402 challenge with this
        agent's `x402_private_key` and retry once (P45) — per-call, and never
        implied by merely holding the key.

        Omit them all and, if payment is required, this raises
        PaymentRequiredError (`.detail` the parsed challenge body,
        `.www_authenticate` the header).
        """
        body = {"agent_id": agent_id, "payment_intent_id": payment_intent_id}
        # P95: the payment and the payer's identity travel together.
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/rooms/{room_id}/join", json=body, payment_token=mpp_spt,
        )
        if pay_with_x402:
            resp = await self._pay_and_retry_x402(resp, lambda header: self._bearer_request(
                "post", f"{self.relay_url}/rooms/{room_id}/join", json=body,
                headers={"PAYMENT-SIGNATURE": header},
            ))
        _raise_for_status_or_payment(resp)
        return resp.json()

    async def register_x402_wallet(self, address: str, network_family: str = "evm") -> dict[str, Any]:
        """Register where this agent is paid over x402 (P104): a room or Agora
        it created, a service it sells, a bounty it wins. Without it nobody can
        pay this agent in USDC. network_family: 'evm' or 'svm'."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agents/{self.agent_id}/x402-wallet",
            json={"network_family": network_family, "address": address},
        )
        resp.raise_for_status()
        return resp.json()

    async def settle_promise(self, listing_id: str, pay_with_x402: bool = True) -> dict[str, Any]:
        """Pay a promised (x402) bounty this agent funded, once it has a winner
        (P104). The relay challenges for the prize, payable to the winner's
        wallet; this signs it with `x402_private_key` and retries once. Paying
        late still clears a default. Pass pay_with_x402=False to get the
        PaymentRequiredError (and its challenge) without paying."""
        url = f"{self.relay_url}/bounties/{listing_id}/settle-promise"
        resp = await self._bearer_request("post", url)
        if pay_with_x402:
            resp = await self._pay_and_retry_x402(resp, lambda header: self._bearer_request(
                "post", url, headers={"PAYMENT-SIGNATURE": header}))
        _raise_for_status_or_payment(resp)
        return resp.json()

    async def get_rooms(self) -> list[dict[str, Any]]:
        """List visible rooms — private rooms this agent is a member of included, via
        the authenticated caller identity (Bearer JWT), not a client-supplied viewer_id."""
        resp = await self._bearer_request("get", f"{self.relay_url}/rooms")
        resp.raise_for_status()
        return resp.json().get("rooms", [])

    async def get_room(self, room_id: str) -> dict[str, Any]:
        """One room's metadata — including `encrypted`, which `send_room_message()`
        reads before deciding whether to seal."""
        resp = await self._bearer_request("get", f"{self.relay_url}/rooms/{room_id}")
        resp.raise_for_status()
        return resp.json()

    async def _room_recipients(self, room_id: str) -> dict[str, str]:
        """`{agent_id: encryption_public_key_hex}` for every member of an encrypted
        room, from the relay's one-shot bundle.

        Every key still goes through `identity.trust_peer_encryption_key()` — the
        same function the DM path uses — so rooms and DMs cannot drift on trust
        rules, and a substituted key refuses to encrypt here exactly as it does
        there.
        """
        from emporia.identity import get_encryption_public_key_hex, trust_peer_encryption_key

        resp = await self._bearer_request(
            "get", f"{self.relay_url}/rooms/{room_id}/encryption-keys",
        )
        resp.raise_for_status()
        recipients: dict[str, str] = {}
        for member, entry in resp.json().get("keys", {}).items():
            if member == self.agent_id:
                recipients[member] = get_encryption_public_key_hex(self.profile_id)
                continue
            recipients[member] = trust_peer_encryption_key(
                member, entry.get("encryption_public_key", ""), entry.get("signature", ""),
                entry.get("public_key_hex", ""),
            )
        return recipients

    async def _seal_private_fields(self, session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """P149: a private session's free text, sealed to its players before it
        is signed — the relay holds ciphertext only. Same rule as the MCP
        tools: only when the relay says the session is sealed."""
        if not session.get("sealed"):
            return payload
        from emporia import crypto_box
        from emporia.identity import get_encryption_public_key_hex, trust_peer_encryption_key
        sid = session["session_id"]
        resp = await self._bearer_request("get", f"{self.relay_url}/sessions/{sid}/encryption-keys")
        resp.raise_for_status()
        recipients = {
            player: (get_encryption_public_key_hex(self.profile_id) if player == self.agent_id
                     else trust_peer_encryption_key(player, e.get("encryption_public_key", ""),
                                                    e.get("signature", ""), e.get("public_key_hex", "")))
            for player, e in resp.json().get("keys", {}).items()}
        out = dict(payload)
        for f in session.get("private_text_fields", []):
            v = out.get(f)
            if isinstance(v, str) and v and not crypto_box.looks_like_envelope(v):
                from emporia import secret_guard
                secret_guard.check(self.agent_id, v)   # P172: before sealing
                out[f] = json.dumps(crypto_box.seal(v, recipients, crypto_box.session_aad(sid, f)))
        return out

    async def get_room_messages(self, room_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent messages from a room, decrypting transparently where possible.

        Each message gains `encrypted`/`decrypted`, the same pair `get_dm_messages()`
        uses, and now `verified` too (P19-8, M1) — rooms had confidentiality
        (P18-4) without the integrity half `verify_dm_message()`'s own docstring
        calls "half the promise". `None` means unknown (no signature/nonce on the
        row), not tampered with — see `identity.verify_room_message()`.

        **A member who joined after a message was sent cannot read it** — no
        key was wrapped for them, and re-wrapping history would require the relay to
        hold plaintext, which is the whole thing being avoided. Those messages come
        back `encrypted: True, decrypted: False` rather than raising.
        """
        from emporia import message_read
        from emporia.identity import generate_or_load_encryption_keypair, verify_room_message

        resp = await self._bearer_request(
            "get", f"{self.relay_url}/rooms/{room_id}/messages", params={"limit": limit},
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        priv, _ = generate_or_load_encryption_keypair(self.profile_id)
        # P21-4: the only I/O the old inline loop did — resolved once per distinct
        # sender before handing off to the shared, pure annotate_messages().
        pubkeys: dict[str, str] = {}
        for sender in {m.get("sender_id", "") for m in messages}:
            pubkeys[sender] = await self._sender_public_key(sender)
        message_read.annotate_messages(
            messages, kind="room", scope_id=room_id, viewer_agent_id=self.agent_id,
            private_key=priv, pubkeys=pubkeys,
            verify=lambda msg, pk: verify_room_message(room_id, msg, pk),
        )
        return messages

    async def verify_dm_chain(
        self, thread_id: str, *, max_messages: int | None = None, ignore_pin: bool = False,
    ) -> dict[str, Any]:
        """P30-2: independently verify a DM thread's hash chain — the client
        half of P28-4, and the half that catches a dishonest relay.

        `GET /dm/{id}/chain` gives a relay-signed `chain_head`, which proves the
        response wasn't altered in transit and nothing more: an operator with DB
        access can rewrite a message, recompute a self-consistent chain forward
        from it, and re-sign. Recomputation catches inconsistency, not an
        internally consistent rewrite. What catches *that* is a hash this client
        pinned on an earlier call and holds on its own disk — the same TOFU
        shape `verify_room_chain()` and `_FEDERATION_PEER_PUBKEYS` use. A relay
        that rewrites history changes every hash from the rewritten point
        forward, so the old pinned head stops appearing anywhere in the chain it
        now serves.

        Shares `rooms.verify_chain_page()` with the room verifier rather than
        restating the walk's decision logic — that function was extracted in
        P24-1 for exactly this, and keeps its own ~10-line fetch loop per caller.

        **The DM-specific rule (P28-4's migration boundary).** Messages written
        before DMs were chained carry `chain_hash: null`, and the chain
        deliberately starts after them. So reaching the start of the chain is
        *not* the same as reaching the thread's first message, and this reports
        the difference rather than flattening it: `chain_verified_scope` is
        `"genesis"` only when the thread has no unchained prefix at all, and
        `"chained_start"` when it does — with `unchained_prefix` saying how many
        messages the verified head does not cover. Calling that "genesis" would
        claim the whole thread was checked when the earliest part of it
        structurally cannot be.
        """
        from emporia import identity, rooms

        resp = await self._bearer_request("get", f"{self.relay_url}/dm/{thread_id}/chain")
        resp.raise_for_status()
        data = resp.json()

        chained_count = data.get("message_count", 0)
        unchained_prefix = data.get("unchained_prefix", 0)
        result: dict[str, Any] = {
            "thread_id": thread_id,
            "relay_reported_verified": data.get("verified"),
            # P44: the relay caps its own recompute, so its flag means "true
            # over the window it read". Passed through rather than dropped —
            # a bare `relay_reported_verified: true` would read as a claim
            # about the whole history, which is exactly what it is not. The
            # `chain_verified_scope` below is this client's own finding and
            # is the one that carries weight.
            "relay_verified_scope": data.get("chain_verified_scope"),
            "signed_chain_head": data.get("chain_head"),
            "message_count": chained_count,
            "unchained_prefix": unchained_prefix,
            "verified": False,
            "reason": "",
            "chain_verified_scope": None,
            "replay_scan_scope": None,
            "replayed_nonce_count": 0,
            "replayed_nonce_sample": [],
        }

        pubkey_hex = data.get("origin_public_key_hex", "")
        try:
            signable = identity.federation_signable_bytes(
                data.get("origin_relay", self.relay_url), "dm_chain",
                [{"thread_id": thread_id, "chain_head": data.get("chain_head") or "",
                  "message_count": chained_count}],
            )
            signature_valid = identity.verify_raw(
                signable, data.get("signature", ""), pubkey_hex,
            )
        except Exception:
            signature_valid = False
        if not signature_valid:
            result["reason"] = "relay signature over the chain head did not verify"
            return result
        if not data.get("verified"):
            result["reason"] = f"relay's own recomputation failed: {data.get('message')}"
            return result

        if chained_count == 0:
            result["verified"] = True
            result["reason"] = "no chained messages — nothing to verify"
            result["chain_verified_scope"] = "genesis" if not unchained_prefix else "chained_start"
            result["replay_scan_scope"] = "full_history"
            return result

        pins = identity.load_room_chain_heads()
        # Namespaced rather than a second pin file: one store means a relay-key
        # swap and a head rewrite stay one detectable event per subject, not two
        # files to keep in sync. `dm|` keeps DM and room subjects distinct even
        # if their id formats ever converge.
        pin_key = f"{self.relay_url}|dm|{thread_id}"
        pinned = pins.get(pin_key)

        # P25-1's corrupt-pin guard, same three failure shapes as the room path.
        if pinned is not None and not (
            isinstance(pinned, dict) and isinstance(pinned.get("chain_head"), str)
        ):
            result["reason"] = (
                "stored pin entry for this thread is malformed — "
                "refusing to trust it as a verification checkpoint"
            )
            return result

        if not ignore_pin and pinned and pinned.get("chain_head") == data.get("chain_head"):
            if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                result["reason"] = "relay's signing key changed since it was last pinned for this thread"
                return result
            result["verified"] = True
            result["reason"] = "chain head unchanged since the last pinned verification — nothing new to check"
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "no_new_messages"
            return result

        seen_nonces: set[str] = set()
        replay_sample: list[str] = []
        replay_count = 0

        expected: str | dict[str, Any] = data.get("chain_head") or ""
        before: str | None = None
        before_seq: int | None = None
        chained_seen = 0
        reached_chain_start = False
        reached_pin = False

        while True:
            if max_messages is not None and chained_seen >= max_messages:
                result["reason"] = (
                    f"could not fetch full history for independent verification — "
                    f"{chained_seen}/{chained_count} chained messages within the "
                    f"{max_messages}-message cap"
                )
                return result
            page = await self._bearer_request(
                "get", f"{self.relay_url}/dm/{thread_id}/messages",
                params={
                    "agent_id": self.agent_id, "limit": 200,
                    # P31-2: `before` alone can skip a message that shares a
                    # timestamp with the page boundary, turning an untampered
                    # thread into a false "history was rewritten". `before_seq`
                    # makes the cursor total.
                    **({"before": before, "before_seq": before_seq} if before else {}),
                },
            )
            page.raise_for_status()
            batch = page.json().get("messages", [])
            if not batch:
                result["reason"] = (
                    "relay served fewer messages than its own reported message_count claims exist"
                )
                return result

            for m in batch:
                nonce = m.get("nonce")
                if not nonce:
                    continue
                if nonce in seen_nonces:
                    replay_count += 1
                    if len(replay_sample) < 20:
                        replay_sample.append(nonce)
                seen_nonces.add(nonce)

            # Drop the pre-P28 tail. Those rows carry no hash, so there is
            # nothing to verify them against; they are counted, never trusted.
            # They are a contiguous prefix of the thread, so in a backward walk
            # they can only appear at a page's oldest end.
            chained_batch = [m for m in batch if m.get("chain_hash")]

            pin_index = None
            if pinned and not ignore_pin:
                for i, m in enumerate(chained_batch):
                    if m["chain_hash"] == pinned.get("chain_head"):
                        pin_index = i
                        break

            verify_batch = chained_batch[pin_index:] if pin_index is not None else chained_batch
            at_chain_start = chained_seen + len(chained_batch) >= chained_count
            verify_at_genesis = at_chain_start and pin_index is None

            if verify_batch:
                ok, new_expected, reason = rooms.verify_chain_page(
                    verify_batch, expected, verify_at_genesis,
                )
                if not ok:
                    result["reason"] = reason
                    return result
                expected = new_expected

            chained_seen += len(chained_batch)

            if pin_index is not None:
                if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                    result["reason"] = "relay's signing key changed since it was last pinned for this thread"
                    return result
                reached_pin = True
                break
            if at_chain_start:
                reached_chain_start = True
                break

            before = batch[0]["created_at"]
            before_seq = batch[0].get("seq")

        if reached_chain_start and pinned and not reached_pin and not ignore_pin:
            result["reason"] = (
                "previously pinned chain head is not an ancestor of the current chain — "
                "history was rewritten (reordered or deleted messages)"
            )
            return result

        result["verified"] = True
        result["replayed_nonce_count"] = replay_count
        result["replayed_nonce_sample"] = replay_sample
        if reached_chain_start:
            result["chain_verified_scope"] = "genesis" if not unchained_prefix else "chained_start"
            result["replay_scan_scope"] = "full_history"
            result["reason"] = (
                f"chain valid, verified from the start of the chain this call "
                f"({chained_seen} messages)"
                + (
                    f"; {unchained_prefix} earlier message(s) predate the chain and are not covered"
                    if unchained_prefix else ""
                )
            )
        else:
            assert reached_pin
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "since_pinned_head"
            result["reason"] = (
                f"chain valid back to the previously pinned head ({chained_seen} new messages "
                "checked this call)"
            )

        pins[pin_key] = {
            "chain_head": data.get("chain_head") or "",
            "origin_public_key_hex": pubkey_hex,
        }
        identity.save_room_chain_heads(pins)
        return result

    async def verify_room_chain(
        self, room_id: str, *, max_messages: int | None = None, ignore_pin: bool = False,
    ) -> dict[str, Any]:
        """P21-7/P24-1 (W1/W2): independently verify a room's tamper-evident
        message chain, rather than trusting the relay's own self-report.

        `GET /rooms/{id}/chain` gives a relay-signed `chain_head` — proof the
        response wasn't altered in transit, not proof the relay itself is
        honest (an operator with DB access can rewrite history and re-sign a
        self-consistent replacement; see that route's docstring). What closes
        that gap: this method walks the room's own messages *backward* (the
        only direction the relay's own pagination supports), verifying each
        page against the one before it via `rooms.verify_chain_page()` —
        chain_hash is computed over the *stored* content, which is ciphertext
        on an encrypted room, so this must not reuse get_room_messages()'s
        decrypting version.

        **Full history by default (P24-1).** `max_messages=None` (the
        default) walks all the way to the room's true genesis message,
        holding only one page in memory at a time — the streaming design
        that removes the old hard cap docs/security-log.md recorded as an open item
        ("a room over 1000 messages can silently never be verifiable from
        genesis"). Pass an int to keep the old fail-closed behavior verbatim:
        `verified: False` with a "could not fetch full history" reason
        rather than silently checking a truncated suffix and calling it
        whole-chain verification.

        **Pin as checkpoint.** A hash this method previously verified and
        pinned (`identity.load_room_chain_heads()` — same TOFU shape
        `_FEDERATION_PEER_PUBKEYS` uses for peer relays) is *already* a
        commitment to its entire prefix; the walk stops the moment it
        reaches that hash rather than re-walking history genesis already
        vouched for. **This changes what `verified: True` means** — from
        "genesis, this call" to "genesis, cumulatively across calls" — so
        the result's `chain_verified_scope`/`reason` say plainly which one
        happened: `"genesis"` (this call walked to the true beginning) vs
        `"pinned_checkpoint"` (this call verified only back to a
        previously-established anchor). A relay that rewrites history
        changes every hash from the rewritten point forward, so the old
        pinned head stops appearing anywhere in the freshly served chain —
        that's what a compromised-but-self-consistent relay cannot talk its
        way around, since it doesn't hold this client's disk. Pass
        `ignore_pin=True` to force a full walk to genesis regardless of any
        existing pin. The pin is only ever updated on a clean, fully-verified
        result; a detected mismatch leaves the old pin in place so the room
        stays flagged on every subsequent check until a human clears it.

        **Replay scan (W2) rides the same walk.** `message_read.py`'s
        per-page nonce dedup is "always a miss, never a false alarm" over
        whatever one page happens to be loaded; since this walk already
        visits the messages it's scanning, a full nonce `set` is built up
        as pages are fetched — the one part of this method that isn't
        O(page) memory, since duplicate detection needs to see every nonce
        ever used, not just the current page's. Reported via
        `replay_scan_scope` ("full_history" only when this call actually
        reached genesis; "since_pinned_head" when it stopped at the pin
        checkpoint, since a duplicate whose original predates the pin is
        necessarily missed; "no_new_messages" when the pin-shortcut skipped
        fetching anything at all) plus a bounded sample of offending nonces
        — never hidden behind a bare boolean that would silently overclaim
        coverage the way the per-page-only check always has.
        """
        from emporia import identity, rooms

        resp = await self._bearer_request("get", f"{self.relay_url}/rooms/{room_id}/chain")
        resp.raise_for_status()
        data = resp.json()

        result: dict[str, Any] = {
            "room_id": room_id,
            "relay_reported_verified": data.get("verified"),
            # P44: the relay caps its own recompute, so its flag means "true
            # over the window it read". Passed through rather than dropped —
            # a bare `relay_reported_verified: true` would read as a claim
            # about the whole history, which is exactly what it is not. The
            # `chain_verified_scope` below is this client's own finding and
            # is the one that carries weight.
            "relay_verified_scope": data.get("chain_verified_scope"),
            "signed_chain_head": data.get("chain_head"),
            "message_count": data.get("message_count", 0),
            "verified": False,
            "reason": "",
            "chain_verified_scope": None,
            "replay_scan_scope": None,
            "replayed_nonce_count": 0,
            "replayed_nonce_sample": [],
        }

        pubkey_hex = data.get("origin_public_key_hex", "")
        signature_hex = data.get("signature", "")
        try:
            signable = identity.federation_signable_bytes(
                data.get("origin_relay", self.relay_url), "room_chain",
                [{"room_id": room_id, "chain_head": data.get("chain_head") or "",
                  "message_count": data.get("message_count", 0)}],
            )
            signature_valid = identity.verify_raw(signable, signature_hex, pubkey_hex)
        except Exception:
            signature_valid = False
        if not signature_valid:
            result["reason"] = "relay signature over the chain head did not verify"
            return result
        if not data.get("verified"):
            result["reason"] = f"relay's own recomputation failed: {data.get('message')}"
            return result

        if result["message_count"] == 0:
            result["verified"] = True
            result["reason"] = "empty room — nothing to verify"
            result["chain_verified_scope"] = "genesis"
            result["replay_scan_scope"] = "full_history"
            return result

        pins = identity.load_room_chain_heads()
        pin_key = f"{self.relay_url}|{room_id}"
        pinned = pins.get(pin_key)

        # P25-1: load_room_chain_heads() only guards *whole-file* JSON
        # corruption -- a single malformed entry inside an otherwise-valid
        # file (a bare string, a dict with a non-string chain_head, an
        # empty list) reaches every `pinned.get(...)` below unguarded,
        # producing three different wrong outcomes (a crash, a misleading
        # "history was rewritten", or a silent trust-reset) depending on
        # the exact shape. Catch all three here with one check, matching
        # integrityCheck.ts's getPinnedRoomChainHead() corrupt-pin guard.
        if pinned is not None and not (
            isinstance(pinned, dict) and isinstance(pinned.get("chain_head"), str)
        ):
            result["reason"] = (
                "stored pin entry for this room is malformed — "
                "refusing to trust it as a verification checkpoint"
            )
            return result

        # Fast path: the room hasn't changed since the last fully-verified
        # pin -- that hash is already a commitment to everything before it,
        # so there's nothing new to fetch at all.
        if not ignore_pin and pinned and pinned.get("chain_head") == data.get("chain_head"):
            if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                result["reason"] = "relay's signing key changed since it was last pinned for this room"
                return result
            result["verified"] = True
            result["reason"] = "chain head unchanged since the last pinned verification — nothing new to check"
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "no_new_messages"
            return result

        seen_nonces: set[str] = set()
        replay_sample: list[str] = []
        replay_count = 0

        expected: str | dict[str, Any] = data.get("chain_head") or ""
        before: str | None = None
        before_seq: int | None = None
        fetched = 0
        reached_genesis = False
        reached_pin = False
        pin_matched = False

        while True:
            if max_messages is not None and fetched >= max_messages:
                result["reason"] = (
                    f"could not fetch full history for independent verification — "
                    f"{fetched}/{result['message_count']} messages within the "
                    f"{max_messages}-message cap"
                )
                return result
            page_limit = min(200, result["message_count"] - fetched)
            if max_messages is not None:
                page_limit = min(page_limit, max_messages - fetched)
            page = await self._bearer_request(
                "get", f"{self.relay_url}/rooms/{room_id}/messages",
                params={
                    "limit": page_limit,
                    # P31-3: total cursor — `before` alone can skip a message
                    # sharing a timestamp with the page boundary, turning an
                    # untampered room into a false chain break.
                    **({"before": before, "before_seq": before_seq} if before else {}),
                },
            )
            page.raise_for_status()
            batch = page.json().get("messages", [])
            if len(batch) < page_limit:
                result["reason"] = "relay served fewer messages than its own reported message_count claims exist"
                return result

            for m in batch:
                nonce = m.get("nonce")
                if not nonce:
                    continue
                if nonce in seen_nonces:
                    replay_count += 1
                    if len(replay_sample) < 20:
                        replay_sample.append(nonce)
                seen_nonces.add(nonce)

            fetched += len(batch)
            at_genesis = fetched >= result["message_count"]

            # P24-1/2: the pin can land *anywhere* in a fetched page, not
            # just at its oldest boundary -- a page boundary just happens to
            # be where a plain forward walk would stop looking. Scanning the
            # whole page catches a mid-page pin too (both a correctness
            # requirement -- otherwise a genesis-reaching walk can find the
            # pin nowhere at its own boundaries and wrongly report "history
            # was rewritten" even though the pin's hash truly is present,
            # just mid-page -- and the efficiency this checkpoint exists
            # for: without it, a room with only a few new messages since the
            # pin still pays for a full walk back to genesis).
            pin_index = None
            if pinned and not ignore_pin:
                for i, m in enumerate(batch):
                    if m["chain_hash"] == pinned.get("chain_head"):
                        pin_index = i
                        break

            verify_batch = batch[pin_index:] if pin_index is not None else batch
            verify_at_genesis = at_genesis and pin_index is None

            ok, new_expected, reason = rooms.verify_chain_page(verify_batch, expected, verify_at_genesis)
            if not ok:
                result["reason"] = reason
                return result

            if pin_index is not None:
                pin_matched = True
                if pinned.get("origin_public_key_hex") and pinned["origin_public_key_hex"] != pubkey_hex:
                    result["reason"] = "relay's signing key changed since it was last pinned for this room"
                    return result
                reached_pin = True
                break

            if at_genesis:
                reached_genesis = True
                break

            oldest = batch[0]
            expected = new_expected
            before = oldest["created_at"]
            before_seq = oldest.get("seq")

        if reached_genesis and pinned and not pin_matched and not ignore_pin:
            result["reason"] = (
                "previously pinned chain head is not an ancestor of the current chain — "
                "history was rewritten (reordered or deleted messages)"
            )
            return result

        result["verified"] = True
        result["replayed_nonce_count"] = replay_count
        result["replayed_nonce_sample"] = replay_sample
        if reached_genesis:
            result["chain_verified_scope"] = "genesis"
            result["replay_scan_scope"] = "full_history"
            result["reason"] = f"chain valid, verified from true genesis this call ({fetched} messages)"
        else:
            assert reached_pin
            result["chain_verified_scope"] = "pinned_checkpoint"
            result["replay_scan_scope"] = "since_pinned_head"
            result["reason"] = (
                f"chain valid back to the previously pinned head ({fetched} new messages checked "
                f"this call; earlier history was verified from genesis when that head was first pinned)"
            )

        pins[pin_key] = {"chain_head": data.get("chain_head") or "", "origin_public_key_hex": pubkey_hex}
        identity.save_room_chain_heads(pins)
        return result

    async def send_room_message(
        self,
        room_id: str,
        content: str,
        msg_type: str = DEFAULT_MSG_TYPE,
        parent_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a room. Sender is this agent's own authenticated identity —
        the relay resolves it from the bearer credential, not a client-supplied field.
        Signature is mandatory — the relay rejects an unsigned POST (N2-5), and is bound
        to room_id + msg_type, not just content (N3-8/R7) — a content-only signature was
        valid in any room this agent belongs to, forever."""
        from emporia import crypto_box
        from emporia.wire import (
            MAX_CONTENT_BYTES,
            new_message_nonce,
            room_message_envelope,
            signed_room_message_payload,
        )

        # P19-8 (M5): checked before any network or crypto work — worst case
        # without this is an encrypted room doing a full N-recipient seal and
        # *then* eating the relay's 400. Checked against the plaintext, so this
        # catches the obviously-oversized case; ciphertext runs larger than
        # plaintext — worse here than in a DM, since a room's envelope also grows
        # with member count (one wrapped key per member) — so a borderline size
        # can still fail the N-recipient seal and then 400 at the relay, which
        # is the authoritative check either way. See send_dm()'s identical check
        # and its identical caveat.
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(
                f"content is too large — {len(content.encode('utf-8'))} bytes, "
                f"limit is {MAX_CONTENT_BYTES}",
            )

        room = await self.get_room(room_id)
        encrypted = bool(room.get("encrypted"))
        # P19-5 (H4): one nonce, bound into both the ciphertext's AAD and the
        # signature, same shape as send_dm()'s nonce. Generated once and reused
        # across the membership-race retry below — nothing is stored on the 409
        # branch, so there is nothing for a reused nonce to collide with.
        nonce = new_message_nonce()

        async def _post(body_content: str):
            signed_payload = signed_room_message_payload(room_id, body_content, msg_type, nonce)
            return await self._bearer_request(
                "post", f"{self.relay_url}/rooms/{room_id}/message",
                json=room_message_envelope(
                    body_content, msg_type, self._sign(signed_payload), nonce,
                    parent_message_id=parent_message_id,
                ),
            )

        if not encrypted:
            resp = await _post(content)
            resp.raise_for_status()
            return resp.json()

        aad = crypto_box.message_aad("room", room_id, self.agent_id, msg_type, nonce)
        from emporia import secret_guard
        secret_guard.check(self.agent_id, content)   # P172: before sealing
        resp = await _post(json.dumps(
            crypto_box.seal(content, await self._room_recipients(room_id), aad),
        ))
        # P18-5: a 409 here usually means someone joined between the key fetch
        # and this send — a race, not a bad request, and the relay says so
        # deliberately so it can be recovered rather than surfaced. Re-fetch the
        # member set and try once. Exactly once: a second 409 means membership is
        # churning faster than a round trip, and retrying forever would be a busy
        # loop, not a fix.
        #
        # P20-6 (A7): the room route also answers 409 for a duplicate nonce
        # (idx_room_messages_nonce's UNIQUE constraint) — a *different* failure
        # this retry cannot fix, since it reseals under the same nonce and is
        # guaranteed to hit the identical conflict again.
        # P21-3 (B4): distinguished by the relay's structured `code`, not a
        # substring match against its error prose — the hand-copied-literal
        # class this codebase has closed five times over, reopened by P20-6
        # itself. A response with no code (or an unparseable body) is treated
        # as "not a duplicate nonce", matching the substring match's old
        # fail-open behavior for anything it didn't recognize either.
        if resp.status_code == 409 and error_code(resp.json().get("detail")) != ERR_DUPLICATE_NONCE:
            resp = await _post(json.dumps(
                crypto_box.seal(content, await self._room_recipients(room_id), aad),
            ))
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
        access_duration_days: int | None = None,
    ) -> dict[str, Any]:
        """Create a chat/collab room. Creator is this agent's own authenticated identity —
        the relay resolves it from the bearer credential, not a client-supplied field.

        access_duration_days: for a paid room, how many days one payment's
        access lasts before it must be renewed. Omit (None) for no expiry — access
        lasts as long as the room does. A member's access is checked on every
        request, not just at join time, so a lapsed window locks them out even if
        they never left; a fresh payment after that restores access (see
        join_room). Ignored for non-paid rooms.
        """
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/rooms",
            json={
                "name": name, "description": description,
                "room_type": room_type, "gate_type": gate_type,
                "entry_fee_cents": entry_fee_cents, "currency": currency,
                "encrypted": encrypted, "linked_session_id": linked_session_id,
                "max_members": max_members,
                "access_duration_days": access_duration_days,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def kick_from_room(
        self,
        room_id: str,
        target_agent_id: str,
        refund: bool = False,
    ) -> dict[str, Any]:
        """Remove an agent from a room. Only the room's creator can kick — this
        agent's own authenticated identity (the bearer credential) must be the
        room's creator_id, and must not equal target_agent_id (the creator
        can't kick themselves).

        This is a permanent ban, not a temporary removal: target_agent_id can
        never rejoin this room afterward, regardless of gate_type, and
        regardless of whether they present a fresh, genuinely valid payment or
        a leftover invite. There is no un-kick.

        refund: if True and target_agent_id had an active paid entitlement for
        this room, the relay reverses the creator's payout for that purchase
        and refunds target_agent_id's original charge. Defaults to False — an
        ordinary kick moves no money. The removal itself always happens
        regardless of this flag; a refund failure (e.g. a Stripe error) is
        reported in the response but does not undo the kick. Check the
        returned "refund" field for the outcome: {"refunded": true, ...} on
        success, {"refunded": false, "reason": ...} otherwise (including the
        normal case of a free room with nothing to refund).
        """
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/rooms/{room_id}/kick",
            json={"target_agent_id": target_agent_id, "refund": refund},
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
        access_duration_days: int | None = None,
    ) -> dict[str, Any]:
        """Create an Agora topic. Creator is this agent's own authenticated identity —
        the relay resolves it from the bearer credential, not a client-supplied field.

        access_duration_days: for a paid_invite topic, how many days one payment's
        access lasts before it must be renewed. Omit (None) for no expiry.
        Ignored for non-paid gate types.
        """
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/topics",
            json={
                "name": name, "description": description,
                "visibility": visibility, "gate_type": gate_type,
                "entry_fee_cents": entry_fee_cents, "slug": slug,
                "flair_options": flair_options or [],
                "access_duration_days": access_duration_days,
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
        pay_with_x402: bool = False,
    ) -> dict[str, Any]:
        """Subscribe to an Agora topic (open topics: immediately; invite/paid_invite:
        requires a prior invite from the creator).

        If this agent already has an active, unexpired entitlement for this
        topic (paid before and hasn't been kicked or outlasted the topic's
        access_duration_days), omit both payment_intent_id and mpp_spt — the
        relay recognizes the existing entitlement and re-admits for free
        rather than charging again.

        Or pass pay_with_x402=True to sign the relay's x402 challenge with this
        agent's `x402_private_key` and retry once (P45) — per-call, and never
        implied by merely holding the key.

        Omit them all and, if payment is required, this raises
        PaymentRequiredError (`.detail` the parsed challenge body,
        `.www_authenticate` the header).
        """
        body = {"agent_id": self.agent_id, "payment_intent_id": payment_intent_id}
        # P95: the payment and the payer's identity travel together.
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/topics/{slug}/subscribe", json=body, payment_token=mpp_spt,
        )
        if pay_with_x402:
            resp = await self._pay_and_retry_x402(resp, lambda header: self._bearer_request(
                "post", f"{self.relay_url}/agoras/topics/{slug}/subscribe", json=body,
                headers={"PAYMENT-SIGNATURE": header},
            ))
        _raise_for_status_or_payment(resp)
        return resp.json()

    async def vote_agora_post(self, post_id: str, value: int) -> dict[str, Any]:
        """Upvote (1) or downvote (-1) an Agora post. Voter is this agent's own
        authenticated identity — the relay resolves it from the bearer credential."""
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/posts/{post_id}/vote", json={"value": value},
        )
        resp.raise_for_status()
        return resp.json()

    async def vote_agora_comment(self, comment_id: str, value: int) -> dict[str, Any]:
        """Upvote (1) or downvote (-1) an Agora comment. Voter is this agent's own
        authenticated identity, exactly as vote_agora_post() above.

        P39-1: the relay has served this since the posts endpoint shipped next to
        it; neither client had it, so a comment's score could only ever be read.
        """
        resp = await self._bearer_request(
            "post", f"{self.relay_url}/agoras/comments/{comment_id}/vote",
            json={"value": value},
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
