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
    data = tmp_path / "home" / "plugin-data" / "emporia"     # P295: settings in plugin-data, not the plugin
    data.mkdir(parents=True)
    (data / "emporia.json").write_text(json.dumps(
        {"python": "/nonexistent", "relay_url": "http://relay.test", "runtime_dir": str(runtime)}))
    harness = tmp_path / "harness.py"
    harness.write_text(f'''
import importlib.util, json, os, threading, time
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# A stand-in console on the socket the plugin expects.
runtime = Path({str(runtime)!r}); runtime.mkdir(mode=0o700)
stub = FastAPI()
@stub.get("/agents")
async def agents(): return {{"agents": ["alpha"]}}
@stub.api_route("/echo/{{rest:path}}", methods=["GET", "POST"])
async def echo(rest: str, request: Request):
    return {{"path": rest, "query": request.url.query, "headers": dict(request.headers),
            "body": (await request.body()).decode()}}
server = uvicorn.Server(uvicorn.Config(stub, uds=str(runtime / "console.sock"), log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if (runtime / "console.sock").exists(): break
    time.sleep(0.05)

spec = importlib.util.spec_from_file_location("p", {str(plugin / "plugin_api.py")!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
app = FastAPI(); app.include_router(m.router, prefix="/api/plugins/emporia")
c = TestClient(app)
r = c.post("/api/plugins/emporia/echo/relay/alpha/sessions?status=active&profile=beta",
           headers={{"cookie": "hermes_session=SECRET", "X-Hermes-Session-Token": "SECRET",
                    "Authorization": "Bearer SECRET", "content-type": "application/json"}},
           content=b'{{"x": 1}}')
refused = False
try:
    with c.websocket_connect("/api/plugins/emporia/ws/alpha/ws/agent/alpha"):
        pass
except Exception:
    refused = True
here = c.get("/api/plugins/emporia/_hermes_profile?profile=beta").json()
print(json.dumps({{"status": r.status_code, "echo": r.json(), "here": here,
                  "cache": r.headers.get("cache-control"), "ws_refused": refused}}))
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
    assert result["ws_refused"], "a websocket without Hermes's credential is refused"
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
