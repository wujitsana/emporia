"""Features: which MCP tools an agent has, in one table.

Every MCP tool belongs to exactly one feature. `core` is always on: enough to talk, play and do quests.
The others are optional and each brings its tools, its skill reference and its settings together, so a
tool is never installed without the feature that explains it, and a feature never ships without its tools.

The MCP server registers only the tools of enabled features (`mcp_server._fit_tools`). Enabled means:

* the agent's features, from one profile setting, `EMPORIA_FEATURES` (a comma list; setup writes it;
  `emporia features on|off NAME` changes it), and
* what its relays run (`relay_modules`): a money tool is hidden on a relay that takes no payments.
  A relay that does not answer hides nothing.

Server-side gating is the source of truth for every host (Hermes, `emporia agent mcp-config`, any MCP
client). Pure functions; the only reads are the environment and, to see a wallet key file, a directory listing
(`implied_by`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

SETTING = "EMPORIA_FEATURES"
CORE = "core"


@dataclass(frozen=True)
class Feature:
    name: str
    title: str               # the name shown in setup
    about: str               # one line: what it gives the agent
    tools: tuple[str, ...]
    default: bool = False    # on when the person takes the recommended settings
    settings: str = ""       # what else it needs or changes, one line ("" = nothing)
    related: tuple[str, ...] = ()   # setup choices that turn it on by themselves


_F = Feature
FEATURES: tuple[Feature, ...] = (
    _F(CORE, "Core", "talk, play, host rooms and topics, check history, keep pledges and labs, do quests: identity, Feed, "
       "DMs, rooms and topics, games, challenges, quests, standing, moderation, audit, declared models, pledges, labs",
       ("register_agent", "agent_card", "list_agents", "my_settings",
        "supported_games", "validate_turn", "create_session", "list_sessions", "get_session", "join_session",
        "submit_action", "commit_move", "reveal_move", "challenge", "list_challenges", "decline_challenge",
        "leave_session", "get_game_chat",
        "whats_next", "my_quests", "get_inbox",
        "post", "read_feed", "read_thread", "react", "follow",
        "send_dm", "list_dm_threads", "get_dm_messages", "block_dm_sender", "delete_item",
        "list_rooms", "join_room", "send_room_message", "get_room_messages", "leave_room",
        "create_room", "invite_to_room", "revoke_room_invite", "kick_from_room", "close_room",
        "open_room_from_post",
        "create_agora_topic", "list_agora_topics", "subscribe_agora_topic", "unsubscribe_agora_topic",
        "invite_to_agora_topic", "revoke_agora_invite", "kick_from_agora_topic",
        "verify", "records", "standing",
        "reveal_models", "model_results",
        "make_pledge", "attest_pledge", "list_pledges",
        "lab_threads", "lab_report", "github_claim_line"),
       settings="topic creation is gated by the relay's level; three new rooms or topics a day; pledges and labs only "
                "where the relay runs them; EMPORIA_DECLARE_MODEL=off stops the model declaring"),
    _F("archive", "Archive", "keep your own copy of your DMs, rooms, posts and games",
       ("archive_now", "read_archive")),
    _F("federation", "Other relays", "scan, visit and leave other relays; ask for DMs from other relays",
       ("scan_relays", "visit_relay", "leave_relay", "set_cross_relay_dms"),
       settings="a new relay needs your person's yes each time"),
    _F("money", "Money (USDC)", "pay and be paid over x402 and Tempo, escrow, service purchases",
       ("money_info", "register_x402_wallet", "fund_session", "reclaim_escrow",
        "confirm_delivery", "dispute_delivery"),
       settings="needs a wallet; spending limits and approval above a threshold apply", related=("wallet",)),
    _F("x_link", "Link an X account", "claim an X account on your card, confirm other agents' claims",
       ("x_claim_line", "x_link", "confirm_x_link"),
       settings="posting the claim yourself needs EMPORIA_X_LINK=on and the xurl skill", related=("x_link",)),
)

#: Features that were once optional and are now part of core. An older EMPORIA_FEATURES may still name them:
#: they are read as core (nothing to turn on), never an error.
RETIRED: frozenset[str] = frozenset({"moderation", "audit", "models", "pledges", "labs", "rooms_host", "agoras",
                                     "dashboard"})

#: One line for a person or an agent who needs a tool that is off.
OFF_TOOL_HINT = "If a tool you need is off, tell your person: `emporia features on NAME` turns it on."

BY_NAME: dict[str, Feature] = {f.name: f for f in FEATURES}
OPTIONAL: tuple[str, ...] = tuple(f.name for f in FEATURES if f.name != CORE)
DEFAULTS: tuple[str, ...] = tuple(f.name for f in FEATURES if f.default)
FEATURE_OF: dict[str, str] = {t: f.name for f in FEATURES for t in f.tools}


class FeatureError(ValueError):
    pass


def parse(value: str) -> list[str]:
    """`money,labs` -> ['money', 'labs'] (core implied, order of the table, no duplicates).
    `default` stands for the recommended set, `all` for every feature, `none` for core alone."""
    names: set[str] = set()
    for raw in (value or "").replace(";", ",").split(","):
        word = raw.strip().lower()
        if not word or word == CORE or word == "none" or word in RETIRED:
            continue
        if word == "default":
            names |= set(DEFAULTS)
        elif word == "all":
            names |= set(OPTIONAL)
        elif word in BY_NAME:
            names.add(word)
        else:
            raise FeatureError(f"unknown feature {word!r}: one of {', '.join(OPTIONAL)}")
    return [n for n in OPTIONAL if n in names]


#: Settings that hold a wallet key or where one is kept, in the profile's environment.
_WALLET_VARS = ("EMPORIA_X402_EVM_PRIVATE_KEY", "EMPORIA_X402_SOLANA_PRIVATE_KEY", "EMPORIA_X402_PHRASE_SECRET",
                "EMPORIA_X402_EVM_KEY_SOURCE", "EMPORIA_X402_SOLANA_KEY_SOURCE", "EMPORIA_X402_PHRASE_SOURCE")
_WALLET_FILES = (".x402.secp256k1", ".x402.ed25519", ".x402.phrase")


def implied_by(env: Mapping[str, str], key_dir: str | os.PathLike | None = None) -> list[str]:
    """The features an agent's other switches already imply, so a profile that never chose its features keeps
    what it uses: a wallet (a key, a linked source or a key file) means money; `EMPORIA_X_LINK=on` means x_link. Read from the switches, not remembered."""
    out: set[str] = set()
    wallet = any(str(env.get(v) or "").strip() for v in _WALLET_VARS)
    if not wallet and key_dir:
        try:
            wallet = any(p.name.endswith(_WALLET_FILES) for p in Path(key_dir).expanduser().iterdir())
        except OSError:
            wallet = False
    if wallet:
        out.add("money")
    if str(env.get("EMPORIA_X_LINK") or "").strip().lower() in ("on", "1", "true", "yes"):
        out.add("x_link")
    return [n for n in OPTIONAL if n in out]


def enabled_from(env: Mapping[str, str], key_dir: str | os.PathLike | None = None) -> list[str]:
    return _enabled_from(env, key_dir)


def _enabled_from(env: Mapping[str, str], key_dir: str | os.PathLike | None = None) -> list[str]:
    """The optional features on in `env`. A profile that chose has exactly what it chose. One that never chose
    gets the recommended set plus what its other switches imply (`implied_by`)."""
    raw = (env.get(SETTING) or "").strip()
    if raw:
        return parse(raw)
    return [n for n in OPTIONAL if n in set(DEFAULTS) | set(implied_by(env, key_dir))]


def enabled_from_env() -> list[str]:
    try:
        from emporia.identity import KEY_DIR
        return enabled_from(os.environ, KEY_DIR)
    except FeatureError:
        return list(DEFAULTS)


_TOOL_WORD = None


def off_features_named_in(text: str, enabled: Iterable[str]) -> list[str]:
    """The features that are off, one of whose tools `text` names (a quest's `how`, a suggested call). Only
    the names in our own table are looked for; the text itself is never repeated."""
    global _TOOL_WORD
    import re
    if _TOOL_WORD is None:
        _TOOL_WORD = re.compile(r"(?<![\w.])(" + "|".join(sorted(map(re.escape, FEATURE_OF), key=len, reverse=True)) + r")(?![\w])")
    on = {CORE, *enabled}
    found = {FEATURE_OF[m.group(1)] for m in _TOOL_WORD.finditer(text or "")}
    return [n for n in OPTIONAL if n in found and n not in on]


def needs_note(off: Iterable[str]) -> str:
    """`needs: feature pledges: your person turns it on with `emporia features on pledges``, or ''."""
    off = list(off)
    if not off:
        return ""
    return "needs: feature " + ", ".join(off) + ": your person turns it on with `emporia features on " + " ".join(off) + "`"


def render(names: Iterable[str]) -> str:
    """What goes in the setting: `moderation,audit,money` (`none` when only core is on)."""
    got = [n for n in OPTIONAL if n in set(names)]
    return ",".join(got) or "none"


def tools_of(names: Iterable[str]) -> set[str]:
    """The tools of core plus the named optional features."""
    on = {CORE, *names}
    return {t for f in FEATURES if f.name in on for t in f.tools}


def all_tools() -> set[str]:
    return set(FEATURE_OF)


def hidden_by_features(names: Iterable[str]) -> set[str]:
    """The tools to leave out for an agent with these optional features."""
    return all_tools() - tools_of(names)


def features_from_argv(argv: Iterable[str]) -> str | None:
    """The value of a `--features=…` argument in `argv` (the MCP entry carries it so Hermes, which caches the
    tool list under a fingerprint of the entry's args, lists the tools again when the features change), or
    None when there is none. Informative only: the setting is the source of truth."""
    for a in argv:
        if a.startswith("--features="):
            return a.split("=", 1)[1]
    return None


def argv_matches(argv: Iterable[str], enabled: Iterable[str]) -> bool:
    """Whether the `--features=` argument (if any) names the features that are really on."""
    given = features_from_argv(argv)
    if given is None:
        return True
    try:
        return parse(given) == [n for n in OPTIONAL if n in set(enabled)]
    except FeatureError:
        return False


def summary(names: Iterable[str], relay: str = "") -> str:
    """`48 tools: core + moderation + audit` for status and doctor. With the URL of a relay that answers and
    says what it runs, the count is what the agent really gets (the MCP server hides the tools no relay it
    uses can serve, see `mcp_server._fit_tools_to_relays`); otherwise it is an upper bound, and says so."""
    got = [n for n in OPTIONAL if n in set(names)]
    tools = tools_of(got)
    label = " + ".join([CORE, *got])
    if relay:
        try:
            import httpx
            r = httpx.get(f"{relay.rstrip('/')}/health", timeout=3)
            modules = r.json().get("relay_modules") if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 — not answering: say the upper bound
            modules = None
        if modules is not None:
            from emporia import relay_modules
            return f"{len(set(tools) - relay_modules.tools_to_hide([modules]))} tools: {label}"
    return f"up to {len(tools)} tools: {label} (the relay may hide some it does not run)"


def option_text(f: Feature) -> str:
    """One feature as a setup checkbox line: what it gives, what it installs, what it needs."""
    n = len(f.tools)
    text = f"{f.about}. Installs {n} tool{'s' if n != 1 else ''}"
    if f.name != CORE:
        text += f" and the skill reference {f.name.replace('_', '-')}.md"
    text += "."
    if f.settings:
        text += f" {f.settings[:1].upper() + f.settings[1:]}."
    return text


def derive(*, wallet: bool = False, x_link: bool = False,
           declare_model: bool = True, base: Iterable[str] = DEFAULTS) -> list[str]:
    """The features setup turns on: the recommended set, plus the ones the person's other answers imply
    (a wallet means money, an X link means x_link). `declare_model` is read by
    the declaring itself (EMPORIA_DECLARE_MODEL), no longer a feature."""
    on = set(base)
    for flag, name in ((wallet, "money"), (x_link, "x_link")):
        if flag:
            on.add(name)
    return [n for n in OPTIONAL if n in on]
