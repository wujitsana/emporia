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


class NoRelease(SetupFailed):
    """This plugin build pins no commit of the runtime (a build before its first release)."""


def _clean_env() -> dict[str, str]:
    """This process's environment without what the `hermes` launcher sets for Hermes's own
    interpreter (P296, found by the end-to-end run: with Hermes's PYTHONPATH, pip took
    Hermes's packages as Emporia's dependencies and skipped them, and Emporia's Python
    imported Hermes's copies)."""
    return {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP")
            and not k.startswith(("PIP_", "UV_PROJECT", "UV_PYTHON"))}


_TOOL_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
             "XDG_CACHE_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "PIP_INDEX_URL", "UV_INDEX_URL")


def _tool_env() -> dict[str, str]:
    """P298 (security audit): what git, uv and pip are given — the locale, the network's
    certificates and proxies, the caches, and nothing else. `_clean_env` is everything
    the Hermes process holds (its .env's API keys and tokens among it) less its Python
    settings, and pip and uv run packages' build code from PyPI."""
    return {k: v for k, v in os.environ.items() if k in _TOOL_ENV or k.startswith("LC_")}


def _run(argv: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT_S,
                             stdin=subprocess.DEVNULL, env=_tool_env())
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
    if not pin and not sha:
        # Found by the end-to-end run: this said "Needs git, and uv or python3", which
        # sent people after tools they had.
        raise NoRelease("this plugin build pins no release yet; use --runtime-from REPO@SHA "
                        "(an Emporia repo on this machine, or its https URL, at a full commit)")
    if not (repo.startswith(("https://", "file://") if pin else "https://") and _is_sha(sha)):
        raise SetupFailed(f"{SOURCE_FILE.name} names no pinned commit (repo {repo!r}, sha {sha!r})")
    return _sync_runtime(say, target if target is not None and from_catalog(target) else runtime_dir(), repo, sha)


_INSTALLED = "emporia-installed"      # in the runtime's .venv: the commit its packages were installed for


def _sync_runtime(say, rt: Path, repo: str, sha: str) -> Path:
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
    marker = rt / ".venv" / _INSTALLED
    first = not python_of(rt).exists()
    if not first and marker.is_file() and marker.read_text().strip() == sha:
        return rt                      # its packages are already this commit's: nothing to install
    say("  Installing its Python packages" + (" (first time: a minute or two)" if first else ""))
    uv = shutil.which("uv")
    if uv:
        _run([uv, "sync", "--quiet", "--frozen", "--no-dev", "--extra", "x402"], cwd=rt)
    else:
        py = shutil.which("python3") or sys.executable
        if first:
            _run([py, "-m", "venv", str(rt / ".venv")])
        _run([str(python_of(rt)), "-m", "pip", "install", "--quiet", "-e", f"{rt}[x402]"])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(sha + "\n")
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


def _hermes_root() -> Path:
    home = hermes_home()
    return home.parent.parent if home.parent.name == "profiles" else home


def _profile_exists(name: str) -> bool:
    return name == "default" or (_hermes_root() / "profiles" / name).is_dir()


def _has_agent(name: str) -> bool:
    """Whether setup agent wrote anything of the agent's into the profile (its MCP entry, its keys)."""
    prof = _hermes_root() / "profiles" / name
    try:
        if "emporia.mcp_server" in (prof / "config.yaml").read_text():
            return True
    except OSError:
        pass
    keys = prof / "home" / ".hermes" / "keys"
    return keys.is_dir() and any(keys.iterdir())


def _remove_profile(name: str) -> None:
    """A profile this setup made a moment ago and could not finish, through Hermes's own delete,
    so no half-made empty profile is left behind. Never one that holds an agent."""
    hermes = shutil.which("hermes")
    if not hermes or _has_agent(name):
        return
    r = subprocess.run([hermes, "profile", "delete", name, "--yes"], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=_TIMEOUT_S, env=_clean_env())
    print(f"  Removed the profile {name} this setup had just made" if r.returncode == 0
          else f"  ! The profile {name} was made but not finished: hermes profile delete {name}")


