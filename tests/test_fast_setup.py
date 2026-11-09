"""A fast first run: `hermes emporia setup` installs the agent's packages only (not the relay's),
from caches every profile of the user shares, with a line per phase saying how long it took."""
import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
spec = importlib.util.spec_from_file_location("emporia_setup_cli_fast", REPO / "hermes_plugin" / "emporia" / "setup_cli.py")
setup_cli = importlib.util.module_from_spec(spec)
sys.modules["emporia_setup_cli_fast"] = setup_cli
spec.loader.exec_module(setup_cli)

#: Importable names (top-level) of the packages only a relay uses: its faster event loop and HTTP
#: parser (uvicorn[standard]), Sentry, the stripe SDK (nothing imports it). Not chess: the house
#: agent's runner and local turn validation use it on an agent.
RELAY_ONLY_MODULES = ("sentry_sdk", "uvloop", "httptools", "watchfiles", "stripe")


def _names(specs):
    import re
    return {re.split(r"[\[<>=!~ ;]", s, maxsplit=1)[0].lower() for s in specs}


def test_the_base_dependencies_are_the_agents_and_the_relay_extra_holds_the_servers():
    base = PYPROJECT["project"]["dependencies"]
    relay = PYPROJECT["project"]["optional-dependencies"]["relay"]
    assert not ({"sentry-sdk", "py-ecc", "stripe"} & _names(base)), base
    assert {"sentry-sdk", "py-ecc"} <= _names(relay)
    assert "chess" in _names(base) and "chess" not in _names(relay), "the house runner needs chess on an agent"
    assert not any(s.startswith("uvicorn[standard]") for s in base), "the agent's console needs plain uvicorn only"
    assert any(s.startswith("uvicorn[standard]") for s in relay)
    assert "stripe" not in _names(base + relay), "the stripe SDK was never imported; cards use HTTP"


_BLOCKED_IMPORT_PROBE = """
import importlib, pkgutil, sys

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {blocked!r}:
            raise ModuleNotFoundError("not installed on an agent: " + name, name=name)
        return None

sys.meta_path.insert(0, Block())
import emporia
failed = []
for m in pkgutil.walk_packages(emporia.__path__, "emporia."):
    try:
        importlib.import_module(m.name)
    except BaseException as exc:
        failed.append(m.name + ": " + repr(exc.__cause__ or exc)[:200])
for name in ("emporia.mcp_server", "emporia.cli", "emporia.agent_sdk", "emporia.console.app",
             "emporia.plugins.platforms.emporia.bridge", "emporia.relay_server"):
    importlib.import_module(name)
print("FAILED:" + "|".join(failed))
"""


def test_an_agent_install_without_the_relay_packages_imports_every_emporia_module(tmp_path):
    """Every import of a relay-only package is lazy or inside a relay-only function: with those
    packages blocked, the MCP server, bridge, CLI, SDK and console (and every other module)
    still import — what an install without the `relay` extra has."""
    env = {"PATH": "/usr/bin:/bin", "HERMES_HOME": str(tmp_path / "h"), "HOME": str(tmp_path),
           "EMPORIA_NO_DOTENV": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-c", _BLOCKED_IMPORT_PROBE.format(blocked=set(RELAY_ONLY_MODULES))],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip().endswith("FAILED:"), out.stdout[-1500:]


def test_the_house_runners_chess_move_works_with_the_agent_set():
    """chess is a base dependency, so `emporia house run` on an agent runtime has it."""
    import random

    from emporia import house
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert house.chess_move(start, random.Random(1)) is not None
    assert "chess" in _names(PYPROJECT["project"]["dependencies"])


def test_the_agents_install_asks_for_no_relay_extra():
    src = (REPO / "hermes_plugin" / "emporia" / "setup_cli.py").read_text()
    sync = [line for line in src.splitlines() if '"sync"' in line]
    assert sync and all('"x402"' in line and '"relay"' not in line for line in sync), sync
    export = (REPO / "scripts" / "pin_plugin_runtime.py").read_text()
    assert '"--extra", "x402"' in export and '"relay"' not in export, "the pip path's lock export is the agent set"
    requirements = (REPO / setup_cli.LOCKED_REQUIREMENTS).read_text()
    for dist in ("sentry-sdk", "uvloop", "stripe"):
        assert f"\n{dist}==" not in "\n" + requirements, dist
    for dist in ("fastmcp", "eth-account", "fastapi", "uvicorn", "chess"):
        assert f"\n{dist}==" in "\n" + requirements, dist


def test_the_relay_image_and_a_checkout_install_the_relay_extra():
    for rel, needle in (("Dockerfile", "--extra relay"), (".github/workflows/ci.yml", "--extra relay"),
                        ("installer/install.py", '"relay"'), ("scripts/local_relay.py", '"relay"')):
        assert needle in (REPO / rel).read_text(), rel
    assert "--extra relay" in (REPO / "README.md").read_text()


def test_uv_and_pip_use_the_users_cache_not_the_profiles_home(monkeypatch, tmp_path):
    """Each profile can have a HOME of its own; a cache under it is cold for every profile. Hermes
    knows the OS user's real home; the tools are given a cache there, once, shared by all profiles."""
    real = tmp_path / "real"
    profile_home = tmp_path / "hh" / "profiles" / "judge" / "home"
    profile_home.mkdir(parents=True)
    for name in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(profile_home))
    monkeypatch.setattr(setup_cli, "_real_home", lambda: real)
    env = setup_cli._tool_env()
    assert env["UV_CACHE_DIR"] == str(real / ".cache" / "uv")
    assert env["PIP_CACHE_DIR"] == str(real / ".cache" / "pip")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert setup_cli._tool_env()["UV_CACHE_DIR"] == str(tmp_path / "xdg" / "uv")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "chosen"))          # the person's own choice wins
    assert setup_cli._tool_env()["UV_CACHE_DIR"] == str(tmp_path / "chosen")


