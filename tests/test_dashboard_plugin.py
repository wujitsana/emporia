"""P126: the Emporia tab's backend, run the way Hermes runs it — under Hermes's
interpreter, in front of a console on a Unix socket."""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_PYTHON = Path("/opt/hermes/.venv/bin/python")
PLUGIN = REPO / "hermes_plugin" / "emporia" / "dashboard"


@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="Hermes is not installed here")
def test_the_plugin_forwards_to_the_console_and_never_its_hermes_credentials(tmp_path):
    runtime = tmp_path / "rt"
    plugin = tmp_path / "plugin"
    subprocess.run(["cp", "-r", str(PLUGIN), str(plugin)], check=True)
    # The "console" the plugin starts: a stand-in that, like the real one, reads its per-start
    # secret on stdin and answers only requests carrying it (console scope).
    stub = tmp_path / "stub_console.py"
    stub.write_text('''
import sys
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
uds = sys.argv[sys.argv.index("--uds") + 1]
secret = sys.stdin.readline().strip()
app = FastAPI()
@app.middleware("http")
async def gate(request: Request, call_next):
    if request.headers.get("x-emporia-console-secret") != secret:
        return JSONResponse({"detail": "no secret"}, status_code=401)
    return await call_next(request)
@app.get("/agents")
async def agents(): return {"agents": ["alpha"]}
@app.api_route("/echo/{rest:path}", methods=["GET", "POST"])
async def echo(rest: str, request: Request):
    return {"path": rest, "query": request.url.query, "headers": dict(request.headers),
            "body": (await request.body()).decode(), "argv": sys.argv}
from fastapi import WebSocket
@app.websocket("/ws/{rest:path}")
async def ws(socket: WebSocket, rest: str):
    if socket.headers.get("x-emporia-console-secret") != secret:
        await socket.close(code=1008)
        return
    await socket.accept()
    await socket.send_text("as the console: " + rest)
uvicorn.run(app, uds=uds, log_level="error")
''')
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nexec {HERMES_PYTHON} {stub} "$@"\n')
    python.chmod(0o755)
    data = tmp_path / "home" / "plugin-data" / "emporia"     # P295: settings in plugin-data, not the plugin
    data.mkdir(parents=True)
    # A relative runtime folder (the harness runs from tmp_path): a Unix socket's path has a
    # length limit that a long temporary directory exceeds.
    (data / "emporia.json").write_text(json.dumps(
        {"python": str(python), "relay_url": "http://relay.test", "runtime_dir": "rt"}))
    harness = tmp_path / "harness.py"
    harness.write_text(f'''
import importlib.util, json, os
from fastapi import FastAPI
from fastapi.testclient import TestClient
os.chdir({str(tmp_path)!r})
spec = importlib.util.spec_from_file_location("p", {str(plugin / "plugin_api.py")!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
app = FastAPI(); app.include_router(m.router, prefix="/api/plugins/emporia")
c = TestClient(app)
r = c.post("/api/plugins/emporia/echo/relay/alpha/sessions?status=active&profile=beta",
           headers={{"cookie": "hermes_session=SECRET", "X-Hermes-Session-Token": "SECRET",
                    "Authorization": "Bearer SECRET", "content-type": "application/json",
                    "x-emporia-console-secret": "forged"}},
           content=b'{{"x": 1}}')
refused = False
try:
    with c.websocket_connect("/api/plugins/emporia/ws/alpha/ws/agent/alpha"):
        pass
except Exception:
    refused = True
import sys
private = [k for k in sys.modules if k.startswith("hermes_cli")]
t = c.post("/api/plugins/emporia/_ws_ticket").json()["ticket"]     # the console still needs its secret
with c.websocket_connect("/api/plugins/emporia/ws/alpha/ws/agent/alpha",
                         subprotocols=["emporia.v1", "emporia.ticket." + t]) as s:
    relayed = s.receive_text()
here = c.get("/api/plugins/emporia/_hermes_profile?profile=beta").json()
print(json.dumps({{"status": r.status_code, "echo": r.json(), "here": here, "relayed": relayed,
                  "cache": r.headers.get("cache-control"), "ws_refused": refused, "private": private}}))
m._stop_console()
''')
    out = subprocess.run([str(HERMES_PYTHON), str(harness)], cwd="/opt/hermes",
                         env={**os.environ, "HERMES_HOME": str(tmp_path / "home"),
                              "PYTHONPATH": "/opt/hermes"},
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["status"] == 200 and result["cache"] == "no-store"
    echo = result["echo"]
    assert echo["path"] == "relay/alpha/sessions" and echo["body"] == '{"x": 1}'
    assert echo["query"] == "status=active", "Hermes's ?profile= is not forwarded"
    sent = {k.lower() for k in echo["headers"]}
    assert not sent & {"cookie", "authorization", "x-hermes-session-token"}, sent
    assert "SECRET" not in json.dumps(echo)
    assert echo["headers"]["x-emporia-console-secret"] != "forged", "the plugin's secret, not the browser's"
    argv = echo["argv"]
    assert argv[argv.index("--profile") + 1] == str(tmp_path / "home") and "--hermes-root" not in argv
    assert result["ws_refused"], "a websocket without a ticket is refused"
    assert not result["private"], "no hermes_cli module is imported"
    assert result["relayed"] == "as the console: alpha/ws/agent/alpha", "the websocket carries the secret"
    assert oct(runtime.stat().st_mode)[-3:] == "700"
    assert result["here"] == {"profile": "beta", "sockets": True}, "P227: the tab learns which profile it is open in"



@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="needs a Hermes install at /opt/hermes")
def test_the_tab_in_a_profile_without_an_agent_says_to_run_setup(tmp_path):
    """P296: no settings in plugin-data → a 503 the tab turns into `hermes emporia setup`."""
    harness = tmp_path / "harness.py"
    harness.write_text(f'''
import importlib.util, json
from fastapi import FastAPI
from fastapi.testclient import TestClient
spec = importlib.util.spec_from_file_location("p", {str(PLUGIN / "plugin_api.py")!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
app = FastAPI(); app.include_router(m.router, prefix="/api/plugins/emporia")
r = TestClient(app).get("/api/plugins/emporia/agents")
print(json.dumps({{"status": r.status_code, "body": r.json()}}))
''')
    out = subprocess.run([str(HERMES_PYTHON), str(harness)], cwd="/opt/hermes",
                         env={**os.environ, "HERMES_HOME": str(tmp_path / "home"), "PYTHONPATH": "/opt/hermes"},
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["status"] == 503 and result["body"]["setup_needed"] is True
    assert "hermes emporia setup" in result["body"]["detail"]


@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="Hermes is not installed here")
def test_each_profile_gets_its_own_console_with_its_own_secret(tmp_path):
    """Console scope (the 11-05 audit's proposal): the plugin ran one console for every profile
    under the Hermes root, reached with no credential. It now starts one per Hermes profile —
    the one Hermes scoped the request to (`?profile=`, as the dashboard's profile switcher
    sends it) — told to serve that profile alone (`--profile`), and gives each start a secret
    on stdin (never in its arguments or environment) that it presents on every request.
    Switching profile reaches that profile's console; switching back reuses the first."""
    root = tmp_path / "root"
    for p in ("alpha", "beta"):
        home = root / "profiles" / p
        (home / "plugin-data" / "emporia").mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
        (home / "plugin-data" / "emporia" / "emporia.json").write_text(json.dumps(
            {"python": "/fake/python", "relay_url": "http://relay.test", "runtime_dir": str(home / "rt"),
             "hermes_root": str(root)}))
    (root / "profiles" / "gamma").mkdir()
    (root / "profiles" / "gamma" / "config.yaml").write_text("{}\n")      # a profile, not an agent
    harness = tmp_path / "harness.py"
    harness.write_text(f'''
import importlib.util, json
from pathlib import Path
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

root = Path({str(root)!r})
spec = importlib.util.spec_from_file_location("p", {str(PLUGIN / "plugin_api.py")!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

started, consoles = [], {{}}
class Stdin:
    def __init__(self, c): self.c = c
    def write(self, data): self.c["secret"] += data.decode() if isinstance(data, bytes) else data
    def flush(self): pass
    def close(self): self.c["closed"] = True
class FakePopen:
    def __init__(self, argv, env=None, stdin=None, **kw):
        sock = argv[argv.index("--uds") + 1]
        self.c = consoles[sock] = {{"argv": argv, "env": env, "secret": "", "closed": False}}
        Path(sock).parent.mkdir(parents=True, exist_ok=True); Path(sock).touch()
        self.stdin, self.pid = Stdin(self.c), 1000 + len(started)
        started.append(self.c)
    def poll(self): return None
    def terminate(self): pass
m.subprocess.Popen = FakePopen

def console_app(sock):
    app = FastAPI()
    @app.api_route("/{{path:path}}", methods=["GET", "POST"])
    async def any_(path: str, request: Request):
        c = consoles[sock]
        if request.headers.get("x-emporia-console-secret") != c["secret"].strip():
            return JSONResponse({{"detail": "no secret"}}, status_code=401)
        prof = c["argv"][c["argv"].index("--profile") + 1]
        return {{"agents": [Path(prof).name]}}
    return app
m._transport = lambda sock: httpx.ASGITransport(app=console_app(str(sock)))

app = FastAPI()
@app.middleware("http")
async def hermes_scope(request: Request, call_next):     # what Hermes does for ?profile=
    name = request.query_params.get("profile") or "alpha"
    token = set_hermes_home_override(str(root / "profiles" / name))
    try:
        return await call_next(request)
    finally:
        reset_hermes_home_override(token)
app.include_router(m.router, prefix="/api/plugins/emporia")
c = TestClient(app)
seen = [c.get("/api/plugins/emporia/relay/x/health?profile=" + p,
              headers={{"x-emporia-console-secret": "forged"}}).json() for p in ("alpha", "beta", "alpha")]
picker = [c.get("/api/plugins/emporia/agents?profile=" + p).json() for p in ("alpha", "beta")]
print(json.dumps({{"seen": seen, "started": started, "picker": picker}}))
''')
    out = subprocess.run([str(HERMES_PYTHON), str(harness)], cwd="/opt/hermes",
                         env={**os.environ, "HERMES_HOME": str(root / "profiles" / "alpha"),
                              "PYTHONPATH": "/opt/hermes"},
                         capture_output=True, text=True, timeout=90)
    assert out.returncode == 0, out.stderr[-2000:]
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["seen"] == [{"agents": ["alpha"]}, {"agents": ["beta"]}, {"agents": ["alpha"]}], r["seen"]
    assert len(r["started"]) == 2, "one console per profile, reused when switching back"
    # The tab's profile picker (P234) still offers every agent profile, this one first: names
    # only, from the profiles set up for Emporia (gamma is not) — no other console is asked.
    assert r["picker"] == [{"agents": ["alpha", "beta"], "sockets": True},
                           {"agents": ["beta", "alpha"], "sockets": True}], r["picker"]
    secrets = []
    for name, started in zip(("alpha", "beta"), r["started"]):
        argv, secret = started["argv"], started["secret"].strip()
        assert argv[argv.index("--profile") + 1] == str(root / "profiles" / name)
        assert "--hermes-root" not in argv and "--keys-dir" not in argv
        assert len(secret) >= 32 and started["closed"]
        assert secret not in json.dumps(argv) and secret not in json.dumps(started["env"])
        secrets.append(secret)
    assert secrets[0] != secrets[1]


