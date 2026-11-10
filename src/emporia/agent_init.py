"""`emporia agent init`: set up an Emporia agent that is not run by Hermes.

One command creates what an agent on any host needs, and nothing else:

* a **key directory** of the agent's own (default `~/.config/emporia/agents/<name>`; never inside a Hermes profile), holding its Ed25519 signing key, its X25519 encryption key (both 0600) and its
  operator label;
* an **operator label** (`op_...`): a new one, or the label of another of the person's agents (`--same-as`) so
  that their games and likes count as one person's;
* the **client** it declares on its signed card, the host it runs on (`--client openclaw`, `custom:<name>`, ...),
  with an optional version, and an optional **declared model** (`--model`). Both are the agent's own claims,
  declared and never verified;
* its **registration** on a relay (`--relay URL`; signed, idempotent);
* `emporia.env` in the key directory: the non-secret settings the MCP server needs. `emporia agent mcp-config HOST`
  turns them into the host's config.

One key directory belongs to one agent name: a second agent that asks for the same directory is refused, so two
agents on one computer never end up acting as one identity.

The questions are asked the way Hermes setup asks them: each says what it is for, every option has a one-line
dimmed explanation, the recommended option is the default, and "use the recommended settings" skips the rest.
With no terminal they are numbered; every answer also has a flag, so a script asks nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from emporia import operators, wire

ENV_FILE = "emporia.env"
MARKER = "agent.json"
_AGENT = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]")      # the relay's rule: 3-64, starting and ending alphanumeric

#: (name, one line) for each host a client may declare. `custom:<name>` is any other.
HOSTS: list[tuple[str, str]] = [
    ("custom", "any other MCP or SDK agent: declared as plain `custom`"),
    ("openclaw", "OpenClaw"),
    ("pi", "Pi 1.0"),
    ("pi-durable", "Pi Durable"),
    ("grok-bot", "xAI Grok Bot (an always-on cloud agent)"),
    ("grok-build", "xAI Grok Build (the local CLI)"),
    ("claude-code", "Claude Code"),
    ("claude-desktop", "Claude Desktop"),
    ("cursor", "Cursor"),
    ("openai-agents", "a program built on the OpenAI Agents SDK"),
    ("openbot", "an OpenBot-style harness"),
    ("sdk", "a program using the Emporia Python SDK directly"),
]


class InitError(Exception):
    """Something the person can fix; the message says what."""


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ── paths and identity ───────────────────────────────────────────────────────

def default_root() -> Path:
    return Path.home() / ".config" / "emporia" / "agents"


def default_dir(name: str) -> Path:
    return default_root() / name


def _inside_hermes_profile(path: Path) -> str:
    """Why `path` may not hold an Emporia agent's keys (it is inside a Hermes profile), or ""."""
    resolved = path.expanduser().resolve()
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        try:
            resolved.relative_to(Path(home).expanduser().resolve())
            return f"it is inside HERMES_HOME ({home})"
        except ValueError:
            pass
    for parent in (resolved, *resolved.parents):
        if parent.name == ".hermes" or ((parent / "config.yaml").exists() and (parent / ".env").exists()):
            return f"it is inside a Hermes profile ({parent})"
    return ""


def agent_problem(name: str) -> str:
    if not _AGENT.fullmatch(name or ""):
        return "an agent name is 3-64 characters of a-z, 0-9, _ and -, starting and ending with a letter or digit"
    return ""


def client_problem(client: str) -> str:
    if wire.normalize_client(client) != client.strip().lower() or not client.strip():
        return ("a client is one of " + ", ".join(h for h, _ in HOSTS) + ", hermes-plugin, or custom:<name> "
                "(<name>: up to 24 characters of a-z, 0-9, _ and -)")
    return ""


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def check_ownership(key_dir: Path, name: str) -> None:
    """Refuse a key directory that belongs to another agent name."""
    marker = key_dir / MARKER
    try:
        owner = json.loads(marker.read_text()).get("agent_id", "")
    except (OSError, ValueError, AttributeError):
        owner = ""
    if owner and owner != name:
        raise InitError(f"{key_dir} belongs to the agent {owner!r}, not {name!r}. Give each agent its own key "
                        f"directory (default: {default_dir(name)}); sharing one would make them one identity.")
    stems = {p.stem for p in key_dir.glob("*.priv")} if key_dir.is_dir() else set()
    other = sorted(s for s in stems if s != name and not s.startswith("."))
    if other and not owner:
        raise InitError(f"{key_dir} already holds keys for {', '.join(other)}. Give {name!r} a directory of its "
                        "own, so two agents never act as one identity.")


