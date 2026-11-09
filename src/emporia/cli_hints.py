"""The `emporia …` commands a hint names, as this install runs them.

A checkout puts `emporia` on PATH. The Hermes plugin (`hermes emporia setup`) installs the
runtime into a profile's `plugin-data/emporia/runtime` and puts nothing on PATH: there the same
commands are `hermes emporia …`, which passes them through to this runtime (found by the
end-to-end run: doctor and the tools' refusals named commands a plugin-only user could not run).
Hints are written for the `emporia` CLI and pass through `localize` where they are shown.
"""
from __future__ import annotations

import re
from pathlib import Path

_RUNTIME = Path(__file__).resolve().parents[2]          # <runtime>/src/emporia/cli_hints.py


def via_plugin(runtime: Path = _RUNTIME) -> bool:
    """This runtime came with the Hermes plugin (it sits in a profile's plugin-data)."""
    return (runtime.name == "runtime" and runtime.parent.name == "emporia"
            and runtime.parent.parent.name == "plugin-data")


# The CLI's commands (tests keep this in step with emporia.cli).
COMMANDS = ("agent", "archive", "delete", "dms", "doctor", "features", "guardians", "house", "identity", "lab", "log",
            "operator", "peek", "relay", "role", "rounds", "setup", "sheet", "signer", "skill-lock", "stats", "update", "visits",
            "wake", "wallet", "welcome")
# `--profile NAME` never reaches the runtime through `hermes emporia …`: Hermes takes it, anywhere
# on its command line, as its own profile switch. So a hint naming one becomes `hermes -p NAME …`.
_COMMAND = re.compile(r"(?<![\w./-])emporia (?:setup agent(?: (?P<agent>[\w-]+))?\b"
                      r"|(?P<scoped>doctor) --profile (?P<doc>[\w-]+)"
                      r"|(?:" + "|".join(COMMANDS) + r")\b)")
_LAUNCHED = re.compile(r"hermes(?: -p [\w-]+)? $")


def _plugin_form(m: re.Match) -> str:
    if m.group(0).startswith("emporia setup agent"):       # the plugin's setup is the agent's
        return f"hermes emporia setup --agent-profile {m.group('agent') or 'NAME'}"
    if m.group("doc"):                                      # the plugin's doctor is this profile's
        return f"hermes -p {m.group('doc')} emporia {m.group('scoped')}"
    return "hermes " + m.group(0)


def localize(text: str, plugin: bool | None = None) -> str:
    """`text` with each `emporia COMMAND …` as this install runs it."""
    if not text or not (via_plugin() if plugin is None else plugin):
        return text
    return _COMMAND.sub(lambda m: m.group(0) if _LAUNCHED.search(text, 0, m.start()) else _plugin_form(m), text)


def command(rest: str) -> str:
    """`emporia REST`, as this install runs it."""
    return localize(f"emporia {rest}")
