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
Hermes's own ``_ws_auth_ok`` and refuses when it cannot (fail closed): before the
handshake (a 403, the same for every refusal) when the credential fails, and with a
close code and a reason once it passed. It never returns without one or the other.

Websockets reach this router only when it runs in the dashboard's own process. A
profile's plugin host (``plugins.isolation: host``, and every profile but the one the
dashboard was launched as) forwards plain requests only and refuses a socket itself,
with an HTTP 501 that uvicorn logs as "ASGI callable returned without completing
handshake". So ``/agents`` and ``/_hermes_profile`` say whether sockets are carried
(``sockets``), and the tab does not dial one where they are not: it polls.

One route is answered here, not by the console: ``/_hermes_stats``, the Stats view's Hermes
side — what the agent's Emporia conversations cost in model tokens, read read-only through
Hermes's documented ``hermes_state.SessionDB``, and the turn cap's count from this plugin's
own data. Aggregates only; the console's interpreter is not Hermes's, so it cannot read them.

Nothing is logged except the console's start; request and response bodies —
which include decrypted DMs — never are. Hermes's own credentials (cookie,
session token, Authorization, ticket) are never forwarded to the console.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import tempfile
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


#: A Unix socket's path must fit `sun_path`: 108 bytes with its NUL on Linux, 104 on macOS.
#: Stay under both, so a deep HERMES_HOME never starts a console that dies at once.
SUN_PATH_MAX = 100


def _sockets_carried() -> bool:
    """Whether Hermes carries a websocket to this router: only in the dashboard's own process.
    In a profile's plugin host (Hermes sets HERMES_PLUGIN_HOST_PROCESS there) it forwards plain
    requests only, and answers every socket with a 501 before it reaches this router."""
    return os.environ.get("HERMES_PLUGIN_HOST_PROCESS") != "1"


def _private_dir(path: Path) -> bool:
    """Make `path` a 0700 folder of this user, or say it cannot be one (someone else's, a link)."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(path, 0o700)
        return True
    except OSError:
        return False


def _short_socket(home: Path) -> Path:
    """A short socket path for this profile's console, for when the configured one is too long:
    `$XDG_RUNTIME_DIR/emporia-console/<hash>/console.sock`, else
    `<tmp>/emporia-console-<uid>/<hash>/console.sock`, each folder 0700 and this user's own.
    The hash names the profile home, so each profile keeps a console of its own."""
    tag = hashlib.sha256(os.fsencode(str(home))).hexdigest()[:16]
    roots = []
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and os.path.isabs(xdg):
        roots.append(Path(xdg) / "emporia-console")
    roots.append(Path(tempfile.gettempdir()) / f"emporia-console-{os.getuid()}")
    for root in roots:
        sock = root / tag / "console.sock"
        if len(os.fsencode(str(sock))) <= SUN_PATH_MAX and _private_dir(root):
            return sock
    raise RuntimeError("no private folder with a short enough path for the Emporia console's socket")


def _socket() -> Path:
    """This profile's console socket: the configured folder (`runtime_dir`, else plugin-data),
    or, when that path is past the AF_UNIX limit, a short private one (`_short_socket`)."""
    runtime = _config().get("runtime_dir")
    if not runtime:
        from plugins.plugin_storage import plugin_data_dir
        runtime = plugin_data_dir("emporia") / "console"
    sock = Path(runtime) / "console.sock"
    if len(os.fsencode(str(sock))) <= SUN_PATH_MAX:      # the path as bound and dialled
        return sock
    return _short_socket(_home())


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
        if not _private_dir(runtime):
            raise RuntimeError(f"the console's folder {runtime} is not this user's own 0700 folder")
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


async def _close(socket: WebSocket, code: int, reason: str = "") -> None:
    """End a socket with a close code, before or after the handshake. Never leave it untouched:
    uvicorn logs a socket the route returns from without either as an incomplete handshake."""
    from starlette.websockets import WebSocketState
    try:
        if socket.application_state == WebSocketState.CONNECTED:
            await socket.close(code=code, reason=reason[:120])
        elif socket.application_state == WebSocketState.CONNECTING:
            await socket.close(code=code)            # before accept: a 403 handshake answer
    except RuntimeError:
        pass                                         # the client went first


@router.websocket("/ws/{path:path}")
async def ws(socket: WebSocket, path: str):
    try:
        from hermes_cli import web_server_chat
        authorized = bool(web_server_chat._ws_auth_ok(socket))
    except Exception:
        authorized = False                                    # fail closed
    if not authorized:
        # Before accept: the same 403 for every refusal, so it says nothing about the plugin.
        await _close(socket, 1008)
        return
    try:
        sock, secret = await _ensure_console()
    except Exception as e:                                    # not set up, no console, no folder
        # The credential passed: accept, then say why, with a close code the tab can read.
        await socket.accept()
        await _close(socket, 1011, f"the Emporia console is not running: {e}")
        return
    query = urlencode([(k, v) for k, v in socket.query_params.multi_items()
                       if k not in _WS_AUTH_PARAMS])
    uri = f"ws://console/ws/{path}" + (f"?{query}" if query else "")
    await socket.accept()
    code, reason = 1000, ""
    try:
        import websockets
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
            # The console's own close code and reason, passed on (a refused room, a finished game).
            if upstream.close_code is not None:
                code, reason = upstream.close_code, upstream.close_reason or ""
    except Exception:
        code, reason = 1011, "the console's socket failed"
    finally:
        if code in (1005, 1006, 1015):                        # codes no endpoint may send
            code = 1000
        await _close(socket, code, reason)


@router.get("/_hermes_profile")
async def hermes_profile(request: Request):
    """P227: the Hermes profile this tab is open in — Hermes adds ?profile= to
    a request from any profile but the one the dashboard was launched as — so
    the tab views as that profile's agent rather than the first one listed."""
    name = request.query_params.get("profile", "").strip()
    if not name:
        home = Path(os.getenv("HERMES_HOME", "")).expanduser()
        name = home.name if home.parent.name == "profiles" else ""
    return JSONResponse({"profile": name, "sockets": _sockets_carried()}, headers={"Cache-Control": "no-store"})