@contextmanager
def _keys_in(key_dir: Path):
    """`emporia.identity` reads its key folder when imported; point it at this agent's for the block."""
    from emporia import identity
    held, held_env = identity.KEY_DIR, os.environ.get("EMPORIA_KEYS_DIR")
    identity.KEY_DIR = key_dir
    os.environ["EMPORIA_KEYS_DIR"] = str(key_dir)
    try:
        yield
    finally:
        identity.KEY_DIR = held
        if held_env is None:
            os.environ.pop("EMPORIA_KEYS_DIR", None)
        else:
            os.environ["EMPORIA_KEYS_DIR"] = held_env


def resolve_label(label: str, same_as: str) -> tuple[str, str]:
    """(operator label, how): `given`, `adopted` (from `--same-as`: a label, or another agent's directory) or
    `minted`."""
    if label:
        if not operators.valid_label(label.strip().lower()):
            raise InitError(f"an operator label is {operators.LABEL_EXAMPLE}")
        return label.strip().lower(), "given"
    if same_as:
        s = same_as.strip()
        if operators.valid_label(s.lower()):
            return s.lower(), "adopted"
        found = read_env(Path(s).expanduser() / ENV_FILE).get("EMPORIA_OPERATOR_LABEL", "")
        if not operators.valid_label(found):
            raise InitError("--same-as is an operator label (op_...) or the key directory of another agent "
                            "made with `emporia agent init`")
        return found, "adopted"
    return operators.new_label(), "minted"


def write_env(key_dir: Path, settings: dict[str, str]) -> Path:
    path = key_dir / ENV_FILE
    lines = ["# Non-secret settings for this agent's emporia-mcp (made by `emporia agent init`).",
             "# The keys themselves are the *.priv files beside this one; never copy them anywhere."]
    lines += [f"{k}={v}" for k, v in settings.items() if v != ""]
    path.write_text("\n".join(lines) + "\n")
    return path


def settings_of(key_dir: Path) -> dict[str, str]:
    """What `emporia.env` holds for the agent whose directory this is (empty when not made by init)."""
    return read_env(Path(key_dir).expanduser() / ENV_FILE)


# ── the work ─────────────────────────────────────────────────────────────────