def _create_profile(name: str) -> str:
    """A new, minimal Hermes profile (`--no-skills`: it gets Emporia's own), unless it exists:
    "made", "there" (it existed), or "" when it could not be made."""
    hermes = shutil.which("hermes")
    if not hermes:
        print(f"  ✗ hermes is not on PATH: run `hermes profile create {name} --no-skills` yourself")
        return ""
    kw = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_TIMEOUT_S, env=_clean_env())
    if subprocess.run([hermes, "profile", "show", name], **kw).returncode == 0:
        return "there"
    r = subprocess.run([hermes, "profile", "create", name, "--no-skills"], **kw)
    if r.returncode != 0:
        print(f"  ✗ hermes profile create {name} failed: {(r.stderr or r.stdout).strip()[-300:]}")
        return ""
    print(f"  Created Hermes profile {name} (--no-skills)")
    return "made"


def cmd_setup(args: argparse.Namespace) -> int:
    rest = list(args.rest or [])
    if rest[:1] and rest[0] in SETUP_PASSTHROUGH:          # `emporia setup relay|stripe|dashboard|log …`
        return cmd_passthrough(argparse.Namespace(emporia_command="setup", cli_args=rest))
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
    except NoRelease as exc:
        print(f"  ✗ {exc}. No profile was changed.")
        return 1
    except SetupFailed as exc:
        print(f"  ✗ {exc}")
        print("  Needs git, and uv or python3 with venv. No profile was changed.")
        return 1
    # Found by the end-to-end run: "(new)" was said of any profile but this one, made or not.
    new = new and not _profile_exists(name)
    print(f"  Runtime: {rt}" + ("" if from_catalog(rt) else "  (a checkout)"))
    print(f"  Agent profile: {name}" + ("  (new)" if new else "  (existing)" if name != here else ""))
    extra = list(args.rest or [])
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if args.relay:
        extra = ["--relay-url", args.relay, *extra]
    # P296: a new profile gets the plugin before the agent is set up, so the restart
    # setup asks for last is the only one needed, and comes after everything it covers.
    made = ""
    if new and from_catalog(rt):
        made = _create_profile(name)
        if not made:
            return 1
        if not _install_plugin_into(name, args.plugin_source):
            if made == "made":
                _remove_profile(name)
            return 1
        if made == "made":
            # Found by the end-to-end run: the profile made just now has a config.yaml,
            # so setup agent took it for an existing one — role from its full tools
            # (developer), keys left in files. It is a new agent.
            extra = ["--new-profile", *extra]
    elif from_catalog(rt) and name != here and not (_hermes_root() / "profiles" / name / "plugins" / "emporia"
                                                    / "plugin.yaml").is_file():
        if not _install_plugin_into(name, args.plugin_source):       # an existing profile without it
            return 1
    code = _emporia(rt, "setup", "agent", name, *extra)
    if code == 0:
        _next_steps(name)
    elif made == "made":
        _remove_profile(name)          # only if setup agent stopped before writing the agent
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """This profile's checks (found by the end-to-end run: it checked every profile,
    and refused the `--all` its own last line suggests)."""
    rt = runtime()
    if rt is None:
        print("Emporia's runtime is not installed: run `hermes emporia setup`")
        return 1
    name = profile_name()
    scope = [] if name == "default" else ["--profile", name]
    return _emporia(rt, "doctor", *scope, *(["--all"] if getattr(args, "all", False) else []))


