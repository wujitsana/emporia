"""Which Hermes runs here, and whether it is new enough to see Emporia in its plugin catalog.

How the version is read: Hermes's own public command, `hermes --version`, whose first line is
`Hermes Agent v0.23.0 (2026.9.24) · upstream 2bd378ed` and whose `Install method:` line says how it was
installed (the runtime never imports Hermes: it runs in its own interpreter). The upgrade step is the one
Hermes itself recommends for that install method (`hermes_cli.config.recommended_update_command_for_method`):
`docker pull nousresearch/hermes-agent:latest` for the Docker image, `pkg upgrade hermes-agent` on Termux,
`hermes update` otherwise. Hermes v0.23.0 and older cannot list Emporia in the catalog (its index passed the
2 MiB cap, fixed in v0.23.1); the URL install form works on any version.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

#: The first Hermes whose catalog lists Emporia (a visibility floor for the catalog install, not what the code needs:
#: `requires_hermes` in plugin.yaml, ">=0.23.0", is what the code needs, and the URL install works there).
MIN_VERSION = (0, 23, 1)
#: The first Hermes that reads `lazy`, `idle_timeout_seconds` and `max_lifetime_seconds` on an `mcp_servers` entry.
LAZY_MCP_MIN = (0, 23, 2)
_VERSION = re.compile(r"\bv?(\d+)\.(\d+)\.(\d+)\b")
URL_INSTALL = 'hermes plugins install "https://github.com/wujitsana/emporia#hermes_plugin/emporia" --enable'


def parse(output: str) -> tuple[tuple[int, int, int] | None, str]:
    """(version, install method) from `hermes --version` output; None / "" where it says nothing."""
    first = (output or "").strip().splitlines()[0] if (output or "").strip() else ""
    m = _VERSION.search(first)
    method = next((ln.split(":", 1)[1].strip().lower() for ln in (output or "").splitlines()
                   if ln.lower().startswith("install method:")), "")
    return ((int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None), method


def read(run=subprocess.run) -> tuple[tuple[int, int, int] | None, str]:
    """Ask Hermes (None when it is not on PATH, does not answer, or says nothing parseable)."""
    exe = os.getenv("EMPORIA_HERMES_BIN", "").strip() or shutil.which("hermes")
    if not exe:
        return None, ""
    try:
        out = run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return parse(out.stdout or "")


def supports_lazy_mcp(version: tuple[int, int, int] | None) -> bool:
    """Whether this Hermes version starts an MCP server on first use and stops it when idle (unknown: no)."""
    return version is not None and version >= LAZY_MCP_MIN


def upgrade_step(method: str) -> str:
    """The step that upgrades Hermes for this install method, as Hermes recommends it."""
    if method == "docker":
        return ("docker pull nousresearch/hermes-agent:latest, then restart whatever runs the container "
                "(`hermes update` does not apply inside Docker)")
    if method == "apt":
        return "pkg upgrade hermes-agent"
    if method in ("nix", "nixos", "home-manager"):
        return "update Hermes through the Nix source that installed it (nix profile upgrade, or your flake input and a rebuild)"
    return "hermes update"


def problem(version: tuple[int, int, int] | None, method: str) -> tuple[str, str]:
    """(what is wrong, the fix) when this Hermes is too old; ("", "") when it is new enough or unknown."""
    if version is None or version >= MIN_VERSION:
        return "", ""
    have = ".".join(map(str, version))
    want = ".".join(map(str, MIN_VERSION))
    return (f"Hermes v{have} is older than v{want}: it cannot see Emporia in its plugin catalog "
            "(`hermes plugins install emporia` finds nothing)",
            f"Upgrade: {upgrade_step(method)}. Until then, install from the address: {URL_INSTALL}")


def check() -> tuple[str, str, str]:
    """(state, what, fix): "ok" (new enough; `what` names it), "old" (what is wrong and the fix), or
    "unknown" (no Hermes found, or it said nothing parseable)."""
    version, method = read()
    if version is None:
        return "unknown", "", ""
    what, fix = problem(version, method)
    if what:
        return "old", what, fix
    return "ok", f"Hermes v{'.'.join(map(str, version))}", ""