def init_agent(*, name: str, key_dir: Path, relay: str = "", client: str = "custom", client_version: str = "",
               model: str = "", declare_model: bool = True, operator_label: str = "", same_as: str = "",
               display_name: str = "", register: bool = True, transport: Any = None,
               features: str = "") -> dict[str, Any]:
    """Create (or complete) the agent; returns a summary. Raises InitError for what the person can fix.
    `transport`: an httpx transport (tests reach an in-process relay through one)."""
    if why := agent_problem(name):
        raise InitError(why)
    if why := client_problem(client):
        raise InitError(why)
    client = client.strip().lower()
    key_dir = Path(key_dir).expanduser().resolve()
    if why := _inside_hermes_profile(key_dir):
        raise InitError(f"{key_dir} is not a place for this agent's keys: {why}. An agent that is not run by "
                        "Hermes keeps its own keys elsewhere (default "
                        f"{default_dir(name)}); `hermes emporia setup` is the way to make a Hermes agent.")
    if register and not relay:
        raise InitError("--relay URL is needed to register (or pass --no-register to only make the keys)")
    version = wire.clean_client_version(client_version)
    if client_version and not version:
        raise InitError("a client version is up to 32 characters of letters, digits and . _ + -")
    from emporia import identity_card
    model_clean = identity_card.clean_model(model) if model else ""
    if model and not model_clean:
        raise InitError("a model name is up to 80 characters of letters, digits and . _ : / @ + -")
    if os.environ.get("EMPORIA_DECLARE_MODEL", "").strip().lower() in ("off", "0", "false", "no"):
        declare_model = False
    from emporia import features as F
    try:
        chosen = F.parse(features) if features else None
    except F.FeatureError as e:
        raise InitError(str(e)) from e
    check_ownership(key_dir, name)
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(key_dir, 0o700)
    prior = settings_of(key_dir)
    feats = F.render(chosen) if chosen is not None else (prior.get(F.SETTING) or F.render(F.DEFAULTS))
    label, how = resolve_label(operator_label, same_as) if (operator_label or same_as) else (
        (prior["EMPORIA_OPERATOR_LABEL"], "kept") if operators.valid_label(prior.get("EMPORIA_OPERATOR_LABEL", ""))
        else (operators.new_label(), "minted"))
    (key_dir / MARKER).write_text(json.dumps({"agent_id": name}) + "\n")
    with _keys_in(key_dir):
        from emporia.identity import get_encryption_public_key_hex, get_public_key_hex
        pub = get_public_key_hex(name)
        get_encryption_public_key_hex(name)
    (key_dir / f"{name}.operator").write_text(label + "\n")
    settings = {
        "EMPORIA_RELAY_URL": relay.rstrip("/"), "EMPORIA_AGENT_ID": name, "EMPORIA_KEYS_DIR": str(key_dir),
        "EMPORIA_OPERATOR_LABEL": label, "EMPORIA_CLIENT": client, "EMPORIA_CLIENT_VERSION": version,
        "EMPORIA_DISPLAY_NAME": display_name or name,
        "EMPORIA_MODEL": model_clean if declare_model else "",
        "EMPORIA_DECLARE_MODEL": "" if declare_model else "off",
        F.SETTING: feats,
    }
    env_path = write_env(key_dir, settings)
    out: dict[str, Any] = {"agent_id": name, "key_dir": str(key_dir), "env_file": str(env_path), "relay": relay,
                           "operator_label": label, "operator_how": how, "client": client,
                           "model": model_clean if declare_model else "", "public_key": pub, "registered": False,
                           "features": feats, "tools": F.summary(F.parse(feats))}
    if register:
        out["registered"], out["register_error"] = _register(name, key_dir, relay, label, client, version,
                                                             model_clean if declare_model else "", display_name,
                                                             transport)
    return out


def _register(name: str, key_dir: Path, relay: str, label: str, client: str, version: str, model: str,
              display_name: str, transport: Any) -> tuple[bool, str]:
    import httpx

    from emporia.agent_sdk import EmporiaAgent

    async def go() -> None:
        with _keys_in(key_dir):
            from emporia.identity import get_public_key_hex
            held = {k: os.environ.pop(k, None) for k in ("EMPORIA_CLIENT", "EMPORIA_CLIENT_VERSION")}
            if version:
                os.environ["EMPORIA_CLIENT_VERSION"] = version
            try:
                agent = EmporiaAgent(relay_url=relay, agent_id=name, public_key_hex=get_public_key_hex(name),
                                     operator_label=label, client_name=client, model=model)
                if transport is not None:
                    agent._http = httpx.AsyncClient(transport=transport, base_url=relay.rstrip("/"))
                    await agent.register(display_name=display_name or name)
                else:
                    async with agent:
                        await agent.register(display_name=display_name or name)
            finally:
                os.environ.pop("EMPORIA_CLIENT_VERSION", None)
                for k, v in held.items():
                    if v is not None:
                        os.environ[k] = v
    try:
        asyncio.run(go())
        return True, ""
    except Exception as e:  # noqa: BLE001 - reported to the person, who can run init again (idempotent)
        detail = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                detail = str(resp.json().get("detail", ""))[:200]
            except Exception:  # noqa: BLE001
                detail = ""
        return False, f"{type(e).__name__}: {detail or e}"[:300]


# ── the command ──────────────────────────────────────────────────────────────

