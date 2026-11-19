"""No crypto in a profile whose agent can control the desktop or a real browser.

A browser wallet may hold mainnet funds even though Emporia is testnet only, and an agent that can click can
press "Sign" in it, or open a desktop terminal (which carries no Hermes session marker) and run a person-only
command. So where the profile gives the agent that control, Emporia does no crypto of its own, with no override:
turning on the `money` feature, the payment paths (`agent_wallet.sign_exact`, `sign_entry`, `pay_tempo`,
`sign_escrow`, `reclaim_escrow`: every MCP payment goes through one of them), anything signed or sent from the
agent's own wallet (`chain_link.person_only`), and `--browser` for ERC-8004 (`agent_card_cli`). `--signature`
(signed on another device) and read-only views stay.

Desktop control, for a profile (the plugin's `desktop_guard.detect` reads the same; a test holds them equal):

* the `computer_use` toolset is on (platform_toolsets, or the top-level `toolsets`);
* the `browser-cdp` toolset is on (it attaches to a running Chrome);
* the `browser` toolset is on (named, or Hermes's default set when no list is saved) AND Hermes's browser is a real
  one: `browser.use_real_profile: true`, or a `browser.cdp_url` (or `BROWSER_CDP_URL`). A plain sandboxed browser is
  not desktop control.

`agent.disabled_toolsets` removes a name. The profile's config.yaml is read with `yaml.safe_load`, read-only; an
unreadable or missing one is *unknown*, which never blocks anything (doctor says so). Which profile: `HERMES_HOME`
when it holds a config.yaml, else the profile holding this checkout, else none (unknown).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

COMPUTER_TOOLS = ("computer_use",)
CDP_TOOLS = ("browser_cdp", "browser_dialog")
BROWSER_TOOLS = CDP_TOOLS + ("browser_exec", "browser_vault")

REFUSED = ("this profile lets the agent control the desktop or a real browser, so Emporia does no crypto here "
           "({what}); turn those tools off for this profile, or link 8004 with `--signature` signed on another "
           "device or hardware wallet")
WALLET_WARNING = ("a wallet with real funds should never sit in a browser or desktop the agent can control")


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def detect(config: Any, env: Mapping[str, str] | None = None) -> dict:
    """What a profile's config says: ``{"known": bool, "tools": [names], "real_browser": bool, "why": [lines]}``.
    ``config`` None (unreadable) or not a mapping is unknown: no tools, ``known`` False."""
    if not isinstance(config, dict):
        return {"known": False, "tools": [], "real_browser": False, "why": []}
    on: set[str] = set()
    explicit = False
    platform_toolsets = config.get("platform_toolsets")
    if isinstance(platform_toolsets, dict):
        for value in platform_toolsets.values():
            explicit = explicit or isinstance(value, (list, tuple))
            on.update(_names(value))
    on.update(_names(config.get("toolsets")))
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    off = set(_names(agent.get("disabled_toolsets")))
    composite = not explicit or any(n.startswith("hermes") for n in on)   # a composite or no saved list: Hermes's default set
    browser_on = ("browser" in on or composite) and "browser" not in off
    browser = config.get("browser") if isinstance(config.get("browser"), dict) else {}
    cdp_url = str(browser.get("cdp_url") or "").strip() or str((env if env is not None else os.environ)
                                                                .get("BROWSER_CDP_URL") or "").strip()
    profile_real = _truthy(browser.get("use_real_profile"))
    real = profile_real or bool(cdp_url)
    tools: list[str] = []
    why: list[str] = []
    if "computer_use" in on and "computer_use" not in off:
        tools.append("computer_use")
        why.append("the computer_use toolset is on (the agent can click and type on the desktop)")
    if "browser-cdp" in on and "browser-cdp" not in off:
        tools += [t for t in CDP_TOOLS if t not in tools]
        why.append("the browser-cdp toolset is on (it attaches to a running Chrome)")
    if browser_on and real:
        tools += [t for t in BROWSER_TOOLS if t not in tools]
        why.append("the browser toolset is on and Hermes's browser is a real one "
                   + ("(browser.use_real_profile)" if profile_real else "(a cdp_url)"))
    return {"known": True, "tools": tools, "real_browser": real, "why": why}


def profile_home(profile: Path | str | None = None) -> Path | None:
    """The profile whose config is read: the one given, else HERMES_HOME (with a config.yaml), else the profile
    holding this checkout, else None."""
    if profile:
        return Path(profile)
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


def detection(profile: Path | str | None = None) -> dict:
    return detect(read_config(profile))


def refusal(what: str, profile: Path | str | None = None) -> str:
    """"" when the profile has no desktop control (or it is unknown); else the plain refusal for `what`."""
    found = detection(profile)
    if found["tools"]:
        return REFUSED.format(what=what) + f" (on here: {', '.join(found['tools'])})"
    return ""


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