def test_the_real_home_is_hermess_answer_when_there_is_one(monkeypatch, tmp_path):
    import types
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_real_home=lambda: str(tmp_path / "me")))
    assert setup_cli._real_home() == tmp_path / "me"
    monkeypatch.delitem(sys.modules, "hermes_constants")
    monkeypatch.setenv("HERMES_REAL_HOME", str(tmp_path / "launcher"))
    assert setup_cli._real_home() in (tmp_path / "launcher", Path.home())


def test_the_shared_cache_still_gives_the_tools_no_secret(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    env = setup_cli._tool_env()
    assert "OPENROUTER_API_KEY" not in env and "UV_CACHE_DIR" in env


def test_the_pinned_commit_is_fetched_alone_not_cloned_with_its_history(monkeypatch, tmp_path):
    sha, tree = "a" * 40, "b" * 40
    ran, said = [], []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: ran.append(argv) or "")
    monkeypatch.setattr(setup_cli, "_git_head", lambda rt: sha)
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda *a, **k: None)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    rt = tmp_path / "plugin-data" / "emporia" / "runtime"
    setup_cli._sync_runtime(said.append, rt, "https://example.test/e", sha, tree)
    assert not any("clone" in a for a in ran), "a clone downloads every commit of Emporia"
    fetch = [a for a in ran if "fetch" in a]
    assert len(fetch) == 1 and "--depth=1" in fetch[0] and fetch[0][-2:] == ["https://example.test/e", sha], fetch
    sync = [a for a in ran if "sync" in a]
    assert sync == [["/usr/bin/uv", "sync", "--quiet", "--frozen", "--no-dev", "--extra", "x402"]], sync


def test_each_slow_phase_says_what_it_does_and_how_long_it_took(monkeypatch, tmp_path):
    sha = "a" * 40
    said = []
    monkeypatch.setattr(setup_cli, "_run", lambda argv, cwd=None: "")
    monkeypatch.setattr(setup_cli, "verify_runtime", lambda *a, **k: None)
    monkeypatch.setattr(setup_cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    setup_cli._sync_runtime(said.append, tmp_path / "rt", "https://example.test/e", sha)
    text = "\n".join(said)
    assert "Fetching Emporia" in text and "Installing its Python packages" in text
    assert text.count(" ...") == 2 and text.count(": done in ") == 2, text


def test_a_failed_phase_does_not_claim_it_is_done(monkeypatch, tmp_path):
    said = []

    def boom(argv, cwd=None):
        raise setup_cli.SetupFailed("no network")
    monkeypatch.setattr(setup_cli, "_run", boom)
    with pytest.raises(setup_cli.SetupFailed):
        setup_cli._sync_runtime(said.append, tmp_path / "rt", "https://example.test/e", "a" * 40)
    assert said and not any("done in" in s for s in said)
