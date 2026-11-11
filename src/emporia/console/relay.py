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

from emporia.console.agents import LocalAgent, dashboard_sign_in

# Hop-by-hop and identity headers never forwarded in either direction.
_DROP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
         "proxy-authorization", "authorization", "x-emporia-authorization",
         "x-emporia-agent-id", "cookie", "set-cookie", "content-length", "content-encoding",
         # The caller's own encodings (the Hermes host's client offers br) are not ours to accept: this
         # client decodes what it asks for itself (httpx's default), and a body it cannot decode would be
         # passed on as bytes the tab reads as broken JSON. Behind a CDN that compresses, it did.
         "accept-encoding"}


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
            signature = dashboard_sign_in(agent, ch["nonce"], self.relay_url)   # M4: names this relay
            r = await self._http.post(
                f"{self.relay_url}/dashboard/session",
                json=dashboard_session_body(agent.agent_id, ch["challenge_id"], self.relay_url, signature))
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


#: What a relay answer may be served as on the console's own origin. Anything else (HTML, SVG, XML, a
#: script) is served as plain text, so a hostile relay cannot run a page here (client audit C1).
_SAFE_TYPES = ("application/json", "application/x-ndjson", "text/plain")
#: Only these relay headers reach the page: never a redirect, a cookie or a policy of the relay's own.
_KEEP = {"content-type", "content-disposition", "x-next-since", "x-export-format", "x-export-count",
         "retry-after", "etag", "last-modified"}


def response_headers(r: httpx.Response) -> dict[str, str]:
    out = {k: v for k, v in r.headers.items() if k.lower() in _KEEP}
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in _SAFE_TYPES:
        out = {k: v for k, v in out.items() if k.lower() != "content-type"}
        out["Content-Type"] = "text/plain; charset=utf-8"
    out["Cache-Control"] = "no-store"
    out["X-Content-Type-Options"] = "nosniff"
    out["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return out
