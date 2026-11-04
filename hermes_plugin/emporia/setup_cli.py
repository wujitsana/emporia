"""`hermes emporia`: install Emporia's runtime and set this profile up (P295).

The plugin itself is what the catalog installs: hooks, the platform, the
skills, the dashboard tab. The agent's tools (the Emporia MCP server), the
bridge and the console are Emporia's Python package, its *runtime*:

* **From the catalog**, ``hermes emporia setup`` clones Emporia at the commit
  pinned in ``emporia_source.json`` into ``<HERMES_HOME>/plugin-data/emporia/
  runtime`` and makes its virtualenv there (``uv sync``, or ``venv`` + ``pip``).
  It never changes the plugin's own files, and the runtime moves only when
  the plugin is updated to a release pinning another commit.
* **From a checkout** of the Emporia repo (this folder inside it), the
  checkout is the runtime: nothing is cloned.

It asks which profile becomes the agent — a new one (recommended: a
marketplace agent's terminal and file tools are off) or this one — then runs
the runtime's ``emporia setup agent <profile>``: the relay to join, the agent's
name there, its role, where its keys live, its wallet and limits. A new
profile gets the plugin through Hermes's own ``hermes -p NAME plugins install``.
Only the chosen profile is written. That runtime knows it came from the catalog (it sits in plugin-data)
and never copies plugin files. Without a terminal it asks nothing and says which flags to pass.

Network: github.com (the clone) and PyPI (its dependencies), then the relay
the agent is set up for. Nothing is sent anywhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_FILE = HERE / "emporia_source.json"
_TIMEOUT_S = 900


# ── Where things are ─────────────────────────────────────────────────────────

def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def profile_name(home: Path | None = None) -> str:
    """Hermes's name for this profile: `default` for the root home."""
    home = home or hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def checkout() -> Path | None:
    """The Emporia repo this plugin folder sits in, when it does (development)."""
    repo = HERE.parents[1]
    if (repo / "src" / "emporia" / "cli.py").is_file() and (repo / "pyproject.toml").is_file():
        return repo
    return None


def runtime_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("emporia") / "runtime"
    except Exception:
        return hermes_home() / "plugin-data" / "emporia" / "runtime"


def _setting(name: str) -> str:
    """A profile setting, read the way Hermes scopes them (never the .env file itself)."""
    try:
        from gateway.platforms._shared import get_scoped_secret
        return str(get_scoped_secret(name, "") or "").strip()
    except Exception:
        return os.environ.get(name, "").strip()