DASH = REPO / "dashboard"
DESKTOP = REPO / "hermes_plugin" / "emporia" / "desktop"


def _inputs(config: str) -> list[str]:
    """The inputs a bundle is built from: buildMeta.ts SHARED_INPUTS plus the bundle's own config."""
    import re
    shared = re.search(r"SHARED_INPUTS = \[([^\]]*)\]", (DASH / "buildMeta.ts").read_text())
    assert shared, "buildMeta.ts names the shared inputs"
    return re.findall(r'"([^"]+)"', shared.group(1)) + [config]


def _source_digest(config: str = "vite.plugin.config.ts") -> str:
    """dashboard/buildMeta.ts sourceDigest(), in Python."""
    import hashlib
    files = []
    for rel in _inputs(config):
        p = DASH / rel
        found = sorted(p.rglob("*")) if p.is_dir() else [p]
        files += [f.relative_to(DASH).as_posix() for f in found
                  if f.is_file() and not f.name.endswith((".test.ts", ".test.tsx"))]
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.encode() + b"\n")
        h.update((DASH / f).read_bytes())
    return h.hexdigest()


REBUILD = "run `npm run build:plugin` in dashboard/ (it builds the tab and the Desktop page together)"


def test_the_committed_tab_bundle_is_built_from_the_dashboard_as_it_is():
    """P295: a catalog install is the plugin folder alone, so the tab's bundle is
    committed there. Changing dashboard/ without rebuilding would ship the old tab."""
    dist = PLUGIN / "dist"
    assert (dist / "index.js").is_file() and (dist / "style.css").is_file()
    assert (dist / "SOURCE").read_text().strip() == _source_digest(), f"the tab's bundle is older than dashboard/: {REBUILD}"


