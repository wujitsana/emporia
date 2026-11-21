"""No crypto in a profile whose agent can control the desktop or a real browser.

A browser wallet may hold mainnet funds even though Emporia is testnet only, and an agent that can click can
press "Sign" in it, or open a desktop terminal (which carries no Hermes session marker) and run a person-only
command. So where the profile gives the agent that control, Emporia does no crypto of its own, with no override:
turning on the `money` feature (setup, `emporia features on money`, `emporia template`, a wallet that implies it),
the payment paths (`agent_wallet.sign_exact`, `sign_entry`, `pay_tempo`, `sign_escrow`, `reclaim_escrow`: every MCP
payment goes through one of them), anything signed or sent from the agent's own wallet (`chain_link.person_only`),
and `--browser` for ERC-8004 (`agent_card_cli`). `--signature` (signed on another device) and read-only views stay.

Desktop control, for a profile (the plugin's `desktop_guard.detect` reads the same; a test holds them equal):

* the `computer_use` toolset: named in a saved list, or part of Hermes's default set (every `hermes-*` bundle holds
  it, and it is not default-off) while the tool is usable, i.e. a cua-driver is present (`HERMES_CUA_DRIVER_CMD`
  resolving, or the one Hermes's package manager installed, from the store's `facts.json`; a bare `cua-driver` on
  PATH does not count, Hermes does not use one: a best-effort mirror of the tool's `check_fn`);
* the `browser-cdp` toolset (it attaches to a running Chrome);
* the `browser` toolset (named, or in the default set) AND a real browser: `browser.use_real_profile: true`, a
  `browser.cdp_url`, or `BROWSER_CDP_URL` / `BU_CDP_WS` / `BU_CDP_URL` (read from the profile's `.env` by name, as a
  yes or no: an MCP server's own environment is filtered, so the process environment alone would miss it). A plain
  sandboxed browser is not desktop control;
* an MCP server in `mcp_servers` whose name looks like a browser or desktop driver (browser, playwright, puppeteer,
  chrome, chromium, devtools, cdp, computer-use, cua: `MCP_DESKTOP`; a server merely called browser, computer or
  desktop is not guessed at, a person lists it in `EMPORIA_DESKTOP_TOOLS`);
* any tool a person names in `EMPORIA_DESKTOP_TOOLS` (comma-separated names or prefixes).

The default set counts unless every platform in use has a saved list: `cli`, `cron`, `emporia` and any platform
named in `platform_toolsets` (a list-literal string such as "['a','b']" is a saved list, as Hermes reads it).
`agent.disabled_toolsets` removes a name. Not seen: a live `/browser connect` (in-process state of Hermes that neither
this module nor the plugin can see; the plugin's session guard does not need it, it refuses every `browser_*` tool in a
marked session).

The profile's config.yaml is read with `yaml.safe_load`, read-only; an unreadable or missing one is *unknown*, which
never blocks anything (doctor says so). Which profile: the one named by the caller, else `HERMES_HOME` when it holds
a config.yaml, else the profile holding this checkout, else none (unknown).
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from collections.abc import Mapping

COMPUTER_TOOLS = ("computer_use",)
CDP_TOOLS = ("browser_cdp", "browser_dialog")
BROWSER_TOOLS = CDP_TOOLS + ("browser_exec", "browser_vault")
#: Platforms Hermes serves an Emporia agent on: each needs a saved toolset list, or its default set counts.
PLATFORMS_IN_USE = ("cli", "cron", "emporia")
#: An MCP server whose name says it drives a local browser or the desktop (not a cloud browser, not a file or
#: terminal server: "browser" and "desktop" alone are too loose; a person adds the rest in EMPORIA_DESKTOP_TOOLS).
MCP_DESKTOP = re.compile(r"playwright|puppeteer|chrome|chromium|devtools|cdp|computer[-_]?use|cua", re.I)
CDP_ENV_NAMES = ("BROWSER_CDP_URL", "BU_CDP_WS", "BU_CDP_URL")
EXTRA_SETTING = "EMPORIA_DESKTOP_TOOLS"

REFUSED = ("this profile lets the agent control the desktop or a real browser, so Emporia does no crypto here "
           "({what}); turn those tools off for this profile, or link 8004 with `--signature` signed on another "
           "device or hardware wallet")
WALLET_WARNING = ("a wallet with real funds should never sit in a browser or desktop the agent can control")


def _list_literal(value: str) -> list[str] | None:
    text = value.strip()
    if text.startswith("["):
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
        if isinstance(parsed, (list, tuple)):
            return [str(i).strip() for i in parsed if str(i).strip()]
    return None


def _names(value: Any) -> list[str]:
    """A config value as names, as Hermes reads it: a list, a list-literal string, or one scalar name."""
    if isinstance(value, str):
        parsed = _list_literal(value)
        if parsed is not None:
            return parsed
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _saved_list(value: Any) -> list[str] | None:
    """A saved `platform_toolsets.<platform>` list (a real list or a list-literal string), else None."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return _list_literal(value)
    return None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def extra_names(value: Any) -> list[str]:
    """EMPORIA_DESKTOP_TOOLS: comma-separated tool names or prefixes, lower-cased."""
    return [n.strip().lower() for n in str(value or "").replace(";", ",").split(",") if n.strip()]


