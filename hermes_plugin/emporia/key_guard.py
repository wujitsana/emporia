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
    r"|\.env-backups\b"
    r"|\.emporia-signer\b|\bEMPORIA_SIGNER_DIR\b"       # P203: the signer's keys and limits
    r"|\bprofiles/\.signer\b"                              # P206: and its default folder beside Hermes's
    # P206: its code, called directly — run as the agents' user, it would
    # approve, change limits or mint tokens without an `emporia signer` command.
    # Its source is no secret: reading or linting signer.py is fine; running it is not.
    r"|\bimport\s+emporia\.signer\b|\bfrom\s+emporia(?:\.signer)?\s+import\s+[\w\s,]*\b(?:signer|decide|save_policy"
    r"|new_agent_token|admin_token)\b|-m\s+emporia\.signer\b")
_KEY_NAMES = re.compile(
    r"\bEMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY\b"
    r"|\bEMPORIA_X402_PHRASE_SECRET\b|\bOP_SERVICE_ACCOUNT_TOKEN\b|\bBW_SESSION\b"
    # the tokens that unlock a remote vault holding a linked key
    r"|\bVAULT_TOKEN\b|\bDOPPLER_TOKEN\b|\bINFISICAL_TOKEN\b|\bAZURE_CLIENT_SECRET\b"
    r"|\bEMPORIA_SIGNER_TOKEN\b")
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
    ("relay", {"use", "move", "backup", "link", "unlink", "access", "members", "config"}),
    ("visits", {"policy", "allow", "deny", "forget"}),   # P188: where an agent may go
    ("dms", {"on", "off"}),                                # P185: DMs from other relays
    ("agent", {"leave"}),                                  # P195: leaving a relay
)


# P175/P177: commands only a person runs, in their own terminal — never
# approvable from an agent's tools: `wallet backup` shows the recovery phrase,
# `wallet mainnet` lets an agent pay real money, `relay host` (P183) opens a
# relay to other machines.
_PERSON_ONLY = (("wallet", {"backup", "mainnet"}), ("relay", {"host", "rotate", "remove"}), ("agent", {"delete", "rotate", "recovery", "recover"}),
                # P203: the signer is what an agent cannot change — its keys, limits and approvals
                ("signer", {"serve", "start", "stop", "host", "add", "remove", "policy", "pending", "approve", "deny"}))
# P183: other ways to open a relay to other machines — telling it to listen on
# another address (in a command or a file the agent writes), or starting the
# Docker relay or its system service. The relay itself refuses another address
# outside a container.
_NOT_LOCAL = r"(?!['\"]?(?:127\.0\.0\.1|localhost|::1)\b)"
_RELAY_SETTING = re.compile(r"\bEMPORIA_RELAY_HOST['\"]?\s*[=:]\s*" + _NOT_LOCAL
                            # P186: who may use the relay is the operator's to say
                            + r"|\bEMPORIA_RELAY_(?:ACCESS|MEMBERS)['\"]?\s*[=:]"
                            # P188: and where its agent may go
                            + r"|\bEMPORIA_VISIT_(?:POLICY|RELAYS|DENY)['\"]?\s*[=:]"
                            + r"|\bEMPORIA_BACKUP_RELAYS['\"]?\s*[=:]")
# Its folder (`relay host`'s default): its settings, including where its fee goes.
_RELAY_FOLDER = re.compile(r"\bemporia-relay/|\bemporia-relay\b\s*$")
_OPENS_RELAY = re.compile(
    r"\brelay_server\b[^\n]*--host[= ]\s*" + _NOT_LOCAL
    + r"|\b(?:docker|systemctl)\b[^\n]*\bemporia[-_]relay\b|\bemporia[-_]relay\b[^\n]*\b(?:docker|systemctl)\b")


def _cli_matches(text: str, rules) -> bool:
    if not _EMPORIA_CLI.search(text):
        return False
    words = set(re.findall(r"[a-z][a-z0-9_]*", text.lower()))   # EMPORIA_RELAY_HOST is one word
    return any(cmd in words and words & actions for cmd, actions in rules)


def _sensitive_cli(text: str) -> bool:
    return _cli_matches(text, _SENSITIVE)
_SECRET_NAME = (r"(EMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY"
                r"|EMPORIA_X402_PHRASE_SECRET|OP_SERVICE_ACCOUNT_TOKEN|BW_SESSION"
                r"|VAULT_TOKEN|DOPPLER_TOKEN|INFISICAL_TOKEN|AZURE_CLIENT_SECRET|EMPORIA_SIGNER_TOKEN)")
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


PERSON_ONLY = ("Blocked by Emporia: only a person runs this, in their own terminal — `emporia wallet "
               "backup` shows a recovery phrase, `emporia wallet mainnet` turns on real money, `emporia "
               "relay host` opens a relay to other machines. Tell your operator; never ask for the phrase.")
HOME_RELAY = ("Blocked by Emporia: only a person changes which relay this agent lives on "
              "(`emporia relay use URL NAME`). To act on another relay, use visit_relay, which asks.")
_HOME_SETTING = re.compile(r"\bEMPORIA_RELAY_URL\b")
_CONFIG_FILE = re.compile(r"(?:^|[/\s'\"])(?:config\.ya?ml|\.env)\b|\bhermes\b[^\n]*\bconfig\b")


def _changes_home_relay(args: dict) -> bool:
    """P188: a write to its own config.yaml or .env naming its relay, or a
    command doing the same — the agent re-pointing its home."""
    named = [t for key in ("path", "file_path", "command", "cmd", "code", "script") for t in _strings(args.get(key))]
    written = [t for t in _strings(args) if _HOME_SETTING.search(t)]
    return bool(written) and any(_CONFIG_FILE.search(t) for t in named)


RELAY_OPEN = ("Blocked by Emporia: only a person opens a relay to other machines (`emporia relay host`, in "
              "their own terminal). The relay on this machine listens on 127.0.0.1.")
# The block the CLI prints around a recovery phrase (cli.PHRASE_BEGIN / PHRASE_END).
_PHRASE_BLOCK = re.compile(r"──── RECOVERY PHRASE ────.*?(?:──── END OF RECOVERY PHRASE ────|\Z)", re.S)


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
    if any(_RELAY_SETTING.search(text) for text in _strings(args)) or any(
            _RELAY_FOLDER.search(text) for key in _READ_ARGS for text in _strings(args.get(key))):
        return {"action": "block", "message": RELAY_OPEN}
    if _changes_home_relay(args):
        return {"action": "block", "message": HOME_RELAY}
    for key in ("command", "cmd", "code", "script"):
        for text in _strings(args.get(key)):
            if _OPENS_RELAY.search(text):
                return {"action": "block", "message": RELAY_OPEN}
            if _cli_matches(text, _PERSON_ONLY):
                return {"action": "block", "message": PERSON_ONLY}
            if _sensitive_cli(text):
                # P173: "always" covers this exact command only — one approval of
                # a harmless change must not approve every later one.
                import hashlib
                digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]
                return {"action": "approve", "message": APPROVE, "rule_key": f"emporia-cli-{digest}"}
    return None


def transform_tool_result(result: Any = None, **_: Any) -> str | None:
    """Hide a key assignment's value, or a shown recovery phrase, if one
    reaches a tool result."""
    if not isinstance(result, str):
        return None
    out = _PHRASE_BLOCK.sub("[recovery phrase hidden by Emporia]", result)
    if _ASSIGNMENT.search(out):
        out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}: [hidden by Emporia]"
                              if m.group(2) else f"{m.group(1)}=[hidden by Emporia]", out)
    return out if out != result else None