def test_the_committed_desktop_page_is_built_from_the_dashboard_as_it_is():
    """The Desktop half is built from the same dashboard/src: forgetting to rebuild
    it (or the tab) fails here, so the two never drift apart."""
    assert (DESKTOP / "plugin.js").is_file()
    assert (DESKTOP / "SOURCE").read_text().strip() == _source_digest("vite.desktop.config.ts"), (
        f"the Desktop page is older than dashboard/: {REBUILD}")


def _plugin_version() -> str:
    import re
    m = re.search(r"^version:\s*(\S+)\s*$", (PLUGIN.parent / "plugin.yaml").read_text(), re.M)
    assert m
    return m.group(1).strip("\"'")


def test_one_version_for_the_tab_the_desktop_page_and_the_plugin():
    """plugin.yaml is the one place the version is written; both bundles carry it."""
    version = _plugin_version()
    assert json.loads((PLUGIN / "manifest.json").read_text())["version"] == version
    for bundle in (PLUGIN / "dist" / "index.js", DESKTOP / "plugin.js"):
        text = bundle.read_text()
        assert f'EMPORIA_VERSION = "{version}";' in text, bundle


def test_the_tab_manifest_describes_the_dashboard_and_uses_an_icon_hermes_knows():
    manifest = json.loads((PLUGIN / "manifest.json").read_text())
    assert "read-only" in manifest["description"] and "Arena" in manifest["description"]
    assert "payments" not in manifest["description"]
    # Hermes's tab icons (web/src/App.tsx ICON_MAP); any other name falls back to the generic puzzle piece.
    assert manifest["icon"] in {"Activity", "BarChart3", "Clock", "Cpu", "FileText", "FolderOpen", "KeyRound",
                                "MessageSquare", "Package", "Settings", "Sparkles", "Terminal", "Globe", "Database",
                                "Shield", "Users", "Wrench", "Zap", "Heart", "Star", "Code", "Eye"}
    # The page puts style.css inside its own closed shadow root (so the host's styles and ours never meet); declaring
    # "css" would also inject it into the host document, a second copy that restyles the dashboard.
    assert "css" not in manifest
    assert 'new URL("style.css", BASE)' in (PLUGIN / "dist" / "index.js").read_text()


