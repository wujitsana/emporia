"""The console's side of the relay: signed in as each local agent (P126).

The relay — possibly someone else's — sees an ordinary agent session: the
`/dashboard/challenge` → `/dashboard/session` exchange every Emporia client
uses, signed with the agent's own Ed25519 key. No "trust this machine" setting
is involved, so this works against any relay.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from emporia.console.agents import LocalAgent, load_identity_key

# Hop-by-hop and identity headers never forwarded in either direction.
_DROP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
         "proxy-authorization", "authorization", "x-emporia-authorization",
         "x-emporia-agent-id", "cookie", "set-cookie", "content-length", "content-encoding"}


class RelayClient:
    def __init__(self, relay_url: str, *, timeout: float = 150.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.relay_url = relay_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._tokens: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def token(self, agent: LocalAgent, *, fresh: bool = False) -> str:
        """A relay session token for `agent`, cached until shortly before expiry."""
        lock = self._locks.setdefault(agent.agent_id, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(agent.agent_id)
            if cached and not fresh and cached[1] > time.time() + 30:
                return cached[0]
            from emporia.wire import dashboard_session_body
            ch = (await self._http.post(f"{self.relay_url}/dashboard/challenge")).json()
            signature = load_identity_key(agent).sign(ch["nonce"].encode()).hex()
            r = await self._http.post(
                f"{self.relay_url}/dashboard/session",
                json=dashboard_session_body(agent.agent_id, ch["challenge_id"], signature))
            r.raise_for_status()
            data = r.json()
            self._tokens[agent.agent_id] = (data["token"], time.time() + data.get("expires_in", 3600))
            return data["token"]

    async def forward(self, agent: LocalAgent, method: str, path: str, query: str,
                      headers: dict[str, str], body: bytes) -> httpx.Response:
        """One request to the relay as `agent`, re-signing once if refused."""
        url = f"{self.relay_url}/{path.lstrip('/')}" + (f"?{query}" if query else "")
        sent = {k: v for k, v in headers.items() if k.lower() not in _DROP}
        for attempt in (0, 1):
            sent["Authorization"] = f"Bearer {await self.token(agent, fresh=attempt == 1)}"
            r = await self._http.request(method, url, headers=sent, content=body)
            if r.status_code not in (401, 403) or attempt == 1:
                return r
        return r  # pragma: no cover

    def ws_url(self, path: str, query: dict[str, str]) -> str:
        from urllib.parse import urlencode
        base = "ws" + self.relay_url.removeprefix("http")
        return f"{base}/{path.lstrip('/')}" + (f"?{urlencode(query)}" if query else "")


def response_headers(r: httpx.Response) -> dict[str, str]:
    out = {k: v for k, v in r.headers.items() if k.lower() not in _DROP}
    out["Cache-Control"] = "no-store"
    return out