def cmd_update(args: argparse.Namespace) -> int:
    rt = runtime()
    if rt is not None and not from_catalog(rt):
        print(f"This profile runs the Emporia checkout at {rt}: update it with `emporia update` there.")
        return 0
    try:
        pin = parse_runtime_from(args.runtime_from) if getattr(args, "runtime_from", None) else None
        before = _state(rt)
        rt = install_runtime(target=rt, pin=pin)
    except SetupFailed as exc:
        print(f"  ✗ {exc}")
        return 1
    sha = pin[1] if pin else source().get("sha", "")
    if _state(rt) == before:
        # Found by the end-to-end run: it said "restart the gateway" when nothing had changed.
        print(f"Runtime already at {sha[:12]}: nothing to do.")
        return 0
    print(f"Runtime at {sha[:12]}. Restart each profile that runs it to use it:")
    for name in _profiles_running(rt) or [profile_name()]:
        print(f"    hermes {'' if name == 'default' else f'-p {name} '}gateway restart")
    return 0


def _state(rt: Path | None) -> tuple[str, str]:
    """The runtime's commit and the commit its packages were installed for."""
    if rt is None:
        return "", ""
    try:
        installed = (rt / ".venv" / _INSTALLED).read_text().strip()
    except OSError:
        installed = ""
    return _git_head(rt), installed


def _profiles_running(rt: Path) -> list[str]:
    """The profiles whose Emporia MCP server is this runtime's python."""
    py, root, names = str(python_of(rt)), _hermes_root(), []
    for cfg in [root / "config.yaml", *sorted((root / "profiles").glob("*/config.yaml"))]:
        try:
            if py in cfg.read_text():
                names.append("default" if cfg.parent == root else cfg.parent.name)
        except OSError:
            continue
    return names


# The runtime's own CLI commands, passed through for this profile: `hermes emporia wallet …`
# runs `emporia wallet …` (found by the end-to-end run: a plugin-only install puts no `emporia`
# on PATH, yet doctor and the tools' refusals name its commands). setup, status, doctor and
# update are the plugin's own. Kept in step with emporia.cli's commands by a test.
#
# The key guard is unchanged by this: it matches an `emporia` command by its words, whatever
# starts it, so `hermes emporia wallet backup` from an agent's terminal is blocked exactly as
# `emporia wallet backup` is, and the commands that change keys, wallets or roles still wait
# for the person's yes. An agent with a terminal could already run the runtime's CLI
# (`$EMPORIA_PYTHON -m emporia.cli`, guarded the same way); this adds no reach.
PASSTHROUGH = ("agent", "archive", "bugs", "delete", "dms", "guardians", "house", "identity", "ideas", "log",
               "login", "relay", "role", "sheet", "signer", "stats", "visits", "wallet")
# `emporia setup <these>` passes through too: the plugin's setup is the agent's.
SETUP_PASSTHROUGH = ("relay", "stripe", "dashboard", "log")


def cmd_passthrough(args: argparse.Namespace) -> int:
    rt = runtime()
    if rt is None:
        print("Emporia's runtime is not installed: run `hermes emporia setup`")
        return 1
    return _emporia(rt, args.emporia_command, *(args.cli_args or []))


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
    d = sub.add_parser("doctor", help="Check everything Emporia needs in this profile")
    d.add_argument("--all", action="store_true", help="every check, not just a count of those that pass")
    u = sub.add_parser("update", help="Move the runtime to the commit this plugin release pins")
    u.add_argument("--runtime-from", **_runtime_from)
    for name in PASSTHROUGH:
        # No option of its own (prefix_chars nobody types): every word after the command,
        # --help included, goes to the runtime's `emporia NAME`.
        c = sub.add_parser(name, add_help=False, prefix_chars="\x00", help=f"emporia {name} … (the runtime's CLI)")
        c.add_argument("cli_args", nargs=argparse.REMAINDER)


_COMMANDS = {"setup": cmd_setup, "status": cmd_status, "doctor": cmd_doctor, "update": cmd_update}


def dispatch(args: argparse.Namespace) -> int:
    name = getattr(args, "emporia_command", None) or "status"
    command = _COMMANDS.get(name) or (cmd_passthrough if name in PASSTHROUGH else None)
    if command is None:
        print(f"hermes emporia: no command {name!r}")
        return 2
    return command(args)
