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


def _is_emporia(path: Path) -> bool:
    return (path / "src" / "emporia" / "cli.py").is_file() and (path / "pyproject.toml").is_file()


def runtime() -> Path | None:
    """The Emporia runtime this profile runs: the checkout this plugin sits in;
    else the one its EMPORIA_PYTHON belongs to (a profile `setup` made shares
    the runtime of the profile that made it; a profile set up from a checkout
    runs that checkout); else the one in this profile's plugin-data."""
    found = checkout()
    if found:
        return found
    py = _setting("EMPORIA_PYTHON")
    if py:
        venv_root = Path(py).parent.parent.parent              # <runtime>/.venv/bin/python
        if _is_emporia(venv_root):
            return venv_root
    rt = runtime_dir()
    return rt if _is_emporia(rt) else None


def from_catalog(rt: Path) -> bool:
    """A runtime `hermes emporia setup` installed (in some profile's plugin-data),
    which follows this plugin's pinned commit — not a checkout someone develops in."""
    return rt.parent.name == "emporia" and rt.parent.parent.name == "plugin-data"


def python_of(rt: Path) -> Path:
    return rt / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


# ── Installing the runtime ───────────────────────────────────────────────────

class SetupFailed(Exception):
    pass


def _clean_env() -> dict[str, str]:
    """This process's environment without what the `hermes` launcher sets for Hermes's own
    interpreter (P296, found by the end-to-end run: with Hermes's PYTHONPATH, pip took
    Hermes's packages as Emporia's dependencies and skipped them, and Emporia's Python
    imported Hermes's copies)."""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP")
            and not k.startswith(("PIP_", "UV_PROJECT", "UV_PYTHON"))}


def _run(argv: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT_S,
                             stdin=subprocess.DEVNULL, env=_clean_env())
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


def _is_sha(sha: str) -> bool:
    return len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)


def parse_runtime_from(spec: str) -> tuple[str, str]:
    """`--runtime-from REPO[@SHA]`, for trying a release before it is pushed: an
    Emporia repo on this machine (a path or file:// URL) or an https one, at a
    commit (default: that local repo's HEAD). Never used unless asked for."""
    repo, _, sha = spec.partition("@")
    if repo.startswith("/"):
        repo = "file://" + repo
    if not repo.startswith(("https://", "file://")):
        raise SetupFailed(f"--runtime-from: {repo!r} is neither an https URL nor a path on this machine")
    if not sha and repo.startswith("file://"):
        sha = _run(["git", "-C", repo[len("file://"):], "rev-parse", "HEAD"]).strip()
    if not _is_sha(sha):
        raise SetupFailed(f"--runtime-from: give the full commit (REPO@SHA), not {sha!r}")
    return repo, sha


def install_runtime(say=print, target: Path | None = None, pin: tuple[str, str] | None = None) -> Path:
    """Clone (or move) the runtime to the pinned commit — or `pin`, from
    `--runtime-from` — and sync its venv: the one this profile already runs
    (`target`), else this profile's plugin-data."""
    src = source()
    repo, sha = pin or (src.get("repo", ""), src.get("sha", ""))
    if not (repo.startswith(("https://", "file://") if pin else "https://") and _is_sha(sha)):
        raise SetupFailed(f"{SOURCE_FILE.name} names no pinned commit (repo {repo!r}, sha {sha!r})")
    rt = target if target is not None and from_catalog(target) else runtime_dir()
    if not (rt / ".git").is_dir():
        if rt.exists():
            shutil.rmtree(rt)
        say(f"  Fetching Emporia {sha[:12]} from {repo}")
        _run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", repo, str(rt)])
    if _git_head(rt) != sha:
        _run(["git", "-C", str(rt), "fetch", "--quiet", repo, sha])
    if _git_head(rt) != sha or not _is_emporia(rt):
        # P296: always after a --no-checkout clone, whose files are not there yet even when
        # its HEAD is already the pinned commit (a release's commit is its branch's tip).
        _run(["git", "-C", str(rt), "checkout", "--quiet", "--detach", sha])
    _run(["git", "-C", str(rt), "remote", "set-url", "origin", repo])   # where it came from, for status
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
    env = {**_clean_env(), "HERMES_HOME": str(hermes_home())}
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


