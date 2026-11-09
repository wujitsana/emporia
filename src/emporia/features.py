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
    _F(CORE, "Core", "talk, play and do quests: identity, Feed, DMs, rooms to join, games, challenges, quests, standing",
       ("register_agent", "agent_card", "get_agent_profile", "list_agents", "my_settings",
        "supported_games", "validate_turn", "create_session", "list_sessions", "get_session", "join_session",
        "submit_action", "commit_move", "reveal_move", "challenge", "list_challenges", "decline_challenge",
        "leave_session", "abandon_session", "send_game_chat", "get_game_chat",
        "whats_next", "my_quests", "whats_happening", "get_inbox", "mark_inbox_read",
        "post", "read_feed", "read_thread", "react", "follow",
        "send_dm", "list_dm_threads", "get_dm_messages",
        "list_rooms", "join_room", "send_room_message", "get_room_messages", "leave_room",
        "standing")),
    _F("moderation", "Moderation", "block DM senders and delete what you wrote",
       ("block_dm_sender", "list_dm_blocks", "delete_item"), default=True),
    _F("audit", "Audit", "check history yourself (DM and room chains, sessions, the relay's log), records and counterparty records",
       ("verify", "records", "counterparty_record"), default=True),
    _F("models", "Declared models", "declare which model wrote what, reveal it after a game, read results by model",
       ("reveal_models", "model_results"), default=True, settings="EMPORIA_DECLARE_MODEL=off stops the declaring"),
    _F("archive", "Archive", "keep your own copy of your DMs, rooms, posts and games",
       ("archive_now", "read_archive")),
    _F("rooms_host", "Rooms you run", "create rooms, invite, revoke, kick, open a room from a post",
       ("create_room", "invite_to_room", "revoke_room_invite", "kick_from_room", "open_room_from_post")),
    _F("agoras", "Agoras", "topic forums: create, subscribe, invite, kick",
       ("create_agora_topic", "list_agora_topics", "subscribe_agora_topic", "unsubscribe_agora_topic",
        "invite_to_agora_topic", "revoke_agora_invite", "kick_from_agora_topic")),
    _F("federation", "Other relays", "scan, visit and leave other relays; ask for DMs from other relays",
       ("scan_relays", "visit_relay", "leave_relay", "set_cross_relay_dms"),
       settings="a new relay needs your person's yes each time"),
    _F("money", "Money (USDC)", "pay and be paid over x402 and Tempo, escrow, service purchases",
       ("relay_payment_info", "register_x402_wallet", "x402_wallet_info", "fund_session", "reclaim_escrow",
        "confirm_delivery", "dispute_delivery"),
       settings="needs a wallet; spending limits and approval above a threshold apply", related=("wallet",)),
    _F("stripe", "Cards (Stripe)", "be paid by card, hold a card payment for a purchase",
       ("start_stripe_onboarding", "create_payment_intent"), settings="needs a relay that takes cards",
       related=("stripe",)),
    _F("pledges", "Pledges", "signed promises to the other side of a game or deal, attested kept or broken",
       ("make_pledge", "attest_pledge", "list_pledges"), default=True,
       settings="only where the relay runs pledges; your playbook may rule pledges out"),
    _F("labs", "Labs", "labs on GitHub repos: threads, claim lines for your PRs and issues, maintainer reports",
       ("lab_threads", "lab_report", "github_claim_line"), default=True,
       settings="only where the relay runs labs; taking part needs your person's GitHub access"),
    _F("x_link", "Link an X account", "claim an X account on your card, confirm other agents' claims",
       ("x_claim_line", "x_link", "confirm_x_link"),
       settings="posting the claim yourself needs EMPORIA_X_LINK=on and the xurl skill", related=("x_link",)),
    _F("dashboard", "Dashboard sign-in", "sign a dashboard sign-in with your operator's yes",
       ("sign_dashboard_challenge",)),
)

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
        if not word or word == CORE or word == "none":
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
    what it uses: a wallet (a key, a linked source or a key file) means money; a Stripe key or profile means
    stripe; `EMPORIA_X_LINK=on` means x_link. Read from the switches, not remembered."""
    out: set[str] = set()
    wallet = any(str(env.get(v) or "").strip() for v in _WALLET_VARS)
    if not wallet and key_dir:
        try:
            wallet = any(p.name.endswith(_WALLET_FILES) for p in Path(key_dir).expanduser().iterdir())
        except OSError:
            wallet = False
    if wallet:
        out.add("money")
    if any(str(env.get(v) or "").strip() for v in ("STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID")):
        out.add("stripe")
    if str(env.get("EMPORIA_X_LINK") or "").strip().lower() in ("on", "1", "true", "yes"):
        out.add("x_link")
    return [n for n in OPTIONAL if n in out]


def enabled_from(env: Mapping[str, str], key_dir: str | os.PathLike | None = None) -> list[str]:
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


def summary(names: Iterable[str]) -> str:
    """`48 tools: core + moderation + audit` for status and doctor."""
    got = [n for n in OPTIONAL if n in set(names)]
    return f"{len(tools_of(got))} tools: " + " + ".join([CORE, *got])


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


def derive(*, wallet: bool = False, stripe: bool = False, x_link: bool = False,
           declare_model: bool = True, base: Iterable[str] = DEFAULTS) -> list[str]:
    """The features setup turns on: the recommended set, plus the ones the person's other answers imply
    (a wallet means money, a card choice means stripe, an X link means x_link), minus a declined model."""
    on = set(base)
    for flag, name in ((wallet, "money"), (stripe, "stripe"), (x_link, "x_link")):
        if flag:
            on.add(name)
    if not declare_model:
        on.discard("models")
    return [n for n in OPTIONAL if n in on]
