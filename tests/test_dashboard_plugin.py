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