_CUA_BINARIES = ("cua-driver", "cua-driver.exe", "CuaDriver.app/Contents/MacOS/cua-driver")
_cua_entries: dict[str, tuple[float, list[str]]] = {}


def _cua_store_roots(env: Mapping[str, str]) -> list[Path]:
    """Where Hermes's package manager keeps its store: `HERMES_RUNTIME_DIR`, else `<hermes root>/tools` (the root is
    the folder above `profiles/`, or the home itself)."""
    roots: list[Path] = []
    runtime = str(env.get("HERMES_RUNTIME_DIR") or "").strip()
    if runtime:
        roots.append(Path(runtime))
    home = str(env.get("HERMES_HOME") or "").strip()
    for base in ([Path(home)] if home else []) + [Path.home() / ".hermes"]:
        roots.append(base / "tools")
        roots += [p.parent / "tools" for p in base.parents if p.name == "profiles"]
    return roots


def _cua_entries_of(root: Path) -> list[str]:
    """The store entries `facts.json` lists for cua-driver (read once per change of the file)."""
    facts = root / "facts.json"
    try:
        stamp = facts.stat().st_mtime
    except OSError:
        return []
    hit = _cua_entries.get(str(facts))
    if hit is None or hit[0] != stamp:
        found: list[str] = []
        try:
            packages = (json.loads(facts.read_text(encoding="utf-8")) or {}).get("packages") or {}
            rows = packages.get("cua-driver")
            for row in (rows if isinstance(rows, list) else [rows]):
                entry = row.get("entry") if isinstance(row, dict) else None
                if isinstance(entry, str) and entry and "/" not in entry and "\\" not in entry and entry != "..":
                    found.append(entry)
        except (OSError, ValueError, AttributeError, TypeError):
            found = []
        hit = _cua_entries[str(facts)] = (stamp, found)
    return hit[1]


def cua_driver_present(env: Mapping[str, str] | None = None) -> bool:
    """Whether Hermes's `computer_use` tool has a driver, the way Hermes finds it (`resolve_cua_driver_cmd`):
    `HERMES_CUA_DRIVER_CMD` when set (it must resolve), else the cua-driver its package manager installed (an entry
    in `<store>/facts.json` whose binary is there). Never a bare `cua-driver` on PATH: Hermes does not use one.
    Read-only, without importing Hermes; a miss means "not seen", never an error."""
    env = os.environ if env is None else env
    cmd = str(env.get("HERMES_CUA_DRIVER_CMD") or "").strip()
    if cmd:
        return shutil.which(os.path.expanduser(cmd)) is not None
    for root in _cua_store_roots(env):
        for entry in _cua_entries_of(root):
            if any((root / entry / rel).is_file() for rel in _CUA_BINARIES):
                return True
    return False