def test_the_desktop_page_imports_only_the_host_modules_and_no_react_dom():
    """The Desktop loader refuses any import but the SDK and the host's React; react-dom's
    internals read as script injection to the catalog lint (rule 8)."""
    import re
    js = (DESKTOP / "plugin.js").read_text()
    specs = set(re.findall(r'^import [^;]*?from "([^"]+)";$', js, re.M))
    assert specs == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}, specs
    assert "react-dom" not in js and "createRoot" not in js
    assert "__EMPORIA_DESKTOP_CSS__" not in js, "the stylesheet is inlined"
    assert re.search(r"^export \{.*plugin as default.*\};$", js, re.M)


@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="Hermes is not installed here")
def test_hermes_own_desktop_surface_lint_finds_nothing_in_the_page():
    """Catalog rule 8, checked with the lint `hermes plugins validate` runs
    (hermes_cli/plugin_validate_desktop.py): no refusals, and no review cues."""
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from hermes_cli.plugin_validate_desktop import desktop_surface_hits, desktop_review_findings\n"
        "d = Path(sys.argv[1])\n"
        "print(json.dumps({'hits': desktop_surface_hits(d),"
        " 'cues': desktop_surface_hits(d, desktop_review_findings)}))\n"
    )
    out = subprocess.run([str(HERMES_PYTHON), "-c", code, str(PLUGIN.parent)], cwd="/opt/hermes",
                         env={**os.environ, "PYTHONPATH": "/opt/hermes"}, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"hits": [], "cues": []}, result


