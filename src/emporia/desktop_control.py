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
  it, and it is not default-off) while the tool is usable, i.e. a cua-driver is present (`HERMES_CUA_DRIVER_CMD`,
  `cua-driver` on PATH, or Hermes's own install folder; a best-effort mirror of the tool's `check_fn`);
* the `browser-cdp` toolset (it attaches to a running Chrome);
* the `browser` toolset (named, or in the default set) AND a real browser: `browser.use_real_profile: true`, a
  `browser.cdp_url`, or `BROWSER_CDP_URL` / `BU_CDP_WS` / `BU_CDP_URL` (read from the profile's `.env` by name, as a
  yes or no: an MCP server's own environment is filtered, so the process environment alone would miss it). A plain
  sandboxed browser is not desktop control;
* an MCP server in `mcp_servers` whose name looks like a browser or desktop driver (browser, playwright, puppeteer,
  chrome, devtools, computer, desktop);
* any tool a person names in `EMPORIA_DESKTOP_TOOLS` (comma-separated names or prefixes).

The default set counts unless every platform in use has a saved list: `cli`, `cron`, `emporia` and any platform
named in `platform_toolsets` (a list-literal string such as "['a','b']" is a saved list, as Hermes reads it).
`agent.disabled_toolsets` removes a name. Not seen: a live `/browser connect` (the plugin's session guard asks
Hermes for that; a runtime run outside Hermes cannot).

The profile's config.yaml is read with `yaml.safe_load`, read-only; an unreadable or missing one is *unknown*, which
never blocks anything (doctor says so). Which profile: the one named by the caller, else `HERMES_HOME` when it holds
a config.yaml, else the profile holding this checkout, else none (unknown).
"""

from __future__ import annotations

import ast
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

COMPUTER_TOOLS = ("computer_use",)
CDP_TOOLS = ("browser_cdp", "browser_dialog")
BROWSER_TOOLS = CDP_TOOLS + ("browser_exec", "browser_vault")
#: Platforms Hermes serves an Emporia agent on: each needs a saved toolset list, or its default set counts.
PLATFORMS_IN_USE = ("cli", "cron", "emporia")
#: An MCP server whose name says it drives a browser or the desktop.
MCP_DESKTOP = re.compile(r"browser|playwright|puppeteer|chrome|devtools|computer|desktop", re.I)
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


def cua_driver_present(env: Mapping[str, str] | None = None) -> bool:
    """Whether a cua-driver is there for the `computer_use` tool: `HERMES_CUA_DRIVER_CMD`, else `cua-driver` on
    PATH, else a copy in Hermes's own install folder (a bounded look). Mirrors the tool's `check_fn` without
    importing Hermes; best effort, and a miss means "not seen", never an error."""
    env = os.environ if env is None else env
    cmd = str(env.get("HERMES_CUA_DRIVER_CMD") or "").strip()
    if cmd:
        return shutil.which(os.path.expanduser(cmd)) is not None
    if shutil.which("cua-driver"):
        return True
    roots: list[Path] = []
    home = str(env.get("HERMES_HOME") or "").strip()
    for base in ([Path(home)] if home else []) + [Path.home() / ".hermes"]:
        roots.append(base / "installs")
        for parent in base.parents:
            if parent.name == "profiles":
                roots.append(parent.parent / "installs")
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for here, dirs, files in os.walk(root):
            seen += 1
            if len(Path(here).parts) - base_depth >= 7 or seen > 4000:
                dirs[:] = []
            if any(f in ("cua-driver", "cua-driver.exe") for f in files):
                return True
    return False


def detect(config: Any, env: Mapping[str, str] | None = None, cua: bool | None = None) -> dict:
    """What a profile's config (and environment) says: ``{"known": bool, "tools": [names], "real_browser": bool,
    "why": [lines]}``. ``config`` None (unreadable) or not a mapping is unknown: no tools, ``known`` False.
    ``cua``: whether a cua-driver is present (None: looked up when needed)."""
    if not isinstance(config, dict):
        return {"known": False, "tools": [], "real_browser": False, "why": []}
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
    composite = any(p not in saved for p in PLATFORMS_IN_USE) or any(n.startswith("hermes") for n in on)
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
    return {"known": True, "tools": tools, "real_browser": real, "why": why}


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


def read_config(profile: Path | str | None = None) -> Any:
    """The profile's config.yaml as a dict, or None (unreadable, missing, not yaml)."""
    home = profile_home(profile)
    if home is None:
        return None
    try:
        import yaml
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — unknown, never an error here
        return None


def profile_env(profile: Path | str | None = None) -> dict[str, str]:
    """The environment detection reads: the process's own, plus, from the profile's `.env`, whether the CDP names
    are set (a yes, never the value: through Hermes where the profile is Hermes's) and EMPORIA_DESKTOP_TOOLS (a
    list of tool names). An MCP server's process environment is filtered by Hermes, so the `.env` is asked."""
    env = {k: v for k, v in os.environ.items() if k in CDP_ENV_NAMES + (EXTRA_SETTING, "HERMES_CUA_DRIVER_CMD",
                                                                      "HERMES_HOME")}
    home = profile_home(profile)
    if home is None:
        return env
    held, extra = _env_names(home)
    for name in held:
        env[name] = "set"
    if extra:
        env[EXTRA_SETTING] = (env.get(EXTRA_SETTING, "") + "," + extra).strip(",")
    return env


def read_profile_env_names(home: Path) -> tuple[list[str], str]:
    """From the profile's `.env`, by name only: which of the CDP names are set (a yes, never a value; asked of
    Hermes where the profile is Hermes's) and the value of EMPORIA_DESKTOP_TOOLS. ([], "") when it cannot be
    asked."""
    try:
        from emporia import profile_env as pe
        held = [n for n, yes in pe.present(Path(home) / ".env", list(CDP_ENV_NAMES)).items() if yes]
        extra = pe.read_names(Path(home) / ".env", [EXTRA_SETTING]).get(EXTRA_SETTING, "")
        return held, str(extra or "")
    except Exception:  # noqa: BLE001 — the .env could not be asked: what the config and process say stands
        return [], ""


_env_names = read_profile_env_names


def detection(profile: Path | str | None = None) -> dict:
    return detect(read_config(profile), profile_env(profile))


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
        return "ok", f"desktop control: none on; {WALLET_WARNING}"
    return "danger", (f"desktop control on here: {', '.join(found['tools'])}. Refused now: the money feature, "
                      f"payments, sends and signs from the agent's own wallet, and ERC-8004 `--browser`. "
                      f"{WALLET_WARNING[0].upper() + WALLET_WARNING[1:]}")
