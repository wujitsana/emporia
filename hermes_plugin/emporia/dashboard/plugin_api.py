"""Emporia tab of the Hermes dashboard: a proxy to the Emporia console (P126).

Imported by the Hermes dashboard process, under Hermes's interpreter, and
mounted at /api/plugins/emporia/. It imports nothing from Emporia: the work —
agent keys, signing in to the relay, opening DMs — happens in the Emporia
console, a separate process in Emporia's own venv, reached over a Unix socket
in a 0700 directory. No TCP port. See docs/local-console.md.

Console scope: one console per Hermes profile — the one Hermes scoped the request to
(`?profile=`, from the dashboard's profile switcher) — serving that profile's agents
only, and each start gets a secret on stdin that every request here presents.

Hermes authenticates every HTTP request here (its /api/ gate). WebSocket
upgrades it leaves to the route, so ``ws`` below checks the credential with
Hermes's own ``_ws_auth_ok`` and refuses when it cannot (fail closed).

Nothing is logged except the console's start; request and response bodies —
which include decrypted DMs — never are. Hermes's own credentials (cookie,
session token, Authorization, ticket) are never forwarded to the console.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, Response

router = APIRouter()
log = logging.getLogger("emporia.dashboard_plugin")

CONFIG_NAME = "emporia.json"     # in <HERMES_HOME>/plugin-data/emporia/, written by `emporia setup agent`
_CONSOLE_START_S = 25.0
_DROP_HEADERS = {"host", "cookie", "authorization", "connection", "content-length",
                 "accept-encoding", "transfer-encoding", "upgrade", "te", "keep-alive"}
_WS_AUTH_PARAMS = {"ticket", "token", "internal", "profile"}

#: Console scope: the header carrying a console's per-start secret (console.app.SECRET_HEADER).
SECRET_HEADER = "x-emporia-console-secret"


class _Console:
    """The console of one Hermes profile, as this dashboard started it."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.secret = ""
        self.lock = asyncio.Lock()


#: Console scope: one console per Hermes profile home, each serving that profile's agents only,
#: each with its own secret. Before, one console served every profile under the Hermes root.
_consoles: dict[str, _Console] = {}


@atexit.register
def _stop_console() -> None:
    """Each console lives as long as the dashboard that started it."""
    for c in _consoles.values():
        if c.proc is not None and c.proc.poll() is None:
            c.proc.terminate()