def _platforms(config: dict) -> list[str]:
    """Every platform the agent runs on: cli, cron, emporia, those named in `platform_toolsets`, and each enabled
    one in the config's `platforms` (a mapping by name, or a list of `{platform: name}`) and `gateway.platforms`."""
    names = list(PLATFORMS_IN_USE)
    sections = [config.get("platforms")]
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        sections.append(gateway.get("platforms"))
    for section in sections:
        rows = [(k, v) for k, v in section.items()] if isinstance(section, dict) else (
            [(r.get("platform") or r.get("name"), r) for r in section if isinstance(r, dict)]
            if isinstance(section, list) else [])
        for name, row in rows:
            if name and not (isinstance(row, dict) and row.get("enabled") is False):
                names.append(str(name))
    return list(dict.fromkeys(names))


def detect(config: Any, env: Mapping[str, str] | None = None, cua: bool | None = None,
           env_known: bool = True) -> dict:
    """What a profile's config (and environment) says: ``{"known": bool, "tools": [names], "real_browser": bool,
    "why": [lines], "mcp_servers": [names], "env_known": bool}``. ``config`` None (unreadable) or not a mapping is
    unknown: no tools, ``known`` False. ``cua``: whether a cua-driver is present (None: looked up when needed).
    ``env_known`` False: the profile's `.env` could not be asked, so the CDP names and
    EMPORIA_DESKTOP_TOOLS in `env` are only what the process had. Then a profile whose config already shows the
    browser toolset on is treated as having desktop control (fail closed: the unseen names may make that browser
    real, and a payment is the one thing that cannot be undone); any other profile keeps working and doctor says
    the read failed."""
    if not isinstance(config, dict):
        return {"known": False, "tools": [], "real_browser": False, "why": [], "mcp_servers": [], "env_known": True}
    env = os.environ if env is None else env
    raw = config.get("platform_toolsets")
    saved: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for platform, value in raw.items():
            names = _saved_list(value)
            if names is not None:
                saved[str(platform)] = names
    on: set[str] = {n for names in saved.values() for n in names}
    on.update(_names(config.get("toolsets")))
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    off = set(_names(agent.get("disabled_toolsets")))
    composite = any(p not in saved for p in _platforms(config)) or any(n.startswith("hermes") for n in on)
    browser_on = ("browser" in on or composite) and "browser" not in off
    browser = config.get("browser") if isinstance(config.get("browser"), dict) else {}
    cdp_url = str(browser.get("cdp_url") or "").strip() or next(
        (str(env.get(n) or "").strip() for n in CDP_ENV_NAMES if str(env.get(n) or "").strip()), "")
    profile_real = _truthy(browser.get("use_real_profile"))
    real = profile_real or bool(cdp_url)
    tools: list[str] = []
    why: list[str] = []
    if "computer_use" not in off:
        if "computer_use" in on:
            tools.append("computer_use")
            why.append("the computer_use toolset is on (the agent can click and type on the desktop)")
        elif composite:
            usable = cua_driver_present(env) if cua is None else cua
            if not usable:
                backend = config.get("computer_use")
                backend = str(backend.get("backend") or "").strip().lower() if isinstance(backend, dict) else ""
                usable = bool(backend) and backend != "cua"        # another backend: its own check, not mirrored
            if usable:
                tools.append("computer_use")
                why.append("computer_use is in Hermes's default set and a desktop driver is present")
    if "browser-cdp" in on and "browser-cdp" not in off:
        tools += [t for t in CDP_TOOLS if t not in tools]
        why.append("the browser-cdp toolset is on (it attaches to a running Chrome)")
    if browser_on and real:
        tools += [t for t in BROWSER_TOOLS if t not in tools]
        why.append("the browser toolset is on and Hermes's browser is a real one "
                   + ("(browser.use_real_profile)" if profile_real else "(a cdp_url)"))
    if browser_on and not real and not env_known:
        tools.append("browser (real or not: the profile's .env could not be read)")
        why.append("the browser toolset is on and the profile's .env could not be read, so a real browser "
                   "(BROWSER_CDP_URL, BU_CDP_WS, BU_CDP_URL) cannot be ruled out")
    servers = config.get("mcp_servers")
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if MCP_DESKTOP.search(str(name)) and not (isinstance(cfg, dict) and cfg.get("enabled") is False):
                tools.append(f"mcp__{name}__*")
                why.append(f"the MCP server {name!r} looks like a browser or desktop driver")
    for name in extra_names(env.get(EXTRA_SETTING)):
        if name not in tools:
            tools.append(name)
            why.append(f"{EXTRA_SETTING} names {name}")
    return {"known": True, "tools": tools, "real_browser": real, "why": why,
            "mcp_servers": [str(n) for n in servers] if isinstance(servers, dict) else [], "env_known": env_known}