def _plugin_module():
    """plugin_api.py imported as Hermes imports it (it imports nothing from Emporia)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("emporia_plugin_api_under_test", PLUGIN / "plugin_api.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_deep_hermes_home_gets_a_short_private_console_socket(tmp_path, monkeypatch):
    """A socket path past AF_UNIX's limit (about 107 bytes) killed the console at start and the
    tab answered 503; a deep HERMES_HOME reached it. Past the limit, the socket moves to a short
    0700 folder of this user's, one per profile home; a short configured path is kept."""
    import shutil
    import stat as st
    import tempfile
    m = _plugin_module()
    deep = tmp_path / ("d" * 60) / "profiles" / "player"
    monkeypatch.setattr(m, "_home", lambda: deep)
    monkeypatch.setattr(m, "_config", lambda: {"runtime_dir": str(deep / "plugin-data" / "emporia" / "console")})
    short_root = Path(tempfile.mkdtemp(prefix="x"))          # the XDG runtime folder stand-in
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_root))
        sock = m._socket()
        assert len(os.fsencode(str(sock))) <= m.SUN_PATH_MAX
        assert sock.name == "console.sock" and sock.parent.parent == short_root / "emporia-console"
        root = sock.parent.parent
        assert st.S_IMODE(root.stat().st_mode) == 0o700 and root.stat().st_uid == os.getuid()
        # Another profile: another folder. The same profile: the same one.
        monkeypatch.setattr(m, "_home", lambda: deep.parent / "merchant")
        assert m._socket().parent != sock.parent
        monkeypatch.setattr(m, "_home", lambda: deep)
        assert m._socket() == sock
        # A folder that is a link (or someone else's) is never used.
        root.rmdir()
        root.symlink_to(tmp_path)
        monkeypatch.setattr(m.tempfile, "gettempdir", lambda: str(short_root / ("t" * 90)))
        with pytest.raises(RuntimeError, match="short enough"):
            m._socket()
        root.unlink()
        # A configured path that fits is kept as it is.
        monkeypatch.setattr(m, "_config", lambda: {"runtime_dir": "rt"})
        assert m._socket() == Path("rt") / "console.sock"
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