#: The bridge's conversation ids: `emporia:<agent>`, or `emporia:<agent>:<kind>:<id>` (adapter.chat_for).
_CONVERSATION_KINDS = ("session", "dm", "room", "agora")
_AGENT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]")     # the relay's own rule
_PAGE = 200
_MAX_SESSIONS = 5000
_DAY = 86400


def _model_cost(agent: str, days: int, now: float) -> dict:
    """What `agent`'s Emporia conversations cost in model tokens over `days`, from this
    profile's Hermes session records — read through Hermes's own `hermes_state.SessionDB`
    (read-only), a module Hermes documents for plugins. Aggregates only: no title, preview or
    message leaves this function. The same figure as `my_stats` / `emporia stats NAME`."""
    out: dict = {"available": False, "usd": 0.0, "tokens": 0, "sessions": 0, "by_kind": {},
                 "days": days, "source": "hermes"}
    try:
        from hermes_state import SessionDB
    except ImportError:
        out["reason"] = "Hermes's session records are not readable here"
        return out
    since = now - days * _DAY
    prefix = f"emporia:{agent}"
    by_kind: dict[str, dict] = {}
    try:
        db = SessionDB(read_only=True)
    except Exception:
        out["reason"] = "this profile has no Hermes session records yet"
        return out
    try:
        offset, done = 0, False
        while not done and offset < _MAX_SESSIONS:
            rows = db.list_sessions_rich(source="emporia", limit=_PAGE, offset=offset, include_children=True,
                                         include_archived=True, include_hidden=True,
                                         project_compression_tips=False, compact_rows=True)
            for row in rows:
                started = float(row.get("started_at") or 0)
                if started < since:            # newest first: the rest are older
                    done = True
                    break
                chat = str(row.get("chat_id") or "")
                if chat != prefix and not chat.startswith(prefix + ":"):
                    continue
                rest = chat[len(prefix):].lstrip(":")
                head = rest.split(":", 1)[0]
                kind = head if head in _CONVERSATION_KINDS else ("main" if not rest else "other")
                cost = row.get("actual_cost_usd")
                usd = float(cost if cost is not None else row.get("estimated_cost_usd") or 0)
                k = by_kind.setdefault(kind, {"usd": 0.0, "sessions": 0})
                k["usd"] += usd
                k["sessions"] += 1
                out["usd"] += usd
                out["tokens"] += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
                out["sessions"] += 1
            if len(rows) < _PAGE:
                done = True
            offset += _PAGE
    except Exception:
        out.update({"usd": 0.0, "tokens": 0, "sessions": 0,
                    "reason": "Hermes's session records could not be read"})
        return out
    finally:
        db.close()
    out["by_kind"] = {k: {"usd": round(v["usd"], 4), "sessions": v["sessions"]} for k, v in by_kind.items()}
    out["usd"] = round(out["usd"], 4)
    out["available"] = True
    return out


def _turns_used(now: float) -> int | None:
    """Relay-started turns in the last 24 hours: the turn cap's own counter, in this plugin's
    data (`turn_cap.py`). None when it cannot be read here."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        data = json.loads((plugin_data_dir("emporia") / "turns.json").read_text())
    except FileNotFoundError:
        return 0
    except (ImportError, OSError, ValueError):
        return None
    turns = data.get("turns") if isinstance(data, dict) else None
    return sum(1 for t in (turns or []) if isinstance(t, (int, float)) and now - t < _DAY)


@router.get("/_hermes_stats")
async def hermes_stats(request: Request):
    """Stats view: the Hermes side of an agent's figures — model cost per Emporia
    conversation and relay-started turns — which the relay cannot know. Read-only,
    aggregates only; the relay side the tab reads from the relay itself."""
    import time
    agent = request.query_params.get("agent", "").strip()
    if not _AGENT_ID.fullmatch(agent):
        return JSONResponse({"detail": "agent is required"}, status_code=400, headers={"Cache-Control": "no-store"})
    try:
        days = max(1, min(int(request.query_params.get("days", "30")), 365))
    except ValueError:
        days = 30
    now = time.time()
    model = await asyncio.to_thread(_model_cost, agent, days, now)
    return JSONResponse({"agent_id": agent, "model": model, "turns_24h": _turns_used(now)},
                        headers={"Cache-Control": "no-store"})


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
    return JSONResponse({"agents": own + others, "sockets": _sockets_carried()},
                        headers={"Cache-Control": "no-store"})


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