def profile_home(profile: Path | str | None = None) -> Path | None:
    """The profile whose config is read: the one given, else the one whose `.env` this process was handed
    (`EMPORIA_DOTENV`: an agent's MCP server and the helpers that run as it), else HERMES_HOME (with a
    config.yaml), else the profile holding this checkout, else None."""
    if profile:
        return Path(profile)
    dotenv = os.environ.get("EMPORIA_DOTENV")
    if dotenv and (Path(dotenv).expanduser().parent / "config.yaml").exists():
        return Path(dotenv).expanduser().parent
    home = os.environ.get("HERMES_HOME")
    if home and (Path(home) / "config.yaml").exists():
        return Path(home)
    try:
        from emporia import hermes_layout
        return hermes_layout.containing_profile(Path(__file__).resolve().parents[2])
    except Exception:  # noqa: BLE001 — no way to find it: unknown
        return None


_config_cache: dict[str, tuple[float, Any]] = {}


def read_config(profile: Path | str | None = None) -> Any:
    """The profile's config.yaml as a dict, or None (unreadable, missing, not yaml). Parsed once per change of the
    file."""
    home = profile_home(profile)
    if home is None:
        return None
    path = home / "config.yaml"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    hit = _config_cache.get(str(path))
    if hit is None or hit[0] != stamp:
        try:
            import yaml
            parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — unknown, never an error here
            parsed = None
        hit = _config_cache[str(path)] = (stamp, parsed)
    return hit[1]


def read_profile_env_names(home: Path) -> dict | None:
    """ONE ask of the profile's `.env` (through Hermes where the profile is Hermes's: a subprocess, so it is asked
    once and cached): the CDP names, EMPORIA_DESKTOP_TOOLS and HERMES_CUA_DRIVER_CMD, by name. What comes back is
    reduced at once: the CDP names to a yes (a URL may carry a credential and is never kept); the other two are
    a list of tool names and a path. ``None`` when it could not be asked."""
    try:
        from emporia import profile_env as pe
        got = pe.read_names(Path(home) / ".env", [*CDP_ENV_NAMES, EXTRA_SETTING, "HERMES_CUA_DRIVER_CMD"])
        return {"held": [n for n in CDP_ENV_NAMES if str(got.get(n) or "").strip()],
                "extra": str(got.get(EXTRA_SETTING) or ""), "cua": str(got.get("HERMES_CUA_DRIVER_CMD") or "")}
    except Exception:  # noqa: BLE001 — Hermes did not answer: the caller treats the env as unknown
        return None


_env_names = read_profile_env_names
_env_cache: dict[str, tuple[Any, float, dict | None]] = {}
ENV_TTL = 300.0           # a good answer is reused this long when neither file changed
ENV_FAIL_TTL = 60.0       # a failed ask is not repeated for this long (each one can cost a subprocess timeout)


