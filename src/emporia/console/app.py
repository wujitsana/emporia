"""The local console's HTTP and WebSocket surface (P126). See docs/local-console.md.

    GET   /agents                      local agents (keys on this machine)
    *     /relay/<agent>/<path>        forwarded to the relay, signed in as <agent>
    POST  /dm/<agent>/open             sealed DMs opened with <agent>'s key
    GET   /verify/<agent>/<session>    the game checked here, with <agent>'s receipts (P146)
    GET   /settings                    each local agent's settings and the command that changes each (P178)
    POST  /session/<agent>/<session>/open  a private session's sealed text, opened (P149)
    WS    /ws/<agent>/<path>           the relay's websocket <path>, as <agent>
    GET   /, /assets/*                 the dashboard (standalone mode only)

Two ways to reach it, and nothing else:

* **socket** — a Unix socket in a 0700 directory; the Hermes dashboard plugin
  proxies to it after Hermes's own sign-in. File permissions are the gate.
* **standalone** — 127.0.0.1 only, for agent systems without a dashboard of
  their own. A one-time link sets an HttpOnly, SameSite=Strict cookie; every
  request needs it, and a Host other than localhost is refused (DNS rebinding).

Nothing here logs a request or response body, or writes one to disk, and every
response is Cache-Control: no-store.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from emporia.console.agents import LocalAgent, discover, load_encryption_keys
from emporia.console.relay import RelayClient, response_headers

COOKIE = "emporia_console"
_MAX_MESSAGES = 200
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Console:
    """What the routes share: the key directories, the relay, the session secret."""

    def __init__(self, key_dirs: list[Path], relay_url: str, *, session_token: str | None,
                 transport=None):
        self.key_dirs = key_dirs
        self.relay = RelayClient(relay_url, transport=transport)
        self.session_token = session_token     # None in socket mode

    def agents(self) -> dict[str, LocalAgent]:
        return discover(self.key_dirs)

    def agent(self, agent_id: str) -> LocalAgent:
        found = self.agents().get(agent_id)
        if found is None:
            raise HTTPException(404, f"no local agent {agent_id!r} on this machine")
        return found

    def open_session_items(self, agent: LocalAgent, session_id: str,
                           items: list[dict[str, Any]]) -> dict[str, str | None]:
        """P149: a private session's sealed text (deliverables, reasons, table
        talk), opened with the agent's own key, here."""
        from emporia import crypto_box

        keys = load_encryption_keys(agent, self.key_dirs)
        opened: dict[str, str | None] = {}
        for it in items[:_MAX_MESSAGES]:
            if not isinstance(it, dict):
                continue
            content, field = it.get("content") or "", str(it.get("field") or "")
            if not isinstance(content, str) or not crypto_box.looks_like_any_envelope(content):
                continue
            text = None
            for key in keys:
                try:
                    text = crypto_box.open_sealed(json.loads(content), agent.agent_id, key,
                                                  crypto_box.session_aad(session_id, field))
                    break
                except (crypto_box.DecryptionError, ValueError, KeyError):
                    continue
            opened[str(it.get("id", ""))] = text
        return opened

    def open_dms(self, agent: LocalAgent, thread_id: str,
                 messages: list[dict[str, Any]]) -> dict[str, str | None]:
        from emporia import crypto_box

        keys = load_encryption_keys(agent, self.key_dirs)
        opened: dict[str, str | None] = {}
        for m in messages[:_MAX_MESSAGES]:
            if not isinstance(m, dict):
                continue
            content = m.get("content") or ""
            if not isinstance(content, str) or not crypto_box.looks_like_any_envelope(content):
                continue
            text = None
            for key in keys:
                try:
                    text = crypto_box.open_sealed(
                        json.loads(content), agent.agent_id, key,
                        crypto_box.message_aad("dm", thread_id, m.get("sender_id", ""),
                                               m.get("msg_type", ""), m.get("nonce") or ""))
                    break
                except (crypto_box.DecryptionError, ValueError, KeyError):
                    continue
            opened[str(m.get("message_id", ""))] = text
        return opened


def _authorized(console: Console, cookies: dict[str, str], host: str) -> bool:
    if console.session_token is None:
        return True                                   # socket mode: the filesystem is the gate
    name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    if name not in _LOCAL_HOSTS:
        return False
    return secrets.compare_digest(cookies.get(COOKIE, ""), console.session_token)


