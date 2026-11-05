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
from hermes_cli import web_server_chat               # past Hermes's own websocket check:
web_server_chat._ws_auth_ok = lambda socket: True    # the console still needs its secret
with c.websocket_connect("/api/plugins/emporia/ws/alpha/ws/agent/alpha") as s:
    relayed = s.receive_text()
here = c.get("/api/plugins/emporia/_hermes_profile?profile=beta").json()
print(json.dumps({{"status": r.status_code, "echo": r.json(), "here": here, "relayed": relayed,
                  "cache": r.headers.get("cache-control"), "ws_refused": refused}}))
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
    assert result["ws_refused"], "a websocket without Hermes's credential is refused"
    assert result["relayed"] == "as the console: alpha/ws/agent/alpha", "the websocket carries the secret"
    assert oct(runtime.stat().st_mode)[-3:] == "700"
    assert result["here"] == {"profile": "beta"}, "P227: the tab learns which profile it is open in"



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
    assert r["picker"] == [{"agents": ["alpha", "beta"]}, {"agents": ["beta", "alpha"]}], r["picker"]
    secrets = []
    for name, started in zip(("alpha", "beta"), r["started"]):
        argv, secret = started["argv"], started["secret"].strip()
        assert argv[argv.index("--profile") + 1] == str(root / "profiles" / name)
        assert "--hermes-root" not in argv and "--keys-dir" not in argv
        assert len(secret) >= 32 and started["closed"]
        assert secret not in json.dumps(argv) and secret not in json.dumps(started["env"])
        secrets.append(secret)
    assert secrets[0] != secrets[1]


def _source_digest() -> str:
    """dashboard/vite.plugin.config.ts sourceDigest(), in Python."""
    import hashlib
    dash = REPO / "dashboard"
    inputs = ["src", "vite.plugin.config.ts", "package-lock.json", "postcss.noRemoteFonts.ts"]
    files = []
    for rel in inputs:
        p = dash / rel
        found = sorted(p.rglob("*")) if p.is_dir() else [p]
        files += [f.relative_to(dash).as_posix() for f in found
                  if f.is_file() and not f.name.endswith((".test.ts", ".test.tsx"))]
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.encode() + b"\n")
        h.update((dash / f).read_bytes())
    return h.hexdigest()


def test_the_committed_tab_bundle_is_built_from_the_dashboard_as_it_is():
    """P295: a catalog install is the plugin folder alone, so the tab's bundle is
    committed there. Changing dashboard/ without `npm run build:hermes-plugin`
    would ship the old tab."""
    dist = PLUGIN / "dist"
    assert (dist / "index.js").is_file() and (dist / "style.css").is_file()
    assert (dist / "SOURCE").read_text().strip() == _source_digest(), (
        "the tab's bundle is older than dashboard/: run `npm run build:hermes-plugin` in dashboard/")
