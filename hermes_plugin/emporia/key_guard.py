"""Keep Emporia's keys away from the agent's own tools (P167).

Emporia's MCP tools sign with the agent's keys and return only signatures; no
tool ever gives a key to the model. The way a key could still reach the model
is the agent's *other* tools — its terminal, file reading, code execution —
which on Hermes's `local` terminal backend run on the same disk as the keys.

Two Hermes hooks narrow that:

* ``pre_tool_call`` vetoes a call whose command, path or code names the keys
  folder, a key file, a key variable, or a password-manager session token,
  or reads another process's environment (``/proc/<pid>/environ``, ``ps e``),
  where Emporia's processes are handed their keys. Only what a tool *reads or runs* is inspected (command, path, code), never
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
    r"|\.x25519\.retired\b"                          # P271: encryption keys it replaced
    r"|relay_operator\.secp256k1\b|_relay_jwt_secret\b"
    r"|\.env-backups\b"
    r"|\.emporia-signer\b|\bEMPORIA_SIGNER_DIR\b"       # P203: the signer's keys and limits
    r"|\bprofiles/\.signer\b"                              # P206: and its default folder beside Hermes's
    # Review pass 2: the signer's files by their own names, wherever EMPORIA_SIGNER_DIR put
    # the folder (a signer run as the agents' user, outside the default folders above)
    r"|\badmin\.token\b|\bbackends/[\w-]+\.secret\b|\bagents/[\w-]+/keys\b"
    # P206: its code, called directly — run as the agents' user, it would
    # approve, change limits or mint tokens without an `emporia signer` command.
    # Its source is no secret: reading or linting signer.py is fine; running it is not.
    r"|\bimport\s+emporia\.signer\b|\bfrom\s+emporia(?:\.signer)?\s+import\s+[\w\s,]*\b(?:signer|decide|save_policy"
    r"|new_agent_token|admin_token)\b|-m\s+emporia\.signer\b"
    # P293: its back ends' code (credentials, keys in KMS or a provider) is the signer's too
    r"|\bemporia\.signer_backends\b|\bfrom\s+emporia\s+import\s+[\w\s,]*\bsigner_backends\b"
    # Security review pass 2 (P1): the operator's verdict key (signer folder, operator/verdict.priv —
    # `.priv` above) and its code, called directly: it signs the verdicts that award points
    r"|\boperator/verdict\.priv\b|\bemporia\.operator_verdicts\b"
    r"|\bfrom\s+emporia\s+import\s+[\w\s,]*\boperator_verdicts\b"
    # and a MetaMask Agent Wallet back end's: the signer user's `mm` session (~/.metamask)
    # and the `mm` commands that sign, move money or change its wallets and policy
    # (review pass 2: also mid-command, `tar … ~/.metamask && …`, and `mm` run through npx)
    r"|\.metamask/|\.metamask\b(?![\w.-])"
    r"|(?:\bmm|@metamask/agent-wallet\S*)\s+(?:wallet\s+(?:sign-typed-data|sign-message|send-transaction|select|create|policy|trading-mode"
    r"|password)|login|logout|init|reset|transfer|swap|perps|predict|earn)\b"
    # P298: the dashboard's console holds every local profile's keys behind a Unix
    # socket the folder alone guards; through it one agent reads another's inbox and
    # has its DMs opened. Its socket and folder, and starting one or loading its key
    # discovery directly, are the keys by another name.
    r"|\bconsole\.sock\b|\bemporia-console\b|\bplugin-data/emporia/console\b"
    r"|-m\s+emporia\.console\b|\bfrom\s+emporia\.console\b|\bimport\s+emporia\.console\b")
# Catalog rule 11: Emporia's processes get their settings and keys in their environment,
# through Hermes (an MCP server's `${NAME}` env, the bridge's from the gateway's scope). A
# process's environment is readable by its own user: /proc/<pid>/environ (Hermes's file tool
# refuses it; the agent's terminal and code would not) and `ps e` / `ps eww` / `ps auxe`
# (BSD-style options with `e`, as the first word or alone; `ps -e` lists processes, no env).
_ENV_READS = re.compile(
    r"/proc/[^\n]*?/environ\b"                                   # any pid: 123, self, *, $(pgrep …)
    r"|\bps\s+[A-Za-z]*e[A-Za-z]*(?=$|[\s|;&)])"
    r"|\bps\b[^|;&\n]*\se(?=$|[\s|;&)])"
    r"|['\"]ps['\"]\s*,\s*['\"][A-Za-z]*e[A-Za-z]*['\"]")         # ['ps', 'eww'] from code
_KEY_NAMES = re.compile(
    r"\bEMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY\b"
    r"|\bEMPORIA_X402_PHRASE_SECRET\b|\bEMPORIA_ENCRYPTION_RETIRED_KEYS\b"
    r"|\bOP_SERVICE_ACCOUNT_TOKEN\b|\bBW_SESSION\b"
    # the tokens that unlock a remote vault holding a linked key
    r"|\bVAULT_TOKEN\b|\bDOPPLER_TOKEN\b|\bINFISICAL_TOKEN\b|\bAZURE_CLIENT_SECRET\b"
    r"|\bEMPORIA_SIGNER_TOKEN\b"
    r"|\bMM_CLI_TOKEN\b|\bMM_MNEMONIC\b|\bMM_PASSWORD\b")        # MetaMask's mm: its session token, a phrase
# P170/P173: `emporia` commands that change an agent's keys, wallets, limits or
# role. An agent may run them to help its operator, but only with the
# operator's yes: a developer agent must not, say, hand a marketplace agent back
# its terminal. Matched by the words a command contains, not their order —
# argparse takes options anywhere — and however emporia is started.
_EMPORIA_CLI = re.compile(r"(?:\bemporia\b(?![._/-])|\bemporia\.cli\b|\bemporia/cli\.py\b)")
_SENSITIVE = (
    ("setup", {"agent", "relay", "stripe", "dashboard",
               "hermes"}),                                 # P295: `hermes emporia setup` — profiles, roles, keys
    ("update", {"emporia", "hermes"}),                     # P295: new code for the runtime and the plugin
    ("role", {"marketplace", "developer", "both"}),
    ("identity", {"store", "link"}),
    ("wallet", {"create", "import", "store", "register", "limits"}),
    ("relay", {"use", "move", "backup", "link", "unlink", "access", "members", "config"}),
    ("visits", {"policy", "allow", "deny", "forget"}),   # P188: where an agent may go
    ("dms", {"on", "off", "unblock"}),                     # P185: DMs from other relays; P273: lifting a block
    ("agent", {"leave", "refresh", "playbook"}),           # P195: leaving a relay; P224: refresh; P297: its bounds
)


# P175/P177: commands only a person runs, in their own terminal — never
# approvable from an agent's tools: `wallet backup` shows the recovery phrase,
# `wallet mainnet` lets an agent pay real money, `relay host` (P183) opens a
# relay to other machines.
_PERSON_ONLY = (("wallet", {"backup", "mainnet"}), ("relay", {"host", "rotate", "remove"}), ("agent", {"delete", "rotate", "recovery", "recover"}),
                # P289: a bug verdict gives points, an incident is on the record for good — a person's
                ("bugs", {"verdict", "incident", "fixed"}),
                # Operator section: so does adopting an idea (the launch additions) — the operator's verdict
                ("ideas", {"adopt", "noted", "duplicate"}),
                # Labs: making a lab is a person's (the repo's proof file names them)
                ("lab", {"create"}),
                # Security review pass 2 (P1): the verdict key that signs both — made and used by a person
                ("operator", {"verdict"}),
                # P290: the guardians of a relay's key are people, not agents
                ("guardians", {"key", "set", "propose", "sign", "apply"}),
                # P203: the signer is what an agent cannot change — its keys, limits and approvals
                ("signer", {"serve", "start", "stop", "host", "add", "remove", "policy", "pending", "approve", "deny",
                            "alerts",   # P293: where its alerts go is its person's too
                            # P293 (phase 2): where its keys are kept, and their backup
                            "backend", "backup", "restore"}))
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


# Operator section: the relay operator's controls — the house agent's runner, a copy of the
# relay's database, this week's expedition. An agent may run them for its person, with the
# person's yes each time: nothing about the relay its person runs changes silently.
_RELAY_CONTROLS = (("house", {"run", "start", "stop"}), ("operator", {"snapshot", "expedition"}))


def _relay_control(text: str) -> bool:
    return _cli_matches(text, _RELAY_CONTROLS)
_SECRET_NAME = (r"(EMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY"
                r"|EMPORIA_X402_PHRASE_SECRET|OP_SERVICE_ACCOUNT_TOKEN|BW_SESSION"
                r"|VAULT_TOKEN|DOPPLER_TOKEN|INFISICAL_TOKEN|AZURE_CLIENT_SECRET|EMPORIA_SIGNER_TOKEN"
                r"|MM_CLI_TOKEN|MM_MNEMONIC|MM_PASSWORD)")
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
               "relay host` opens a relay to other machines, a verdict (and the operator's verdict key) "
               "awards points. Tell your operator; never ask for the phrase.")
HOME_RELAY = ("Blocked by Emporia: only a person changes which relay this agent lives on "
              "(`emporia relay use URL NAME`). To act on another relay, use visit_relay, which asks.")
_HOME_SETTING = re.compile(r"\bEMPORIA_RELAY_URL\b")
_CONFIG_FILE = re.compile(r"(?:^|[/\s'\"])(?:config\.ya?ml|\.env)\b|\bhermes\b[^\n]*\bconfig\b")


# Review pass 2: the settings behind `emporia wallet mainnet` (person-only) and `wallet
# limits` / `agent playbook` (the person's yes), written straight into the agent's own
# config — `hermes config set NAME VALUE`, a line added to its .env, a key in config.yaml —
# which passed with no word from the person.
_MAINNET_SETTING = ("EMPORIA_X402_MAINNET",)
_LIMIT_SETTINGS = ("EMPORIA_X402_PAY_NETWORKS", "EMPORIA_X402_MAX_PAYMENT_CENTS", "EMPORIA_X402_MAX_DAILY_CENTS",
                   "EMPORIA_PAYMENT_APPROVAL_CENTS", "EMPORIA_SIGN_LIMITS", "EMPORIA_PAYOUT_ADDRESSES",
                   "EMPORIA_PLAYBOOK")


def _setting_written(names: tuple[str, ...]):
    alt = "|".join(names)
    return re.compile(rf"\b(?:{alt})['\"]?\s*[=:]|\bconfig\s+set\b[^\n]*\b(?:{alt})\b")


_MAINNET_WRITE, _LIMITS_WRITE = _setting_written(_MAINNET_SETTING), _setting_written(_LIMIT_SETTINGS)
# Which tools a turn started by another agent may use is its person's to say (relay_turn.py).
_TURN_TOOLS_WRITE = _setting_written(("EMPORIA_RELAY_TURN_TOOLS",))
# Security review pass 2 (P1): which key a relay takes verdicts from is its person's to say —
# assigned in any command, or written into a config file, it is refused outright.
_VERDICT_KEY_WRITE = _setting_written(("EMPORIA_OPERATOR_VERDICT_KEY",))


def _writes_verdict_key(args: dict) -> bool:
    return _writes_setting(args, _VERDICT_KEY_WRITE) or any(
        _VERDICT_KEY_WRITE.search(t) for key in ("command", "cmd", "code", "script") for t in _strings(args.get(key)))


def _writes_setting(args: dict, pattern: re.Pattern) -> bool:
    """A setting assigned (or `config set`) in a call that names the agent's config: its
    .env, a config.yaml, or a `hermes … config` command."""
    named = [t for key in ("path", "file_path", "command", "cmd", "code", "script") for t in _strings(args.get(key))]
    return any(pattern.search(t) for t in _strings(args)) and any(_CONFIG_FILE.search(t) for t in named)


def _changes_home_relay(args: dict) -> bool:
    """P188: a write to its own config.yaml or .env naming its relay, or a
    command doing the same — the agent re-pointing its home."""
    named = [t for key in ("path", "file_path", "command", "cmd", "code", "script") for t in _strings(args.get(key))]
    written = [t for t in _strings(args) if _HOME_SETTING.search(t)]
    return bool(written) and any(_CONFIG_FILE.search(t) for t in named)


TURN_TOOLS = ("Blocked by Emporia: only a person decides which tools a turn started by another agent may use "
              "(EMPORIA_RELAY_TURN_TOOLS in this profile's .env). Tell your operator.")
RELAY_OPEN = ("Blocked by Emporia: only a person opens a relay to other machines (`emporia relay host`, in "
              "their own terminal). The relay on this machine listens on 127.0.0.1.")
# The block the CLI prints around a recovery phrase (cli.PHRASE_BEGIN / PHRASE_END).
_PHRASE_BLOCK = re.compile(r"──── RECOVERY PHRASE ────.*?(?:──── END OF RECOVERY PHRASE ────|\Z)", re.S)


# P6 (security review pass 2): a path is matched as it would resolve, not only as written —
# `~/.hermes//keys`, `~/.hermes/./keys`, `a/../.hermes/keys`, and a relative path after
# `cd DIR &&` (or the terminal tool's `workdir`). Cheap, and it adds no false positive: each
# variant is a spelling of a path the command really names.
_SLASHES = re.compile(r"/(?:\.?/)+")
_DOTDOT = re.compile(r"(?<![^/\s'\"=:])(?!\.\.?/)[^/\s'\"]+/\.\./")
_CD = re.compile(r"(?:^|[;&|(\n]\s*)(?:cd|pushd)\s+(['\"]?)([^\s;&|'\")]+)\1\s*(?:&&|;|\n)")
_TOKEN = re.compile(r"(?<=[\s=<>'\"])(?![-/~$])([\w.@-][^\s;&|'\"<>]*)")


def _normal(text: str) -> str:
    text = _SLASHES.sub("/", text)
    for _ in range(8):                      # a/b/../../c: one level a pass
        resolved = _DOTDOT.sub("", text)
        if resolved == text:
            break
        text = resolved
    return text


def _variants(text: str, workdir: str = ""):
    """`text`, normalised, and each relative word after a `cd DIR` (or in `workdir`) as DIR/word."""
    yield text
    norm = _normal(text)
    if norm != text:
        yield norm
    bases = [(m.group(2), m.end()) for m in _CD.finditer(norm)]
    if workdir:
        bases.insert(0, (workdir, 0))
    for base, start in bases:
        base = base.rstrip("/")
        rest = " " + norm[start:]
        yield _normal(_TOKEN.sub(lambda m: f"{base}/{m.group(1)}", rest))


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
RELAY_CONTROL = ("Emporia: the agent wants to run one of the relay operator's controls (the house agent, "
                 "a copy of the relay's database, this week's expedition). Approve only if you asked for it.")


def pre_tool_call(tool_name: str = "", args: dict | None = None, **_: Any) -> dict | None:
    """Veto a tool call that reads or runs something naming Emporia's keys, and
    put `emporia` commands that change keys, wallets, limits or roles to the
    operator (Hermes's approval prompt — the agent cannot answer it)."""
    if not isinstance(args, dict):
        return None
    workdir = next((w for k in ("workdir", "cwd") for w in _strings(args.get(k))), "")
    for key in _READ_ARGS:
        for raw in _strings(args.get(key)):
            for text in _variants(raw, workdir):
                if _KEY_PATHS.search(text) or _KEY_NAMES.search(text) or _ENV_READS.search(text):
                    return {"action": "block", "message": BLOCKED}
    if any(_RELAY_SETTING.search(text) for text in _strings(args)) or any(
            _RELAY_FOLDER.search(text) for key in _READ_ARGS for raw in _strings(args.get(key))
            for text in _variants(raw, workdir)):
        return {"action": "block", "message": RELAY_OPEN}
    if _changes_home_relay(args):
        return {"action": "block", "message": HOME_RELAY}
    if _writes_setting(args, _MAINNET_WRITE) or _writes_verdict_key(args):
        return {"action": "block", "message": PERSON_ONLY}
    if _writes_setting(args, _TURN_TOOLS_WRITE):
        return {"action": "block", "message": TURN_TOOLS}
    if _writes_setting(args, _LIMITS_WRITE):
        import hashlib
        digest = hashlib.sha256(repr(sorted((k, repr(v)) for k, v in args.items())).encode()).hexdigest()[:16]
        return {"action": "approve", "message": APPROVE, "rule_key": f"emporia-setting-{digest}"}
    for key in ("command", "cmd", "code", "script"):
        for text in _strings(args.get(key)):
            if _OPENS_RELAY.search(text):
                return {"action": "block", "message": RELAY_OPEN}
            if _cli_matches(text, _PERSON_ONLY):
                return {"action": "block", "message": PERSON_ONLY}
            sensitive, control = _sensitive_cli(text), _relay_control(text)
            if sensitive or control:
                # P173: "always" covers this exact command only — one approval of
                # a harmless change must not approve every later one.
                import hashlib
                digest = hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:16]
                return {"action": "approve", "message": APPROVE if sensitive else RELAY_CONTROL,
                        "rule_key": f"emporia-cli-{digest}"}
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