def _install_plugin_into(name: str, plugin_source: str = "emporia") -> bool:
    """The plugin into a profile setup just created, through Hermes's own installer:
    the catalog entry, or `--plugin-source` (a git URL, `<url>#hermes_plugin/emporia`)."""
    hermes = shutil.which("hermes")
    if not hermes:
        print(f"  ✗ hermes is not on PATH: run `hermes -p {name} plugins install emporia --enable` yourself")
        return False
    print(f"  Installing the Emporia plugin into {name} ({plugin_source})")
    r = subprocess.run([hermes, "-p", name, "plugins", "install", plugin_source, "--enable"],
                       stdin=subprocess.DEVNULL, timeout=_TIMEOUT_S, env=_clean_env())
    return r.returncode == 0


def _create_profile(name: str) -> bool:
    """A new, minimal Hermes profile (`--no-skills`: it gets Emporia's own), unless it exists."""
    hermes = shutil.which("hermes")
    if not hermes:
        print(f"  ✗ hermes is not on PATH: run `hermes profile create {name} --no-skills` yourself")
        return False
    kw = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_TIMEOUT_S, env=_clean_env())
    if subprocess.run([hermes, "profile", "show", name], **kw).returncode == 0:
        return True
    r = subprocess.run([hermes, "profile", "create", name, "--no-skills"], **kw)
    if r.returncode != 0:
        print(f"  ✗ hermes profile create {name} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return False
    print(f"  Created Hermes profile {name} (--no-skills)")
    return True


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
        pin = parse_runtime_from(args.runtime_from) if args.runtime_from else None
        rt = runtime()
        if pin and rt is not None and not from_catalog(rt):
            raise SetupFailed(f"this profile runs the checkout at {rt}; --runtime-from is for a plugin "
                              "installed from the catalog (or a git URL)")
        want = pin[1] if pin else source().get("sha")
        if rt is None or (from_catalog(rt) and _git_head(rt) != want):
            rt = install_runtime(target=rt, pin=pin)
        if not python_of(rt).exists():
            raise SetupFailed(f"{rt} has no virtualenv: run `uv sync` there")
    except SetupFailed as exc:
        print(f"  ✗ {exc}")
        print("  Needs git, and uv or python3 with venv. No profile was changed.")
        return 1
    print(f"  Runtime: {rt}" + ("" if from_catalog(rt) else "  (a checkout)"))
    print(f"  Agent profile: {name}" + ("  (new)" if new else ""))
    extra = list(args.rest or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if args.relay:
        extra = ["--relay-url", args.relay, *extra]
    # P296: a new profile gets the plugin before the agent is set up, so the restart
    # setup asks for last is the only one needed, and comes after everything it covers.
    if new and from_catalog(rt) and not (_create_profile(name) and _install_plugin_into(name, args.plugin_source)):
        return 1
    code = _emporia(rt, "setup", "agent", name, *extra)
    if code == 0:
        _next_steps(name)
    return code


def _next_steps(name: str) -> None:
    """P296: after the restart setup printed, how to see that it worked."""
    p = "" if name == profile_name() else f"-p {name} "
    print("\n  Then:")
    print(f"      hermes {p}emporia status     # its name on the relay, and whether it has registered")
    print(f"      hermes {p}dashboard          # the Emporia tab: its sessions, rooms, payments, DMs")
    print(f"      hermes {p}emporia doctor     # if anything looks wrong; each problem names its fix")


def _origin(rt: Path) -> str:
    try:
        return _run(["git", "-C", str(rt), "remote", "get-url", "origin"]).strip()
    except SetupFailed:
        return ""


def runtime_note(rt: Path, src: dict) -> str:
    """What `status` says of the runtime: a checkout, the pinned release, a
    `--runtime-from` one (and how to go back), or behind the pinned one."""
    if not from_catalog(rt):
        return "a checkout"
    head, pinned, origin = _git_head(rt), src.get("sha", ""), _origin(rt)
    if pinned and head == pinned:
        return "pinned"
    if origin and origin != src.get("repo", ""):
        back = "; `hermes emporia update` goes back to the pinned one" if pinned else ""
        return f"from --runtime-from {origin}{back}"
    if not pinned:
        return "this plugin pins no release: `hermes emporia update --runtime-from REPO@SHA`"
    return f"not the pinned {pinned[:12]}: run `hermes emporia update`"


def cmd_status(_args: argparse.Namespace) -> int:
    src, rt = source(), runtime()
    print(f"Profile : {profile_name()} ({hermes_home()})")
    if rt is None:
        print("Runtime : not installed — run `hermes emporia setup`")
        return 1
    note = runtime_note(rt, src)
    head = _git_head(rt)
    print(f"Runtime : {rt} @ {head[:12] or '?'} ({note})")
    agent, relay = _setting("EMPORIA_AGENT_ID"), _setting("EMPORIA_RELAY_URL")
    if not agent:
        print("Agent   : not set up — run `hermes emporia setup`")
        return 1
    print(f"Agent   : {agent} on {relay or '?'}")
    on_relay = _registered(relay, agent)
    print("Relay   : " + {True: "registered", False: "not registered yet — it registers when its gateway starts "
                           "(hermes gateway restart)", None: "not answering"}[on_relay])
    print("Checks  : hermes emporia doctor")
    return 0 if on_relay else 1


def _registered(relay: str, agent: str) -> bool | None:
    """Whether the relay has an agent by this name (its yes/no name check, P296)."""
    if not relay.startswith(("https://", "http://")):
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(f"{relay.rstrip('/')}/agents/{agent}/available", timeout=5) as r:
            return not json.loads(r.read()).get("available", True)
    except Exception:
        return None


def cmd_doctor(_args: argparse.Namespace) -> int:
    rt = runtime()
    if rt is None:
        print("Emporia's runtime is not installed: run `hermes emporia setup`")
        return 1
    return _emporia(rt, "doctor")


def cmd_update(args: argparse.Namespace) -> int:
    rt = runtime()
    if rt is not None and not from_catalog(rt):
        print(f"This profile runs the Emporia checkout at {rt}: update it with `emporia update` there.")
        return 0
    try:
        pin = parse_runtime_from(args.runtime_from) if getattr(args, "runtime_from", None) else None
        install_runtime(target=rt, pin=pin)
    except SetupFailed as exc:
        print(f"  ✗ {exc}")
        return 1
    sha = pin[1] if pin else source().get("sha", "")
    print(f"Runtime at {sha[:12]}. Restart the gateway to use it: hermes gateway restart")
    return 0


def register_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="emporia_command")
    p = sub.add_parser("setup", help="Install Emporia's runtime and set this profile up as an agent")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--profile", help="The profile to set up as the agent; made if it does not exist")
    where.add_argument("--here", action="store_true", help="Set up this profile (default: asked; a new one is recommended)")
    p.add_argument("--relay", help="The relay to join (https URL)")
    p.add_argument("--plugin-source", default="emporia",
                   help="Where a new profile's plugin comes from (default: the catalog entry; before it is "
                        "listed: https://github.com/wujitsana/emporia#hermes_plugin/emporia)")
    _runtime_from = dict(metavar="REPO[@SHA]", help="Testing a release before it is pushed: take the runtime "
                         "from this Emporia repo (a path here, or a URL) at this commit, not the pinned one")
    p.add_argument("--runtime-from", **_runtime_from)
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="After --: options for `emporia setup agent` (--role, --keys, --wallet, …)")
    sub.add_parser("status", help="Where the runtime is, and this agent on its relay")
    sub.add_parser("doctor", help="Check everything Emporia needs in this profile")
    u = sub.add_parser("update", help="Move the runtime to the commit this plugin release pins")
    u.add_argument("--runtime-from", **_runtime_from)


_COMMANDS = {"setup": cmd_setup, "status": cmd_status, "doctor": cmd_doctor, "update": cmd_update}


def dispatch(args: argparse.Namespace) -> int:
    command = _COMMANDS.get(getattr(args, "emporia_command", None) or "status")
    return command(args)
