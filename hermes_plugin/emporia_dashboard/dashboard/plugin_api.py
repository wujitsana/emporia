"""Emporia tab of the Hermes dashboard: a proxy to the Emporia console (P126).

Imported by the Hermes dashboard process, under Hermes's interpreter, and
mounted at /api/plugins/emporia/. It imports nothing from Emporia: the work —
agent keys, signing in to the relay, opening DMs — happens in the Emporia
console, a separate process in Emporia's own venv, reached over a Unix socket
in a 0700 directory. No TCP port. See docs/local-console.md.

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
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, Response

router = APIRouter()
log = logging.getLogger("emporia.dashboard_plugin")

_HERE = Path(__file__).resolve().parent
_CONFIG_FILE = _HERE / "emporia.json"        # written by Emporia's installer
_CONSOLE_START_S = 25.0
_DROP_HEADERS = {"host", "cookie", "authorization", "connection", "content-length",
                 "accept-encoding", "transfer-encoding", "upgrade", "te", "keep-alive"}
_WS_AUTH_PARAMS = {"ticket", "token", "internal", "profile"}

_proc: subprocess.Popen | None = None
_start_lock = asyncio.Lock()


@atexit.register
def _stop_console() -> None:
    """The console lives as long as the dashboard that started it."""
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()


def _config() -> dict:
    try:
        return json.loads(_CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _socket() -> Path:
    return Path(_config().get("runtime_dir", str(_HERE / ".runtime"))) / "console.sock"


async def _console_up(sock: Path) -> bool:
    if not sock.exists():
        return False
    try:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sock)),
                                     timeout=3) as c:
            return (await c.get("http://console/agents")).status_code == 200
    except httpx.HTTPError:
        return False


async def _ensure_console() -> Path:
    """The console's socket, starting the console first if it is not running."""
    global _proc
    sock = _socket()
    if await _console_up(sock):
        return sock
    async with _start_lock:
        if await _console_up(sock):
            return sock
        cfg = _config()
        if not cfg.get("python"):
            raise RuntimeError("emporia.json is missing — re-run Emporia's installer")
        if _proc is not None and _proc.poll() is None:
            _proc.terminate()
        runtime = sock.parent
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        argv = [cfg["python"], "-m", "emporia.console", "--uds", str(sock),
                "--relay-url", cfg.get("relay_url", "http://localhost:8088")]
        if cfg.get("profiles_root"):
            argv += ["--profiles-root", cfg["profiles_root"]]
        for d in cfg.get("keys_dirs", []):
            argv += ["--keys-dir", d]
        # Only what the console needs: no Hermes secrets in its environment, and
        # no .env merged into it either (it is told everything it needs).
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE")}
        env["EMPORIA_NO_DOTENV"] = "1"
        log_file = open(runtime / "console.log", "ab")
        _proc = subprocess.Popen(argv, env=env, stdout=log_file, stderr=log_file,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        log.info("Emporia: started the console (pid %s)", _proc.pid)
        deadline = asyncio.get_running_loop().time() + _CONSOLE_START_S
        while asyncio.get_running_loop().time() < deadline:
            if await _console_up(sock):
                return sock
            if _proc.poll() is not None:
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
        sock = await _ensure_console()
    except RuntimeError:
        await socket.close(code=1011)
        return
    query = urlencode([(k, v) for k, v in socket.query_params.multi_items()
                       if k not in _WS_AUTH_PARAMS])
    uri = f"ws://console/ws/{path}" + (f"?{query}" if query else "")
    await socket.accept()
    try:
        async with websockets.unix_connect(str(sock), uri=uri) as upstream:
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


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    try:
        sock = await _ensure_console()
    except RuntimeError as e:
        return JSONResponse({"detail": str(e)}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _DROP_HEADERS and not k.lower().startswith("x-hermes")}
    query = urlencode([(k, v) for k, v in request.query_params.multi_items()
                       if k != "profile"])
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sock)),
                                 timeout=180) as c:
        r = await c.request(request.method, f"http://console/{path}" + (f"?{query}" if query else ""),
                            headers=headers, content=await request.body())
    out = {k: v for k, v in r.headers.items()
           if k.lower() in ("content-type", "content-disposition")}
    out["Cache-Control"] = "no-store"
    return Response(r.content, status_code=r.status_code, headers=out)