def source() -> dict:
    """The Emporia commit this release of the plugin runs."""
    try:
        data = json.loads(SOURCE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runtime() -> Path | None:
    """The Emporia runtime in use: the checkout, else the installed one."""
    found = checkout()
    if found:
        return found
    rt = runtime_dir()
    return rt if (rt / "src" / "emporia" / "cli.py").is_file() else None


def python_of(rt: Path) -> Path:
    return rt / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


# ── Installing the runtime ───────────────────────────────────────────────────

class SetupFailed(Exception):
    pass


def _run(argv: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT_S,
                             stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise SetupFailed(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupFailed(f"`{' '.join(argv[:3])}` took longer than {_TIMEOUT_S}s") from exc
    if out.returncode != 0:
        raise SetupFailed(f"`{' '.join(argv[:4])}` failed:\n{(out.stderr or out.stdout).strip()[-1500:]}")
    return out.stdout


def _git_head(rt: Path) -> str:
    try:
        return _run(["git", "-C", str(rt), "rev-parse", "HEAD"]).strip()
    except SetupFailed:
        return ""


def install_runtime(say=print) -> Path:
    """Clone (or move) the runtime to the pinned commit and sync its venv."""
    src = source()
    repo, sha = src.get("repo", ""), src.get("sha", "")
    if not (repo.startswith("https://") and len(sha) == 40):
        raise SetupFailed(f"{SOURCE_FILE.name} names no pinned commit (repo {repo!r}, sha {sha!r})")
    rt = runtime_dir()
    if not (rt / ".git").is_dir():
        if rt.exists():
            shutil.rmtree(rt)
        say(f"  Fetching Emporia {sha[:12]} from {repo}")
        _run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", repo, str(rt)])
    if _git_head(rt) != sha:
        _run(["git", "-C", str(rt), "fetch", "--quiet", "origin", sha])
        _run(["git", "-C", str(rt), "checkout", "--quiet", "--detach", sha])
    say("  Installing its Python packages (first time: a minute or two)")
    uv = shutil.which("uv")
    if uv:
        _run([uv, "sync", "--quiet", "--frozen", "--no-dev", "--extra", "x402"], cwd=rt)
    else:
        py = shutil.which("python3") or sys.executable
        if not python_of(rt).exists():
            _run([py, "-m", "venv", str(rt / ".venv")])
        _run([str(python_of(rt)), "-m", "pip", "install", "--quiet", "-e", f"{rt}[x402]"])
    return rt


# ── The commands ─────────────────────────────────────────────────────────────

def _emporia(rt: Path, *args: str) -> int:
    """Run the runtime's `emporia` CLI for this profile, attached to this terminal."""
    env = {**os.environ, "HERMES_HOME": str(hermes_home())}
    return subprocess.call([str(python_of(rt)), "-m", "emporia.cli", *args], env=env)


_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _valid_profile_name(name: str) -> bool:
    return bool(name) and name != "default" and set(name) <= _NAME_OK and name[0].isalnum()


def choose_profile(args: argparse.Namespace, here: str, tty: bool, ask=input) -> tuple[str, bool] | None:
    """Which profile becomes the agent, and whether it is a new one. A
    marketplace agent has its terminal and file tools turned off, so a profile
    of its own is the recommended answer; this one is offered too."""
    if args.profile:
        return args.profile, args.profile != here
    if args.here:
        return here, False
    if not tty:
        return None
    print("\n  Which Hermes profile should be the Emporia agent?")
    print("    1. A new profile, just for it (recommended: a marketplace agent has no terminal or file tools)")
    print(f"    2. This profile ({here})")
    while True:
        pick = (ask("  Choose [1]: ").strip() or "1")
        if pick in ("1", "2"):
            break
    if pick == "2":
        return here, False
    while True:
        name = (ask("  Name for the new profile [emporia-agent]: ").strip().lower() or "emporia-agent")
        if _valid_profile_name(name):
            return name, True
        print("  Lowercase letters, digits, - and _, starting with a letter or digit (not `default`).")


def _install_plugin_into(name: str) -> bool:
    """The catalog plugin into a profile setup just created, through Hermes's own installer."""
    hermes = shutil.which("hermes")
    if not hermes:
        print(f"  ✗ hermes is not on PATH: run `hermes -p {name} plugins install emporia --enable` yourself")
        return False
    print(f"  Installing the Emporia plugin into {name}")
    r = subprocess.run([hermes, "-p", name, "plugins", "install", "emporia", "--enable", "--yes-deps"],
                       stdin=subprocess.DEVNULL, timeout=_TIMEOUT_S)
    return r.returncode == 0


def cmd_setup(args: argparse.Namespace) -> int:
    print("Emporia setup")
    here = profile_name()
    tty = sys.stdin.isatty()
    chosen = choose_profile(args, here, tty)
    if chosen is None:
        print("  No terminal to ask in. Say which profile, and the answers, for example:\n"
              "      hermes emporia setup --profile shop --relay https://relay.example -- --role marketplace "
              "--keys env\n  (--here sets up this profile instead of a new one)")
        return 2
    name, new = chosen
    try:
        rt = runtime()
        if rt is None or (checkout() is None and _git_head(rt) != source().get("sha")):
            rt = install_runtime()
        if not python_of(rt).exists():
            raise SetupFailed(f"{rt} has no virtualenv: run `uv sync` there")
    except SetupFailed as exc:
        print(f"  ✗ {exc}")
        print("  Needs git, and uv or python3 with venv. No profile was changed.")
        return 1
    print(f"  Runtime: {rt}" + ("  (this checkout)" if rt == checkout() else ""))
    print(f"  Agent profile: {name}" + ("  (new)" if new else ""))
    extra = list(args.rest or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if args.relay:
        extra = ["--relay-url", args.relay, *extra]
    code = _emporia(rt, "setup", "agent", name, *extra)
    if code == 0 and new and checkout() is None and not _install_plugin_into(name):
        return 1
    return code


def cmd_status(_args: argparse.Namespace) -> int:
    src, rt = source(), runtime()
    print(f"Profile : {profile_name()} ({hermes_home()})")
    if rt is None:
        print("Runtime : not installed — run `hermes emporia setup`")
        return 1
    head = _git_head(rt)
    pinned = src.get("sha", "")
    note = ("this checkout" if rt == checkout()
            else "pinned" if head == pinned else f"not the pinned {pinned[:12]}: run `hermes emporia update`")
    print(f"Runtime : {rt} @ {head[:12] or '?'} ({note})")
    agent, relay = _setting("EMPORIA_AGENT_ID"), _setting("EMPORIA_RELAY_URL")
    if not agent:
        print("Agent   : not set up — run `hermes emporia setup`")
        return 1
    print(f"Agent   : {agent} on {relay or '?'}")
    print("Checks  : hermes emporia doctor")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    rt = runtime()
    if rt is None:
        print("Emporia's runtime is not installed: run `hermes emporia setup`")
        return 1
    return _emporia(rt, "doctor")


def cmd_update(_args: argparse.Namespace) -> int:
    if checkout():
        print("This plugin runs from an Emporia checkout: update it with `emporia update`.")
        return 0
    try:
        install_runtime()
    except SetupFailed as exc:
        print(f"  ✗ {exc}")
        return 1
    print(f"Runtime at {source().get('sha', '')[:12]}. Restart the gateway to use it: hermes gateway restart")
    return 0


def register_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="emporia_command")
    p = sub.add_parser("setup", help="Install Emporia's runtime and set this profile up as an agent")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--profile", help="The profile to set up as the agent; made if it does not exist")
    where.add_argument("--here", action="store_true", help="Set up this profile (default: asked; a new one is recommended)")
    p.add_argument("--relay", help="The relay to join (https URL)")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="After --: options for `emporia setup agent` (--role, --keys, --wallet, …)")
    sub.add_parser("status", help="Where the runtime is, and this agent on its relay")
    sub.add_parser("doctor", help="Check everything Emporia needs in this profile")
    sub.add_parser("update", help="Move the runtime to the commit this plugin release pins")


_COMMANDS = {"setup": cmd_setup, "status": cmd_status, "doctor": cmd_doctor, "update": cmd_update}


def dispatch(args: argparse.Namespace) -> int:
    command = _COMMANDS.get(getattr(args, "emporia_command", None) or "status")
    return command(args)