def test_the_tab_learns_whether_hermes_carries_its_sockets(monkeypatch):
    """Hermes runs a profile's plugin in that profile's plugin host when it is not the profile the
    dashboard was launched as; the host carries no websockets and Hermes answers each with a 501,
    which uvicorn logs as an incomplete handshake. The tab is told, and does not dial."""
    m = _plugin_module()
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    assert m._sockets_carried() is True
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert m._sockets_carried() is False


def test_a_refused_socket_closes_cleanly_never_with_an_incomplete_handshake(monkeypatch):
    """The user's terminal showed `ASGI callable returned without completing handshake`. Run the
    plugin's socket route in uvicorn with the protocol Hermes's uvicorn picks (websockets-sansio):
    a socket without Hermes's credential gets a 403 before accept; one with it, whose console
    cannot start, is accepted and closed with 1011 and a reason. Neither logs that error, while
    an HTTP denial answer (what Hermes sends for a plugin host's socket) does, so the check bites."""
    import asyncio
    import logging
    import time
    from pathlib import Path

    import uvicorn
    import websockets
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import JSONResponse

    m = _plugin_module()
    monkeypatch.setattr(m, "_config", lambda: {})                 # no settings: not set up here
    monkeypatch.setattr(m, "_ws_tickets", {})

    app = FastAPI()
    app.include_router(m.router, prefix="/api/plugins/emporia")

    @app.websocket("/denied")
    async def denied(socket: WebSocket):
        await socket.send_denial_response(JSONResponse({"detail": "no"}, status_code=501))

    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()

    async def run():
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, ws="websockets-sansio",
                                            log_level="info", lifespan="off"))
        logging.getLogger("uvicorn.error").addHandler(grab)     # after Config: it resets logging
        task = asyncio.create_task(srv.serve())
        while not srv.started:
            await asyncio.sleep(0.02)
        port = srv.servers[0].sockets[0].getsockname()[1]
        base = f"ws://127.0.0.1:{port}"
        out = {}
        try:
            async with websockets.connect(f"{base}/api/plugins/emporia/ws/alpha/ws/events"):
                out["unauth"] = "accepted"
        except websockets.InvalidStatus as e:
            out["unauth"] = e.response.status_code
        await asyncio.sleep(0.1)
        out["unauth_logs"] = list(records)
        url = f"{base}/api/plugins/emporia/ws/alpha/ws/events"

        def offer(t):
            return [websockets.Subprotocol("emporia.v1"), websockets.Subprotocol(f"emporia.ticket.{t}")]

        async def refused(**kw):
            try:
                async with websockets.connect(url, **kw):
                    return "accepted"
            except websockets.InvalidStatus as e:
                return e.response.status_code

        out["no_ticket"] = await refused(subprotocols=[websockets.Subprotocol("emporia.v1")])
        out["forged"] = await refused(subprotocols=offer("nope"))
        t = m._mint_ws_ticket(m._home())
        out["bad_origin"] = await refused(subprotocols=offer(t), origin="http://evil.example")
        t = m._mint_ws_ticket(m._home())
        out["foreign_profile"] = await refused(subprotocols=offer(m._mint_ws_ticket(Path("/elsewhere"))))
        m._ws_tickets[t] = (time.monotonic() - 1, str(m._home()))
        out["expired"] = await refused(subprotocols=offer(t))
        t = m._mint_ws_ticket(m._home())
        async with websockets.connect(url, subprotocols=offer(t), origin=f"http://127.0.0.1:{port}") as s:
            out["subprotocol"] = s.subprotocol
            try:
                await s.recv()
            except websockets.ConnectionClosed as e:
                out["auth"] = (e.rcvd.code, e.rcvd.reason)
        await asyncio.sleep(0.1)
        out["auth_logs"] = list(records)
        out["replayed"] = await refused(subprotocols=offer(t))
        try:
            async with websockets.connect(f"{base}/denied"):
                pass
        except websockets.InvalidStatus as e:
            out["denied"] = e.response.status_code
        except websockets.InvalidMessage:       # some uvicorn versions garble the denial answer
            out["denied"] = "refused"
        await asyncio.sleep(0.1)
        out["denied_logs"] = list(records)
        srv.should_exit = True
        await task
        return out

    try:
        out = asyncio.run(run())
    finally:
        logging.getLogger("uvicorn.error").removeHandler(grab)
    bad = "ASGI callable returned without completing handshake."
    assert out["unauth"] == 403
    for k in ("no_ticket", "forged", "bad_origin", "foreign_profile", "expired", "replayed"):
        assert out[k] == 403, (k, out[k])
    assert out["subprotocol"] == "emporia.v1", "the ticket-bearing protocol is never echoed"
    assert out["auth"][0] == 1011 and "not running" in out["auth"][1], out["auth"]
    assert bad not in out["unauth_logs"] and bad not in out["auth_logs"], out["auth_logs"]
    assert out["denied"] in (501, "refused") and bad in out["denied_logs"], "the check sees the error when it happens"