def _stamp(home: Path) -> tuple:
    out = []
    for name in (".env", "config.yaml"):
        try:
            out.append((home / name).stat().st_mtime)
        except OSError:
            out.append(None)
    return tuple(out)


def environment(profile: Path | str | None = None) -> tuple[dict[str, str], bool]:
    """(env, known): what detection reads of the environment, and whether the profile's `.env` could be asked.
    The process's own CDP names and settings, plus the profile `.env`'s (one cached ask: an MCP server's own
    environment is filtered by Hermes, so the process alone would miss them). With no profile found there is
    nothing to ask, which is known: the process environment is all there is."""
    env = {k: v for k, v in os.environ.items() if k in CDP_ENV_NAMES + (EXTRA_SETTING, "HERMES_CUA_DRIVER_CMD",
                                                                      "HERMES_HOME", "HERMES_RUNTIME_DIR")}
    home = profile_home(profile)
    if home is None:
        return env, True
    env["HERMES_HOME"] = str(home)
    stamp, now = _stamp(home), time.monotonic()
    hit = _env_cache.get(str(home))
    if hit is not None and hit[0] == stamp and now - hit[1] < (ENV_TTL if hit[2] is not None else ENV_FAIL_TTL):
        info = hit[2]
    else:
        info = _env_names(home)
        _env_cache[str(home)] = (stamp, now, info)
    if info is None:
        return env, False
    for name in info["held"]:
        env[name] = "set"
    if info["extra"]:
        env[EXTRA_SETTING] = (env.get(EXTRA_SETTING, "") + "," + info["extra"]).strip(",")
    if info["cua"]:
        env["HERMES_CUA_DRIVER_CMD"] = info["cua"]
    return env, True


def profile_env(profile: Path | str | None = None) -> dict[str, str]:
    return environment(profile)[0]


def detect_profile(config: Any, profile: Path | str | None = None) -> dict:
    """`detect` with the profile's environment (and whether it could be read)."""
    env, known = environment(profile)
    return detect(config, env, env_known=known)


def detection(profile: Path | str | None = None) -> dict:
    return detect_profile(read_config(profile), profile)


def refusal(what: str, profile: Path | str | None = None) -> str:
    """"" when the profile has no desktop control (or it is unknown); else the plain refusal for `what`."""
    found = detection(profile)
    if found["tools"]:
        return REFUSED.format(what=what) + f" (on here: {', '.join(found['tools'])})"
    return ""


def declared_refusal(what: str, settings: Mapping[str, str]) -> str:
    """For an agent Hermes does not run (a key directory made by `agent init`: no profile to read): refuses only
    when its own settings name desktop tools in `EMPORIA_DESKTOP_TOOLS`."""
    names = extra_names(settings.get(EXTRA_SETTING))
    return REFUSED.format(what=what) + f" (on here: {', '.join(names)})" if names else ""


def doctor_line(found: dict | None = None) -> tuple[str, str]:
    """(level, text) for doctor and setup's closing block: level "ok", "warn" (unknown) or "danger"."""
    found = found if found is not None else detection()
    if not found["known"]:
        return "warn", ("desktop control: unknown (this profile's config.yaml could not be read); "
                        + WALLET_WARNING)
    if not found["tools"]:
        if not found.get("env_known", True):
            return "warn", ("desktop control: none seen, but the profile's .env could not be read, so a real "
                            "browser (BROWSER_CDP_URL, BU_CDP_WS, BU_CDP_URL) and EMPORIA_DESKTOP_TOOLS could not "
                            "be checked; " + WALLET_WARNING)
        return "ok", f"desktop control: none on; {WALLET_WARNING}"
    return "danger", (f"desktop control on here: {', '.join(found['tools'])}. Refused now: the money feature, "
                      f"payments, sends and signs from the agent's own wallet, and ERC-8004 `--browser`. "
                      f"{WALLET_WARNING[0].upper() + WALLET_WARNING[1:]}")
