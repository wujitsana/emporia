"""The `notice` an agent can pass on to its person when its plugin moved and its runtime did not.

`hermes plugins update emporia` (or Hermes's auto-update) moves the plugin; the runtime (this code) moves only on
the person's `hermes emporia update` (catalog rule 3: nothing installs code by itself). Until then the plugin
pins a commit this runtime is not at. The plugin says so in `hermes emporia status`, `doctor`, the gateway log
and the dashboard tab (`money_guard.stale_runtime_notice`); this is the same check from the runtime's side, for
`whats_next`, so the agent can tell its person in one line. Local files only: the plugin's pin and release in the
profile's `plugins/emporia/`, and this runtime's own git head. It fetches nothing and changes nothing.

It speaks only for a catalog runtime (one `hermes emporia setup` put under `plugin-data/emporia/runtime`, which
follows the plugin's pin). A development checkout is never "behind". A runtime NEWER than the plugin (a person
who updated the runtime first, or an older plugin on a newer runtime) is not behind either: the release the
plugin declares must be at least this runtime's.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

NOTICE_START = "Emporia's plugin moved to"


def _version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", (text or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _git_head(repo: Path) -> str:
    git = repo / ".git"
    if not git.is_dir():
        return ""
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(None, 1)[1]
        try:
            return (git / ref).read_text().strip()
        except OSError:
            for line in (git / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0]
    except OSError:
        pass
    return ""


def _plugin_release(plugin: Path) -> str:
    try:
        for line in (plugin / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def stale_notice(dotenv: str | None = None, runtime_root: Path | None = None) -> str:
    """The one line, or "" (not behind, not a catalog runtime, or anything unreadable). `dotenv` is the agent's
    EMPORIA_DOTENV (the plugin sets it); the plugin sits beside it in `plugins/emporia/`."""
    from emporia import __version__
    dotenv = (os.getenv("EMPORIA_DOTENV", "") if dotenv is None else dotenv).strip()
    if not dotenv:
        return ""
    try:
        root = runtime_root or Path(__file__).resolve().parents[2]
        if not (root.parent.name == "emporia" and root.parent.parent.name == "plugin-data"):
            return ""                                        # a checkout: never subject to the pin
        plugin = Path(dotenv).expanduser().parent / "plugins" / "emporia"
        pinned = str(json.loads((plugin / "emporia_source.json").read_text()).get("sha") or "")
        head = _git_head(root)
        release = _plugin_release(plugin)
        mine, theirs = _version(__version__), _version(release)
        if not pinned or not head or head == pinned or not mine or not theirs or theirs < mine:
            return ""
    except (OSError, ValueError, AttributeError, IndexError):
        return ""
    return (f"{NOTICE_START} {release} ({pinned[:12]}): run `hermes emporia update`, then restart the gateway "
            "(`hermes gateway restart`). Your person runs these; you cannot.")