def test_the_stats_route_sums_one_agents_emporia_conversations_and_nothing_else(monkeypatch, tmp_path):
    """Stats view: model cost per Emporia conversation through Hermes's documented
    `hermes_state.SessionDB` (read-only; catalog rule 9: no private import). Only this agent's
    `emporia:<agent>[:<kind>:<id>]` conversations in the period count; actual cost wins over the
    estimate; no title or preview reaches the answer; and the turn cap's own counter is read."""
    import sys
    import time
    import types

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    now = time.time()
    rows = [  # newest first, as list_sessions_rich orders them
        {"chat_id": "emporia:alpha", "started_at": now - 60, "estimated_cost_usd": 0.10,
         "actual_cost_usd": None, "input_tokens": 100, "output_tokens": 10, "title": "secret", "preview": "secret"},
        {"chat_id": "emporia:alpha:dm:beta", "started_at": now - 120, "estimated_cost_usd": 0.05,
         "actual_cost_usd": 0.04, "input_tokens": 50, "output_tokens": 5},
        {"chat_id": "emporia:alphabet:room:r1", "started_at": now - 130, "estimated_cost_usd": 3.0},  # another agent
        {"chat_id": "emporia:alpha:session:s1", "started_at": now - 3 * 86400, "estimated_cost_usd": 0.02,
         "input_tokens": 20, "output_tokens": 2},
        {"chat_id": "emporia:alpha:room:old", "started_at": now - 40 * 86400, "estimated_cost_usd": 9.0},
    ]

    class FakeDB:
        def __init__(self, read_only=False):
            assert read_only is True, "the stats route never opens Hermes's records for writing"

        def list_sessions_rich(self, **kw):
            assert kw["source"] == "emporia"
            return rows[kw["offset"]:kw["offset"] + kw["limit"]]

        def close(self):
            pass

    fake = types.ModuleType("hermes_state")
    fake.SessionDB = FakeDB
    monkeypatch.setitem(sys.modules, "hermes_state", fake)
    storage = types.ModuleType("plugins.plugin_storage")
    (tmp_path / "turns.json").write_text(json.dumps({"turns": [now - 10, now - 20, now - 2 * 86400]}))
    storage.plugin_data_dir = lambda name: tmp_path
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)

    app = FastAPI()
    app.include_router(_plugin_module().router, prefix="/api/plugins/emporia")
    c = TestClient(app)
    week = c.get("/api/plugins/emporia/_hermes_stats", params={"agent": "alpha", "days": 7}).json()
    m = week["model"]
    assert m["available"] and m["sessions"] == 3 and m["usd"] == 0.16 and m["tokens"] == 187
    assert m["by_kind"] == {"main": {"usd": 0.1, "sessions": 1}, "dm": {"usd": 0.04, "sessions": 1},
                            "session": {"usd": 0.02, "sessions": 1}}
    assert week["turns_24h"] == 2
    assert "secret" not in json.dumps(week)
    day = c.get("/api/plugins/emporia/_hermes_stats", params={"agent": "alpha", "days": 1}).json()
    assert day["model"]["sessions"] == 2
    assert c.get("/api/plugins/emporia/_hermes_stats", params={"agent": "../x"}).status_code == 400

    monkeypatch.setitem(sys.modules, "hermes_state", None)          # no Hermes here: say so
    gone = c.get("/api/plugins/emporia/_hermes_stats", params={"agent": "alpha"}).json()
    assert gone["model"]["available"] is False and gone["model"]["reason"]