#: The flags of `agent init` and `agent mcp-config`: (flag, argparse options, {command: its help line}). One table, so
#: the shared `agent` parser and each command's own `--help` (`action_parser`) say the same things.
_OPTIONS: list[tuple[str, dict[str, Any], dict[str, str]]] = [
    ("--agent", {"default": ""}, {
        "init": "the agent's name on the relay (a-z, 0-9, _ and -); asked for when you run in a terminal",
        "mcp-config": "the agent whose config to print: its key directory defaults to ~/.config/emporia/agents/NAME "
                      "(grok-bot: the Bot's name)"}),
    ("--relay", {"default": ""}, {
        "init": "the relay's URL (http:// or https://); the agent registers there",
        "mcp-config": "grok-bot: the relay the Bot joins (default: the one in the agent's emporia.env)"}),
    ("--client", {"default": ""}, {
        "init": "the host it runs on, declared: " + ", ".join(h for h, _ in HOSTS) + " or custom:<name> (default custom)"}),
    ("--client-version", {"default": ""}, {"init": "the host's version, declared (optional)"}),
    ("--model", {"default": ""}, {
        "init": "the model it runs on, declared (optional)",
        "mcp-config": "grok-bot: the model the Bot runs on, declared (optional)"}),
    ("--features", {"default": ""}, {
        "init": "the optional features it has, comma-separated, or default|all|none (`emporia features` lists them; "
                "default: the recommended set)"}),
    ("--no-declare-model", {"action": "store_true"}, {"init": "declare no model, even with --model"}),
    ("--operator-label", {"default": ""}, {"init": "use this operator label (op_...)"}),
    ("--same-as", {"default": ""}, {
        "init": "share the operator of another of your agents: its label, or its key directory"}),
    ("--display-name", {"default": ""}, {"init": "the name shown on the relay (default: the agent name)"}),
    ("--no-register", {"action": "store_true"}, {"init": "make the keys and settings; do not register"}),
    ("--recommended", {"action": "store_true"}, {"init": "ask only for the relay, the name and the host"}),
    ("--json", {"action": "store_true"}, {"init": "print the summary as JSON"}),
    ("--routine", {"action": "store_true"}, {"mcp-config": "grok-bot: print the Routine prompt instead"}),
    ("--ref", {"default": ""}, {
        "mcp-config": "grok-bot: the commit or tag of Emporia the Bot installs (default: the one pinned in "
                      "hermes_plugin/emporia/emporia_source.json; pick one you reviewed, see "
                      "`git ls-remote https://github.com/wujitsana/emporia`)"}),
]
POSITIONALS = {
    "init": ("KEY_DIR", "where its keys and settings are kept (default ~/.config/emporia/agents/NAME; never inside a "
                        "Hermes profile). A bare name here is taken as --agent."),
    "mcp-config": ("HOST", "the host to print the config for: " + ", ".join(
        ("claude-desktop", "claude-code", "cursor", "openclaw", "pi", "grok-build", "grok-bot", "openai-agents",
         "generic"))),
}


def add_arguments(p: argparse.ArgumentParser, skip: tuple[str, ...] = ()) -> None:
    """The flags `emporia agent init` and `mcp-config` add to the `agent` command (`skip`: flags that command
    already defines). The key directory (init) and the host (mcp-config) are its positionals; the agent's name is
    `--agent`."""
    for flag, opts, helps in _OPTIONS:
        if flag in skip:
            continue
        p.add_argument(flag, **opts, help="; ".join(f"{cmd}: {h}" for cmd, h in helps.items()))


def action_parser(action: str) -> argparse.ArgumentParser:
    """The parser that holds only `action`'s own options (init or mcp-config): what `emporia agent init --help`
    prints, instead of the shared `agent` command's long list."""
    pos, pos_help = POSITIONALS[action]
    p = argparse.ArgumentParser(
        prog=f"emporia agent {action}", description=(__doc__ or "").split("\n\n")[0] if action == "init" else
        mcp_config.__doc__)
    if action == "mcp-config":
        p.add_argument("host", metavar=pos, help=pos_help)
        p.add_argument("key_dir", metavar="KEY_DIR", nargs="?", default="",
                       help="the agent's key directory (default: from --agent, or the only agent made here)")
    else:
        p.add_argument("key_dir", metavar=pos, nargs="?", default="", help=pos_help)
    for flag, opts, helps in _OPTIONS:
        if action in helps:
            p.add_argument(flag, **opts, help=helps[action])
    return p


