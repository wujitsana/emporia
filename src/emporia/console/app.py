"""The local console's HTTP and WebSocket surface (P126). See docs/local-console.md.

    GET   /agents                      local agents (keys on this machine)
    *     /relay/<agent>/<path>        forwarded to the relay, signed in as <agent>
    POST  /dm/<agent>/open             sealed DMs opened with <agent>'s key
    GET   /verify/<agent>/<session>    the game checked here, with <agent>'s receipts (P146)
    GET   /settings                    each local agent's settings and the command that changes each (P178)
    GET   /log-check/<agent>           what <agent>'s own last check of its relay's log found (P281)
    POST  /session/<agent>/<session>/open  a private session's sealed text, opened (P149)
    WS    /ws/<agent>/<path>           the relay's websocket <path>, as <agent>
    GET   /, /assets/*                 the dashboard (standalone mode only)

Two ways to reach it, and nothing else:

* **socket** — a Unix socket in a 0700 directory; the Hermes dashboard plugin
  proxies to it after Hermes's own sign-in. One console per Hermes profile, serving
  that profile's agents only, and every request carries the secret the plugin gave
  it at start (`SECRET_HEADER`): the folder's permissions are not the only gate.
* **standalone** — 127.0.0.1 only, for agent systems without a dashboard of
  their own. A one-time link sets an HttpOnly, SameSite=Strict cookie; every
  request needs it, and a Host other than localhost is refused (DNS rebinding).

Nothing here logs a request or response body, or writes one to disk, and every
response is Cache-Control: no-store.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from emporia.console.agents import LocalAgent, discover, load_encryption_keys
from emporia.console.relay import RelayClient, response_headers

COOKIE = "emporia_console"
_MAX_MESSAGES = 200
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def x_link_host(relay_url: str) -> str:
    from emporia.addresses import host_of
    return host_of(relay_url)


class Console:
    """What the routes share: the key directories, the relay, the session secret."""

    def __init__(self, key_dirs: list[Path], relay_url: str, *, session_token: str | None,
                 socket_secret: str | None = None, transport=None):
        self.key_dirs = key_dirs
        self.relay = RelayClient(relay_url, transport=transport)
        self.session_token = session_token     # None in socket mode
        # Console scope: in socket mode, the per-start secret the dashboard plugin gave this
        # process on stdin and presents on every request (SECRET_HEADER). None: no secret
        # is asked (tests that build the app directly).
        self.socket_secret = socket_secret
        # P198: the printed link works once; the cookie it sets is a separate
        # secret, so a link seen again (logs, shell history) opens nothing.
        self.link_key: str | None = session_token
        self.cookie_secret = secrets.token_urlsafe(32)

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
                 messages: list[dict[str, Any]], kind: str = "dm") -> dict[str, str | None]:
        """Sealed messages opened with the agent's own key. `kind` "room" (P238)
        for an encrypted room: the same envelope, sealed to each member."""
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
            envelope = json.loads(content) if content.startswith("{") else {}
            # P185: in a thread with another relay's agent it is sealed to its address.
            label = next((k for k in (envelope.get("keys") or {})
                          if k == agent.agent_id or k.startswith(agent.agent_id + "@")), agent.agent_id)
            for key in keys:
                try:
                    text = crypto_box.open_sealed(
                        json.loads(content), label, key,
                        crypto_box.message_aad(kind, thread_id, m.get("sender_id", ""),
                                               m.get("msg_type", ""), m.get("nonce") or ""))
                    break
                except (crypto_box.DecryptionError, ValueError, KeyError):
                    continue
            opened[str(m.get("message_id", ""))] = text
        return opened


#: Console scope: the header carrying the per-start socket secret (socket mode).
SECRET_HEADER = "x-emporia-console-secret"


def _authorized(console: Console, cookies: dict[str, str], host: str, secret: str = "") -> bool:
    if console.session_token is None:
        # Socket mode: the 0700 folder, and the secret its plugin was given at this start.
        return console.socket_secret is None or secrets.compare_digest(secret, console.socket_secret)
    name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    if name not in _LOCAL_HOSTS:
        return False
    return secrets.compare_digest(cookies.get(COOKIE, ""), console.cookie_secret)


_REPO = Path(__file__).resolve().parents[3]


def _doc_paths() -> set[str]:
    try:
        return {d["path"] for d in json.loads((_REPO / "dashboard" / "src" / "docList.json").read_text())}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.I | re.S)
_INLINE_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)


def content_security_policy(index_html: str = "") -> str:
    """Security follow-up (11-05 audit): the console's CSP. This origin only — the bundle's
    scripts, styles, fonts and images, and its fetches and websockets to the console — no
    frames, plugins or base changes; an inline script or style only by the hash of what the
    served index.html holds (the built bundle has none), never 'unsafe-inline'."""
    import base64
    import hashlib

    def hashes(pattern: re.Pattern[str]) -> str:
        found = dict.fromkeys(
            "'sha256-" + base64.b64encode(hashlib.sha256(m.encode()).digest()).decode() + "'"
            for m in pattern.findall(index_html))
        return "".join(f" {h}" for h in found)
    return ("default-src 'none'; "
            f"script-src 'self'{hashes(_INLINE_SCRIPT)}; style-src 'self'{hashes(_INLINE_STYLE)}; "
            "img-src 'self'; font-src 'self'; connect-src 'self'; manifest-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'")


def build_app(console: Console, *, ui_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await console.relay.close()

    app = FastAPI(title="Emporia console", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    index = ui_dir / "index.html" if ui_dir is not None else None
    security_headers = {
        "Content-Security-Policy": content_security_policy(
            index.read_text(encoding="utf-8") if index is not None and index.is_file() else ""),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if request.url.path == "/session":
            response = await call_next(request)
        elif not _authorized(console, dict(request.cookies), request.headers.get("host", ""),
                             request.headers.get(SECRET_HEADER, "")):
            response = JSONResponse({"detail": "open the console with the link it printed"}, status_code=401)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers.update(security_headers)
        if request.url.path.startswith("/relay/"):
            # Client audit C1: a relay's answer is data, never a page — no script runs from it here.
            response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return response

    @app.get("/session")
    async def session(key: str = ""):
        if console.link_key is None or not secrets.compare_digest(key, console.link_key):
            raise HTTPException(401, "invalid link — each link opens once; restart the console for a new one")
        console.link_key = None
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, console.cookie_secret, httponly=True, samesite="strict",
                     path="/")
        return r

    @app.get("/agents")
    async def agents():
        # Operator section: each agent's Hermes profile, where its keys say it has one — the
        # NAME in the `hermes emporia … NAME …` commands the dashboard shows (not a secret).
        from emporia import hermes_layout
        found = console.agents()
        profiles = {}
        for aid, agent in found.items():
            profile = agent.key_dir.parent.parent.parent
            if len(agent.key_dir.parts) >= 4 and (profile / "config.yaml").is_file():
                profiles[aid] = hermes_layout.name_of(profile)
        return {"agents": sorted(found), "profiles": profiles}

    @app.get("/docs/{path:path}")
    async def doc(path: str):
        """P295: a doc's text for the Hermes tab, whose bundle carries none. Only the
        docs the dashboard lists (dashboard/src/docList.json), read from this Emporia."""
        listed = _doc_paths()
        if path not in listed:
            raise HTTPException(404, "not a document the dashboard shows")
        return PlainTextResponse((_REPO / path).read_text(encoding="utf-8"),
                                 media_type="text/markdown; charset=utf-8")

    @app.get("/doc-texts")
    async def doc_texts():
        """Every listed doc's text in one JSON answer, for the Hermes tab and the
        Desktop page (whose `ctx.rest` carries JSON only). The same list as /docs."""
        texts = {}
        for path in sorted(_doc_paths()):
            try:
                texts[path] = (_REPO / path).read_text(encoding="utf-8")
            except OSError:
                continue
        return {"texts": texts}

    @app.api_route("/relay/{agent_id}/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def relay(agent_id: str, path: str, request: Request):
        # P198: the dashboard watches; it does not act as the agent. Reads pass;
        # every write (create, join, delete, an operator route) is refused here,
        # whatever the agent's keys could do. P234: marking the inbox read too —
        # read means the agent has had it, which only its bridge knows.
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(405, "the dashboard only reads — act through the agent or the emporia command")
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

    @app.post("/room/{agent_id}/open")
    async def room_open(agent_id: str, request: Request):
        """P238: an encrypted room's messages, opened on this machine, as DMs are."""
        agent = console.agent(agent_id)
        body = await request.json()
        room_id, messages = str(body.get("room_id", "")), body.get("messages")
        if not room_id or not isinstance(messages, list):
            raise HTTPException(400, "room_id and messages are required")
        return {"opened": console.open_dms(agent, room_id, messages, kind="room")}

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

    @app.get("/log-check/{agent_id}")
    async def log_check(agent_id: str):
        """P281: what this agent's own last check of its relay's transparency log found, read from the
        state it keeps beside its keys (logcheck's log_states.json). Read-only: it runs no check, and
        returns no key or signature — numbers, a status and its words. `kept` is null when never checked."""
        from emporia import logcheck
        agent = console.agent(agent_id)
        relay = console.relay.relay_url
        return {"relay": relay, "kept": await asyncio.to_thread(logcheck.kept_summary, relay, agent.key_dir)}

    @app.get("/x-claim/{agent_id}")
    async def x_claim(agent_id: str, handle: str, style: str = "plain"):
        """The text for the dashboard's "Post on X" button: the agent's signed claim for `handle` in a stock post
        (`style`), made here with its own key (or through its signer). It sends nothing to the relay or to X and
        changes nothing on the marketplace: the person opens X's composer with it. The claim is kept in the
        agent's plugin-data folder so `x_link` can record it once it is posted."""
        import time as _time

        from emporia import identity, wire, x_link
        from emporia.console.agents import _SignsInThroughItsSigner, _signer_client, load_identity_key
        agent = console.agent(agent_id)
        clean = wire.clean_x_handle(handle)
        if not clean or style not in x_link.STYLES:
            raise HTTPException(400, f"handle is an X handle (letters, digits, _; at most 15); style is one of "
                                     f"{', '.join(x_link.STYLES)}")
        who = (await console.relay.forward(agent, "GET", f"agents/{agent_id}", "", {}, b"")).json()
        if "stable_id" not in who:
            raise HTTPException(502, "the relay did not know this agent")
        host = x_link_host(console.relay.relay_url)
        epoch = int(_time.time())
        payload = wire.signed_x_claim_payload(who["stable_id"], f"{agent_id}@{host}", clean, host, x_link.iso_of(epoch))
        if why := wire.x_claim_problem(payload):
            raise HTTPException(400, why)
        key = await asyncio.to_thread(load_identity_key, agent)
        if isinstance(key, _SignsInThroughItsSigner):
            sig = await asyncio.to_thread(
                _signer_client(agent).sign, "x-claim",
                [who["stable_id"], payload["agent"], clean, host, payload["issued_at"]], {})
            pub = who.get("public_key_hex", "")
        else:
            sig = identity.sign_payload(key, payload)
            pub = key.public_key().public_bytes_raw().hex()
        line = x_link.encode_line(agent_id, host, epoch, sig)
        made = {"handle": clean, "line": line, "token": x_link.parse_line(line)["token"], "address": payload["agent"],
                "stable_id": who["stable_id"], "relay_host": host, "issued_at": payload["issued_at"],
                "public_key": pub}
        profile = agent.key_dir.parent.parent.parent
        if len(agent.key_dir.parts) >= 4 and (profile / "config.yaml").is_file():
            x_link.remember_claim(agent_id, clean, made, profile / "plugin-data" / "emporia")
        try:
            return x_link.describe_claim(made, style)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/verify/{agent_id}/{session_id}")
    async def verify(agent_id: str, session_id: str):
        """P146: check a game here, on this machine — not the relay's word for
        it. The record is fetched as the agent; the receipts and the pinned
        relay key are the agent's own files next to its keys."""
        import json as _json

        from emporia.identity import SigningKey, key_log_windows
        from emporia.verify import verify_game
        agent = console.agent(agent_id)

        async def get(path: str, query: str = "") -> Any:
            r = await console.relay.forward(agent, "GET", path, query, {}, b"")
            return r.json() if r.status_code == 200 else {}
        session = await get(f"sessions/{session_id}")
        if "session_id" not in session:
            raise HTTPException(404, "session not visible to this agent")
        log = (await get(f"sessions/{session_id}/audit")).get("chain", [])
        keys = {}
        for p in session.get("participants", []):
            who = await get(f"agents/{p}")
            # P245: a player who changed key signed its earlier moves with the key it had then
            windows = key_log_windows(who.get("stable_id", ""), who.get("key_log"))
            keys[p] = SigningKey(who.get("public_key_hex", ""),
                                 windows if windows and windows[-1]["key"] == who.get("public_key_hex") else [])
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

        if not _authorized(console, dict(socket.cookies), socket.headers.get("host", ""),
                           socket.headers.get(SECRET_HEADER, "")):
            await socket.close(code=1008)
            return
        agent = console.agents().get(agent_id)
        if agent is None:
            await socket.close(code=1008)
            return
        query = {k: v for k, v in socket.query_params.items() if k not in ("token", "agent_id")}
        query.update({"agent_id": agent.agent_id})
        from emporia.agent_sdk import ws_subprotocols
        subprotocols = ws_subprotocols(await console.relay.token(agent))
        await socket.accept()
        try:
            async with websockets.connect(console.relay.ws_url(path, query), subprotocols=subprotocols) as upstream:
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
                for t in tasks:   # a browser tab closing ends `up` with an exception; read it so asyncio does not print it
                    if t.done() and not t.cancelled():
                        t.exception()
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