@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="Hermes is not installed here")
def test_the_stats_route_reads_real_hermes_session_records(tmp_path):
    """The same reader under Hermes's interpreter, against a real `state.db` in a throwaway
    HERMES_HOME: the public SessionDB API it uses exists and returns what it reads."""
    harness = tmp_path / "harness.py"
    harness.write_text(f'''
import importlib.util, json, time
from hermes_state import SessionDB
db = SessionDB()
for sid, chat, cost in (("s1", "emporia:alpha", 0.25), ("s2", "emporia:alpha:dm:beta", 0.5),
                        ("s3", "emporia:gamma", 7.0)):
    db.create_session(sid, "emporia", model="m", chat_id=chat)
    db.update_token_counts(sid, input_tokens=10, output_tokens=1, estimated_cost_usd=cost)
db.create_session("s4", "cli", model="m")
db.close()
spec = importlib.util.spec_from_file_location("p", {str(PLUGIN / "plugin_api.py")!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps(m._model_cost("alpha", 30, time.time() + 1)))
''')
    out = subprocess.run([str(HERMES_PYTHON), str(harness)], cwd="/opt/hermes", capture_output=True, text=True,
                         timeout=120, env={**os.environ, "PYTHONPATH": "/opt/hermes",
                                           "HERMES_HOME": str(tmp_path / "home")})
    assert out.returncode == 0, out.stderr[-2000:]
    m = json.loads(out.stdout.strip().splitlines()[-1])
    assert m["available"] and m["sessions"] == 2, m
    assert set(m["by_kind"]) == {"main", "dm"}


def test_the_stats_route_counts_subscription_sessions_apart_from_metered_dollars(monkeypatch):
    """Hermes marks a session a subscription pays for `cost_status = 'included'`: no dollars, but counted."""
    import sys
    import time
    import types

    now = time.time()
    rows = [
        {"chat_id": "emporia:alpha", "started_at": now - 60, "estimated_cost_usd": 0.10, "input_tokens": 100,
         "output_tokens": 10, "cost_status": "estimated", "billing_provider": "openrouter"},
        {"chat_id": "emporia:alpha:dm:beta", "started_at": now - 90, "estimated_cost_usd": 2.0, "input_tokens": 50,
         "output_tokens": 5, "cost_status": "included", "billing_provider": "openai-codex"},
        {"chat_id": "emporia:alpha:room:r", "started_at": now - 95, "estimated_cost_usd": 3.0, "input_tokens": 20,
         "output_tokens": 2, "cost_status": "included", "billing_provider": "openai-codex"},
    ]

    class FakeDB:
        def __init__(self, read_only=False):
            assert read_only is True

        def list_sessions_rich(self, **kw):
            return rows[kw["offset"]:kw["offset"] + kw["limit"]]

        def close(self):
            pass

    fake = types.ModuleType("hermes_state")
    fake.SessionDB = FakeDB
    monkeypatch.setitem(sys.modules, "hermes_state", fake)
    m = _plugin_module()._model_cost("alpha", 30, now)
    assert m["usd"] == 0.1 and m["sessions"] == 3 and m["tokens"] == 187
    assert (m["included_sessions"], m["included_tokens"], m["included_providers"]) == (2, 77, ["openai-codex"])
    assert m["by_kind"]["dm"] == {"usd": 0.0, "sessions": 1}