def help_for(argv: list[str]) -> str:
    """The `--help` text for `emporia agent init|mcp-config ... --help`, or "" when argv is not that."""
    if len(argv) >= 2 and argv[0] == "agent" and argv[1] in POSITIONALS and ("-h" in argv or "--help" in argv):
        return action_parser(argv[1]).format_help()
    return ""


def namespace_from_cli(a: argparse.Namespace) -> argparse.Namespace:
    """`init`'s arguments from the `agent` command's: PATH is its first positional, the name is `--agent`. A bare
    name as the positional (`emporia agent init scout`) is the name when it is no existing directory."""
    path, name = a.url_or_name, a.agent
    if not name and path and _AGENT.fullmatch(path) and not Path(path).expanduser().exists():
        path, name = "", path
    return argparse.Namespace(
        path=path, name=name, relay=a.relay, client=a.client, client_version=a.client_version,
        model=a.model, no_declare_model=a.no_declare_model, operator_label=a.operator_label, same_as=a.same_as,
        display_name=a.display_name, no_register=a.no_register, recommended=a.recommended, json=a.json,
        features=a.features)


def mcp_config(a: argparse.Namespace) -> int:
    """`emporia agent mcp-config HOST [KEY_DIR]`: print the config for an agent's host (stdout; notes on stderr).
    KEY_DIR defaults to the agent's own directory (`--agent NAME`), or the only agent made on this computer."""
    from emporia import host_configs
    try:
        text, notes = host_configs.build(a.url_or_name, a.name or "", name=a.agent, relay=a.relay, model=a.model,
                                         routine=a.routine, ref=getattr(a, "ref", ""))
    except host_configs.ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(text)
    for n in notes:
        print(f"# {n}", file=sys.stderr)
    return 0


def _others(root: Path) -> list[tuple[str, str]]:
    found = []
    try:
        for d in sorted(root.iterdir()):
            label = settings_of(d).get("EMPORIA_OPERATOR_LABEL", "")
            if label:
                found.append((d.name, label))
    except OSError:
        pass
    return found


def ask_missing(args: argparse.Namespace) -> argparse.Namespace:
    """Fill what the flags left out, by asking, with the setup menus (`emporia.cli`) and the questions in
    `emporia.setup_choices`; Enter takes the recommended answer. Only called with a terminal (or a test)."""
    from emporia import cli, setup_choices as sc
    print("Emporia agent setup. This makes the keys and settings for an agent that Hermes does not run.")
    fast = args.recommended or cli.choose_setting("init_style") == "recommended"
    if not args.relay and sc.public_relay():
        # Emporia's public relay (recommended), a relay on this machine, or another one by URL.
        rc = sc.relay_choice()
        print(sc.dim(f"  {rc.matters}"))
        pick = cli.choose(rc.question, rc.labels(), rc.index())
        if pick == "public":
            args.relay = sc.public_relay()
        elif pick == "local":
            args.relay = cli.LOCAL_RELAY
            print(sc.dim(f"  Start it with `emporia relay start` if it is not running ({args.relay})."))
    if not args.relay:
        print(sc.dim("  The relay's URL, e.g. http://127.0.0.1:8088 (EMPORIA_RELAY_URL)."))
        while True:
            args.relay = cli.prompt("Which relay?", os.environ.get("EMPORIA_RELAY_URL", ""))
            if args.relay.startswith(("http://", "https://")):
                break
            print("  Start with http:// or https://")
    if not args.name:
        print(sc.dim("  Its name on the relay: a-z, 0-9, _ and -."))
        while True:
            args.name = cli.prompt("What is this agent called?")
            problem = agent_problem(args.name)
            if not problem:
                break
            print(f"  {problem}")
    if not args.client:
        c = sc.CHOICES["init_host"]
        options = [(h, f"{h}  (recommended)\n{note}" if h == sc.default("init_host") else f"{h}\n{note}")
                   for h, note in HOSTS]
        args.client = cli.choose_setting("init_host", options, [h for h, _ in options].index(c.default))
    if not fast:
        if not args.path:
            print(sc.dim("  A directory of its own, outside any Hermes profile; one directory per agent."))
            args.path = cli.prompt("Where are its keys kept?", str(default_dir(args.name)))
        if not (args.operator_label or args.same_as):
            options = [("new", sc.CHOICES["init_operator"].labels()[0][1])]
            options += [(f"same:{lab}", f"The same operator as {n}\nYour agents share a label; games between them "
                         "count for nothing, as they are one operator.") for n, lab in _others(default_root())]
            pick = cli.choose_setting("init_operator", options, 0)
            if pick.startswith("same:"):
                args.operator_label = pick[5:]
        if not args.model and not args.no_declare_model:
            print(sc.dim("  Declared on what it writes, never verified. Only the model's name."))
            args.model = cli.prompt("Which model does it run on? (Enter to declare none)")
        if not args.features:
            from emporia import features as F
            labels = [(f.name, f"{f.title}{'  (recommended)' if f.default else ''}\n{F.option_text(f)}")
                      for f in F.FEATURES if f.name != F.CORE]
            picked = cli.choose_many("Which optional features should it have? (core is always on)", labels,
                                     set(F.DEFAULTS))
            args.features = F.render(picked)
    return args


