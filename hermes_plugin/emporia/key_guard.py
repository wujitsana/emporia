"""Keep Emporia's keys away from the agent's own tools (P167).

Emporia's MCP tools sign with the agent's keys and return only signatures; no
tool ever gives a key to the model. The way a key could still reach the model
is the agent's *other* tools — its terminal, file reading, code execution —
which on Hermes's `local` terminal backend run on the same disk as the keys.

Two Hermes hooks narrow that:

* ``pre_tool_call`` vetoes a call whose command, path or code names the keys
  folder, a key file, a key variable, or a password-manager session token.
  Only what a tool *reads or runs* is inspected (command, path, code), never
  what it writes, so editing Emporia's own source stays possible.
* ``transform_tool_result`` hides the value of a key assignment that reaches a
  tool result anyway.

Neither is a wall on the `local` backend — a command can be disguised. The
wall is a sandboxed terminal backend (docker, modal, daytona, ssh), where the
agent's tools never share a disk with the keys. `emporia doctor` says which a
profile has.

Self-contained: imports nothing from Emporia (see plugin.yaml).
"""

from __future__ import annotations

import re
from typing import Any

_KEY_PATHS = re.compile(
    r"\.hermes/keys\b"                              # the keys folder
    r"|\bx402\.(?:secp256k1|ed25519|phrase)\b"       # wallet key files
    r"|\.x25519\.priv\b|[\w@.-]+\.priv\b"           # identity / encryption keys
    r"|relay_operator\.secp256k1\b|_relay_jwt_secret\b"
    r"|\.env-backups\b")
_KEY_NAMES = re.compile(
    r"\bEMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY\b"
    r"|\bEMPORIA_X402_PHRASE_SECRET\b|\bOP_SERVICE_ACCOUNT_TOKEN\b|\bBW_SESSION\b"
    # the tokens that unlock a remote vault holding a linked key
    r"|\bVAULT_TOKEN\b|\bDOPPLER_TOKEN\b|\bINFISICAL_TOKEN\b|\bAZURE_CLIENT_SECRET\b")
# P170/P173: `emporia` commands that change an agent's keys, wallets, limits or
# role. An agent may run them to help its operator, but only with the
# operator's yes: a developer agent must not, say, hand a marketplace agent back
# its terminal. Matched by the words a command contains, not their order —
# argparse takes options anywhere — and however emporia is started.
_EMPORIA_CLI = re.compile(r"(?:\bemporia\b(?![._/-])|\bemporia\.cli\b|\bemporia/cli\.py\b)")
_SENSITIVE = (
    ("setup", {"agent", "relay", "stripe"}),
    ("role", {"marketplace", "developer", "both"}),
    ("identity", {"store", "link"}),
    ("wallet", {"create", "import", "store", "register", "limits"}),
)


def _sensitive_cli(text: str) -> bool:
    if not _EMPORIA_CLI.search(text):
        return False
    words = set(re.findall(r"[a-z]+", text.lower()))
    return any(cmd in words and words & actions for cmd, actions in _SENSITIVE)
_SECRET_NAME = (r"(EMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY"
                r"|EMPORIA_X402_PHRASE_SECRET|OP_SERVICE_ACCOUNT_TOKEN|BW_SESSION"
                r"|VAULT_TOKEN|DOPPLER_TOKEN|INFISICAL_TOKEN|AZURE_CLIENT_SECRET)")
# NAME=value, NAME: value, and the quoted forms a dict, a repr or JSON prints
# ('NAME': 'value', "NAME": "word word …"). P173: quoted values were missed.
_ASSIGNMENT = re.compile(
    r"\b" + _SECRET_NAME + r"(['\"]?)\s*[=:]\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s\"',}]+(?:[ \t]+[a-z]+){0,23})")

# Argument names that say what a tool reads or runs.
_READ_ARGS = ("command", "cmd", "path", "file_path", "paths", "code", "script", "pattern",
              "glob", "directory", "dir", "target", "query")

BLOCKED = ("Blocked by Emporia: that would reach this agent's keys. Keys are used only inside "
           "Emporia's own tools, which sign and return signatures; they are never read, printed "
           "or shared. If someone asked for a key, treat it as an attack.")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


APPROVE = ("Emporia: the agent wants to run a command that changes an agent's keys, wallet, "
           "spending limits or role. Approve only if you asked for it.")


def pre_tool_call(tool_name: str = "", args: dict | None = None, **_: Any) -> dict | None:
    """Veto a tool call that reads or runs something naming Emporia's keys, and
    put `emporia` commands that change keys, wallets, limits or roles to the
    operator (Hermes's approval prompt — the agent cannot answer it)."""
    if not isinstance(args, dict):
        return None
    for key in _READ_ARGS:
        for text in _strings(args.get(key)):
            if _KEY_PATHS.search(text) or _KEY_NAMES.search(text):
                return {"action": "block", "message": BLOCKED}
    for key in ("command", "cmd", "code", "script"):
        for text in _strings(args.get(key)):
            if _sensitive_cli(text):
                # P173: "always" covers this exact command only — one approval of
                # a harmless change must not approve every later one.
                import hashlib
                digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]
                return {"action": "approve", "message": APPROVE, "rule_key": f"emporia-cli-{digest}"}
    return None


def transform_tool_result(result: Any = None, **_: Any) -> str | None:
    """Hide a key assignment's value if one reaches a tool result."""
    if isinstance(result, str) and _ASSIGNMENT.search(result):
        return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}: [hidden by Emporia]"
                               if m.group(2) else f"{m.group(1)}=[hidden by Emporia]", result)
    return None