def _home() -> Path:
    """The Hermes profile this request is for: Hermes scopes each plugin request to the
    `?profile=` its dashboard asks for (the profile switcher), else its own."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def _config() -> dict:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return json.loads((plugin_data_dir("emporia") / CONFIG_NAME).read_text())
    except (ImportError, OSError, ValueError):
        return {}


def _socket() -> Path:
    runtime = _config().get("runtime_dir")
    if not runtime:
        from plugins.plugin_storage import plugin_data_dir
        runtime = plugin_data_dir("emporia") / "console"
    return Path(runtime) / "console.sock"


def _transport(sock: Path) -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(uds=str(sock))


async def _console_up(sock: Path, secret: str) -> bool:
    if not secret or not sock.exists():
        return False
    try:
        async with httpx.AsyncClient(transport=_transport(sock), timeout=3) as c:
            return (await c.get("http://console/agents", headers={SECRET_HEADER: secret})).status_code == 200
    except httpx.HTTPError:
        return False


class NotSetUp(RuntimeError):
    """This profile has no Emporia agent yet: the tab says how to make one (P296)."""


async def _ensure_console() -> tuple[Path, str]:
    """This profile's console socket and its secret, starting the console first if it is not
    running (or was not started by this dashboard: its secret is unknown here)."""
    home = _home()
    console = _consoles.setdefault(str(home), _Console())
    sock = _socket()
    if await _console_up(sock, console.secret):
        return sock, console.secret
    async with console.lock:
        if await _console_up(sock, console.secret):
            return sock, console.secret
        cfg = _config()
        if not cfg.get("python"):
            raise NotSetUp("Emporia is not set up in this profile — run `hermes emporia setup`")
        if console.proc is not None and console.proc.poll() is None:
            console.proc.terminate()
        runtime = sock.parent
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        # Console scope: this profile only (never --hermes-root), and a fresh secret, given on
        # stdin — not in its arguments (anyone on the machine reads those) or environment.
        argv = [cfg["python"], "-m", "emporia.console", "--uds", str(sock),
                "--relay-url", cfg.get("relay_url", "http://localhost:8088"), "--profile", str(home)]
        # Only what the console needs: no Hermes secrets in its environment, and
        # no .env merged into it either (it is told everything it needs).
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE")}
        env["EMPORIA_NO_DOTENV"] = "1"
        secret = secrets.token_urlsafe(32)
        log_file = open(runtime / "console.log", "ab")
        console.proc = subprocess.Popen(argv, env=env, stdout=log_file, stderr=log_file,
                                        stdin=subprocess.PIPE, start_new_session=True)
        try:
            console.proc.stdin.write((secret + "\n").encode())
            console.proc.stdin.flush()
            console.proc.stdin.close()
        except OSError:
            pass                                   # it exited at once; its log says why
        console.secret = secret
        log.info("Emporia: started the console for %s (pid %s)", home.name, console.proc.pid)
        deadline = asyncio.get_running_loop().time() + _CONSOLE_START_S
        while asyncio.get_running_loop().time() < deadline:
            if await _console_up(sock, secret):
                return sock, secret
            if console.proc.poll() is not None:
                break
            await asyncio.sleep(0.25)
        raise RuntimeError(f"the Emporia console did not start; see {runtime / 'console.log'}")


@router.websocket("/ws/{path:path}")
async def ws(socket: WebSocket, path: str):
    import websockets

    try:
        from hermes_cli import web_server_chat
        authorized = bool(web_server_chat._ws_auth_ok(socket))
    except Exception:
        authorized = False                                    # fail closed
    if not authorized:
        await socket.close(code=1008)
        return
    try:
        sock, secret = await _ensure_console()
    except RuntimeError:
        await socket.close(code=1011)
        return
    query = urlencode([(k, v) for k, v in socket.query_params.multi_items()
                       if k not in _WS_AUTH_PARAMS])
    uri = f"ws://console/ws/{path}" + (f"?{query}" if query else "")
    await socket.accept()
    try:
        async with websockets.unix_connect(str(sock), uri=uri,
                                           additional_headers={SECRET_HEADER: secret}) as upstream:
            async def down():
                async for msg in upstream:
                    await (socket.send_text(msg) if isinstance(msg, str) else socket.send_bytes(msg))

            async def up():
                while True:
                    await upstream.send(await socket.receive_text())

            tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await socket.close()
        except RuntimeError:
            pass


@router.get("/_hermes_profile")
async def hermes_profile(request: Request):
    """P227: the Hermes profile this tab is open in — Hermes adds ?profile= to
    a request from any profile but the one the dashboard was launched as — so
    the tab views as that profile's agent rather than the first one listed."""
    name = request.query_params.get("profile", "").strip()
    if not name:
        home = Path(os.getenv("HERMES_HOME", "")).expanduser()
        name = home.name if home.parent.name == "profiles" else ""
    return JSONResponse({"profile": name}, headers={"Cache-Control": "no-store"})


_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _other_agent_profiles(home: Path) -> list[str]:
    """The machine's other Hermes profiles set up as Emporia agents (their tab settings exist):
    names only — no key, .env or console of theirs is read."""
    root = home.parent.parent if home.parent.name == "profiles" else home
    found = sorted(s.parents[2].name for s in (root / "profiles").glob(f"*/plugin-data/emporia/{CONFIG_NAME}"))
    return [n for n in found if _PROFILE_NAME.fullmatch(n) and root / "profiles" / n != home]


@router.get("/agents")
async def agents():
    """Console scope: this profile's console answers for its own agents only. The tab's profile
    picker (P234) switches Hermes to another agent's profile, so it is also told the names of
    the other agent profiles here — after this profile's own, which it asks the relay through."""
    try:
        sock, secret = await _ensure_console()
    except RuntimeError as e:
        return JSONResponse({"detail": str(e), "setup_needed": isinstance(e, NotSetUp)}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    async with httpx.AsyncClient(transport=_transport(sock), timeout=30) as c:
        r = await c.get("http://console/agents", headers={SECRET_HEADER: secret})
    if r.status_code != 200:
        return Response(r.content, status_code=r.status_code, headers={"Cache-Control": "no-store"})
    own = [str(a) for a in (r.json().get("agents") or [])]
    others = [n for n in _other_agent_profiles(_home()) if n not in own]
    return JSONResponse({"agents": own + others}, headers={"Cache-Control": "no-store"})


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    try:
        sock, secret = await _ensure_console()
    except RuntimeError as e:
        return JSONResponse({"detail": str(e), "setup_needed": isinstance(e, NotSetUp)}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _DROP_HEADERS and not k.lower().startswith("x-hermes")
               and k.lower() != SECRET_HEADER}
    headers[SECRET_HEADER] = secret
    query = urlencode([(k, v) for k, v in request.query_params.multi_items()
                       if k != "profile"])
    async with httpx.AsyncClient(transport=_transport(sock), timeout=180) as c:
        r = await c.request(request.method, f"http://console/{path}" + (f"?{query}" if query else ""),
                            headers=headers, content=await request.body())
    out = {k: v for k, v in r.headers.items()
           if k.lower() in ("content-type", "content-disposition")}
    out["Cache-Control"] = "no-store"
    return Response(r.content, status_code=r.status_code, headers=out)