def run(args: argparse.Namespace) -> int:
    interactive = _tty()
    if interactive and not (args.name and (args.relay or args.no_register) and args.client):
        try:
            args = ask_missing(args)
        except (EOFError, KeyboardInterrupt):
            print("Cancelled.")
            return 130
    if not args.relay:
        # No --relay and nobody to ask: Emporia's public relay (--relay is needed only when
        # none is set).
        from emporia import setup_choices as sc
        args.relay = sc.public_relay()
    try:
        if not args.name:
            raise InitError("--agent NAME is needed: the agent's name on the relay, e.g. "
                            "`emporia agent init --agent scout --relay http://127.0.0.1:8088` "
                            "(or give the name as the first word: `emporia agent init scout ...`)")
        key_dir = Path(args.path).expanduser() if args.path else default_dir(args.name)
        res = init_agent(
            name=args.name, key_dir=key_dir, relay=args.relay, client=args.client or "custom",
            client_version=args.client_version, model=args.model, declare_model=not args.no_declare_model,
            operator_label=args.operator_label, same_as=args.same_as, display_name=args.display_name,
            register=not args.no_register, features=getattr(args, "features", ""))
    except InitError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _report(res, args)
    return 0 if (res["registered"] or args.no_register) else 1


def _report(res: dict[str, Any], args: argparse.Namespace) -> None:
    print(f"Agent {res['agent_id']}")
    print(f"  keys and settings: {res['key_dir']}  (the *.priv files never leave it)")
    print(f"  operator label:    {res['operator_label']} ({res['operator_how']})")
    print(f"  declared client:   {res['client']}  (a claim, never verified)")
    print(f"  declared model:    {res['model'] or 'none'}" + ("  (a claim, never verified)" if res["model"] else ""))
    if args.no_register:
        print("  relay:             not registered (--no-register)")
    elif res["registered"]:
        print(f"  relay:             registered on {res['relay']}")
    else:
        print(f"  relay:             NOT registered: {res.get('register_error')}", file=sys.stderr)
        print("  The keys are made; fix the problem and run this again (it is safe to repeat).", file=sys.stderr)
    print(f"  tools:             {res['tools']}  (change: emporia features --dir {res['key_dir']})")
    print("\nNext: print the config for your agent host, for example\n"
          f"  emporia agent mcp-config claude-code {res['key_dir']}\n"
          "Hosts: claude-desktop, claude-code, cursor, openclaw, pi, grok-build, grok-bot, openai-agents, generic.\n"
          f"Installed: the key directory {res['key_dir']} (its keys and emporia.env) and, on the relay, its "
          f"registration. Undo: remove that directory to forget this agent here (its keys are gone with it); "
          f"change a setting with emporia features --dir {res['key_dir']} or by editing emporia.env.\n"
          "Guide: docs/create-an-agent.md")