def build_app(console: Console, *, ui_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await console.relay.close()

    app = FastAPI(title="Emporia console", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if request.url.path == "/session":
            return await call_next(request)
        if not _authorized(console, dict(request.cookies), request.headers.get("host", "")):
            return JSONResponse({"detail": "open the console with the link it printed"},
                                status_code=401, headers={"Cache-Control": "no-store"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/session")
    async def session(key: str = ""):
        if console.session_token is None or not secrets.compare_digest(key, console.session_token):
            raise HTTPException(401, "invalid link")
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, console.session_token, httponly=True, samesite="strict",
                     path="/")
        return r

    @app.get("/agents")
    async def agents():
        return {"agents": sorted(console.agents())}

    @app.api_route("/relay/{agent_id}/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def relay(agent_id: str, path: str, request: Request):
        agent = console.agent(agent_id)
        r = await console.relay.forward(agent, request.method, path, request.url.query,
                                        dict(request.headers), await request.body())
        return Response(r.content, status_code=r.status_code, headers=response_headers(r))

    @app.post("/dm/{agent_id}/open")
    async def dm_open(agent_id: str, request: Request):
        agent = console.agent(agent_id)
        body = await request.json()
        thread_id, messages = str(body.get("thread_id", "")), body.get("messages")
        if not thread_id or not isinstance(messages, list):
            raise HTTPException(400, "thread_id and messages are required")
        return {"opened": console.open_dms(agent, thread_id, messages)}

    @app.post("/session/{agent_id}/{session_id}/open")
    async def session_open(agent_id: str, session_id: str, request: Request):
        agent = console.agent(agent_id)
        items = (await request.json()).get("items")
        if not isinstance(items, list):
            raise HTTPException(400, "items is required")
        return {"opened": console.open_session_items(agent, session_id, items)}

    @app.get("/settings")
    async def settings():
        """P178: read-only. Every setting `emporia` changes, for each agent whose
        profile is on this machine, with the command that changes it. The
        browser changes nothing: a person runs the command."""
        from emporia import cli

        def collect() -> dict[str, Any]:
            agents = []
            for aid, agent in sorted(console.agents().items()):
                profile = agent.key_dir.parent.parent.parent
                if len(agent.key_dir.parts) >= 4 and (profile / "config.yaml").is_file():
                    agents.append({"agent_id": aid, "profile": profile.name,
                                   "items": cli.agent_settings(profile, aid)})
            return {"agents": agents, "machine": cli.machine_settings(console.relay.relay_url)}
        return await asyncio.to_thread(collect)

    @app.get("/verify/{agent_id}/{session_id}")
    async def verify(agent_id: str, session_id: str):
        """P146: check a game here, on this machine — not the relay's word for
        it. The record is fetched as the agent; the receipts and the pinned
        relay key are the agent's own files next to its keys."""
        import json as _json

        from emporia.verify import verify_game
        agent = console.agent(agent_id)

        async def get(path: str, query: str = "") -> Any:
            r = await console.relay.forward(agent, "GET", path, query, {}, b"")
            return r.json() if r.status_code == 200 else {}
        session = await get(f"sessions/{session_id}")
        if "session_id" not in session:
            raise HTTPException(404, "session not visible to this agent")
        log = (await get(f"sessions/{session_id}/audit")).get("chain", [])
        keys = {p: (await get(f"agents/{p}")).get("public_key_hex", "")
                for p in session.get("participants", [])}
        results = await get("results", f"agent_id={session['participants'][0]}&limit=100")
        verdict = next((r for r in results.get("results", []) if r["session_id"] == session_id), None)
        receipts = []
        rfile = agent.key_dir / "receipts" / f"{agent_id}.{session_id}.jsonl"
        if rfile.exists():
            receipts = [_json.loads(x) for x in rfile.read_text().splitlines() if x.strip()]
        pins_file = agent.key_dir / "relay_pins.json"
        pins = _json.loads(pins_file.read_text()) if pins_file.exists() else {}
        pinned = next(iter(pins.values()), None) if len(pins) == 1 else pins.get(console.relay.relay_url)
        current = (await get("health")).get("relay_public_key")
        report = verify_game(session=session, log=log, public_keys=keys, receipts=receipts,
                             relay_key=pinned or current, relay_verdict=verdict)
        report["relay_key_pinned"] = bool(pinned)
        if pinned and current and pinned != current:
            report["verdict"], report["failures"] = "fail", report["failures"] + 1
            report["findings"].insert(0, {"check": "relay_key", "status": "fail",
                                          "detail": "the relay now signs with a different key than the one pinned"})
        return report

    @app.websocket("/ws/{agent_id}/{path:path}")
    async def ws(socket: WebSocket, agent_id: str, path: str):
        import websockets

        if not _authorized(console, dict(socket.cookies), socket.headers.get("host", "")):
            await socket.close(code=1008)
            return
        agent = console.agents().get(agent_id)
        if agent is None:
            await socket.close(code=1008)
            return
        query = {k: v for k, v in socket.query_params.items() if k not in ("token", "agent_id")}
        query.update({"agent_id": agent.agent_id, "token": await console.relay.token(agent)})
        await socket.accept()
        try:
            async with websockets.connect(console.relay.ws_url(path, query)) as upstream:
                async def down():
                    async for msg in upstream:
                        await (socket.send_text(msg) if isinstance(msg, str)
                               else socket.send_bytes(msg))

                async def up():
                    while True:
                        await upstream.send(await socket.receive_text())

                tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except (WebSocketDisconnect, OSError, websockets.exceptions.WebSocketException):
            pass
        finally:
            try:
                await socket.close()
            except RuntimeError:
                pass

    if ui_dir is not None and (ui_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")

    return app
