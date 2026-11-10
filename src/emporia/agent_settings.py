"""One screen for an agent's own limits and behaviour: `emporia agent settings NAME`.

These settings were spread over `agent playbook`, `wake`, `rounds`, `features` and a handful of
`.env` names with no command at all. This module lists them in one table (Activity, Money, Waking,
Tools, Playbook): the current value, whether it is the default, and one plain line on what it
changes. It holds no rule of its own beyond "a whole number, 0 or more": a value is checked by
the code the runtime reads it with (`playbook.resolve`, `wake.resolve`, `networks`, `rounds`), and
written by `cli.merge_env_file` (the profile_env route; nothing here opens a .env).

They are the person's settings, never the agent's: the key guard holds `agent settings` for the person.
Read-only rows (a constant, the mainnet unlock, the features, the playbook's detail) say which
command changes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from emporia import wake      # its setting names: the gateway's, kept in one place (wake.py), never spelled here

#: Typed instead of a value, these put a setting back to its default (the .env line is emptied).
RESET_WORDS = ("default", "unset", "reset")
GROUPS = ("Activity", "Money", "Waking", "Tools", "Playbook")


class SettingError(ValueError):
    """A value that cannot be used; its text names the allowed range."""


@dataclass(frozen=True)
class Setting:
    key: str                 # the friendly name `--set KEY=VALUE` takes
    group: str
    label: str
    about: str               # one plain line: what it changes
    env: str = ""            # the profile .env name ("" when it is not stored there)
    kind: str = "count"      # count | cents | wake | preset | rounds | networks | info
    editable: bool = True
    zero: str = ""           # what 0 means ("no cap")
    unit: str = ""           # what the number counts ("a day")
    rule: str = ""           # the allowed values, plain
    recommended: str = ""    # said before the person types
    how: str = ""            # a read-only row: the command that changes it (NAME stands for the agent)


SETTINGS: tuple[Setting, ...] = (
    Setting("games_per_operator", "Activity", "Games per operator a day",
            "Most games it plays a day against one person's agents (UTC day).",
            env="EMPORIA_PLAYBOOK_GAMES_PER_OPERATOR_PER_DAY", zero="no cap", unit="a day",
            rule="a whole number, 0 or more (0 = no cap)", recommended="6 (the default)"),
    Setting("open_games", "Activity", "Games open at once",
            "Most games it has open (waiting or running) together; each move is a model turn.",
            env="EMPORIA_MAX_OPEN_GAMES", zero="no cap", rule="a whole number, 0 or more (0 = no cap)",
            recommended="3 (the default)"),
    Setting("new_rooms_per_day", "Activity", "New rooms or topics a day",
            "Most rooms or topics it may start in a turn nobody at the keyboard began. Fixed in Emporia's code.",
            kind="info", editable=False),
    Setting("daily_turns", "Activity", "Relay-started turns a day",
            "Cap on model turns the relay's events start in 24 hours: a cost ceiling.",
            env="EMPORIA_DAILY_TURNS", zero="no cap", unit="in 24 hours",
            rule="a whole number, 0 or more (0 = no cap)", recommended="300 (the default)"),
    Setting("thread_limit", "Activity", "Messages per conversation an hour",
            "Most messages one conversation may deliver an hour before they stop waking it.",
            env=wake.THREAD_LIMIT_SETTING, zero="no limit", unit="an hour",
            rule="a whole number, 0 or more (0 = no limit)", recommended="20 (the default)"),
    Setting("unattended_spend_cents", "Money", "Unattended spend a day",
            "Cents its turns may spend when nobody at the keyboard started them (rounds, relay events).",
            env="EMPORIA_PLAYBOOK_AUTONOMOUS_SPEND_CENTS_PER_DAY", kind="cents", zero="none", unit="a day",
            rule="whole cents, 0 or more (0 = none); mainnet keeps 0 unless you set it",
            recommended="the default (25 cents on testnets, 0 on mainnet)"),
    Setting("max_payment_cents", "Money", "Largest single payment",
            "No payment above this, whoever asks.", env="EMPORIA_X402_MAX_PAYMENT_CENTS", kind="cents",
            rule="whole cents, 0 or more", recommended="1000 (the default)"),
    Setting("max_daily_cents", "Money", "Most it pays in 24 hours",
            "Total payments allowed in a rolling day.", env="EMPORIA_X402_MAX_DAILY_CENTS", kind="cents",
            rule="whole cents, 0 or more", recommended="5000 (the default)"),
    Setting("payment_approval_cents", "Money", "Ask you above",
            "A payment above this waits for your yes (0 = every payment waits).",
            env="EMPORIA_PAYMENT_APPROVAL_CENTS", kind="cents", zero="always asks",
            rule="whole cents, 0 or more (0 = always ask); `default` = never asks",
            recommended="500 (what setup picks)"),
    Setting("networks", "Money", "Networks it pays on",
            "Where it may pay. Mainnet networks need the mainnet unlock first.",
            env="EMPORIA_X402_PAY_NETWORKS", kind="networks",
            rule="base-sepolia, solana-devnet, tempo-moderato (testnets), comma-separated; "
                 "base, solana, tempo only once mainnet is unlocked; `none` = pays nowhere",
            recommended="the testnets it has a wallet for"),
    Setting("mainnet", "Money", "Real money (mainnet)",
            "Whether it may pay on real networks. Your decision, in your own terminal.",
            kind="info", editable=False, how="emporia wallet mainnet NAME (your own terminal only)"),
    Setting("wake", "Waking", "Wake mode",
            "Which relay events start a turn on their own (each is a model turn).", env=wake.SETTING,
            kind="wake", rule="closed, direct or open", recommended="direct"),
    Setting("rounds", "Waking", "Rounds",
            "Scheduled turns in which it looks around on its own; off or every 2h to 24h.",
            kind="rounds", rule="off, 2h, 4h, 8h, 12h or 24h", recommended="4h"),
    Setting("features", "Tools", "Features",
            "Which optional tool groups it has.", kind="info", editable=False,
            how="emporia features on|off FEATURE [FEATURE ...] (hermes -p NAME emporia features ...)"),
    Setting("playbook", "Playbook", "Playbook preset",
            "Starting bounds: which games, selling, buying. A preset clears the playbook's own changes.",
            env="EMPORIA_PLAYBOOK", kind="preset", rule="custom, player, merchant, scout, host or operator",
            recommended="custom"),
    Setting("playbook_detail", "Playbook", "What it may do on its own",
            "Games, who it answers, selling, buying, pledges.", kind="info", editable=False,
            how="emporia agent playbook NAME --games all|none|set-plays|blotto,chess --answers anyone|nobody "
                "--sells yes|no --buys yes|no --pledges yes|no"),
)
BY_KEY = {s.key: s for s in SETTINGS}
EDITABLE = tuple(s.key for s in SETTINGS if s.editable)


def _count(s: Setting, raw: str) -> int:
    text = raw.strip()
    if not re.fullmatch(r"\d{1,9}", text):
        raise SettingError(f"{s.label}: {raw!r} is not allowed. Allowed: {s.rule}.")
    return int(text)


def _fmt(s: Setting, n: int) -> str:
    if n == 0 and s.zero:
        return s.zero
    word = " cents" if s.kind == "cents" else ""
    return f"{n}{word}{' ' + s.unit if s.unit else ''}"


def _defaults(dotenv: dict[str, str]) -> dict[str, int | None]:
    """What each number setting is when its .env line is empty, from the code that reads it."""
    from emporia import agent_wallet, playbook as P, wake as W
    try:
        base = P.PRESETS.get((dotenv.get("EMPORIA_PLAYBOOK") or "custom").strip().lower(), P.PRESETS["custom"])
    except Exception:  # noqa: BLE001
        base = P.PRESETS["custom"]
    return {"games_per_operator": base.games_per_operator_per_day, "open_games": 3, "daily_turns": 300,
            "thread_limit": W.DEFAULT_THREAD_LIMIT,
            "unattended_spend_cents": P.default_autonomous_spend_cents(dotenv),
            "max_payment_cents": agent_wallet.DEFAULT_MAX_PAYMENT_CENTS,
            "max_daily_cents": agent_wallet.DEFAULT_MAX_DAILY_CENTS, "payment_approval_cents": None}


def _number(s: Setting, dotenv: dict[str, str], default: int | None) -> tuple[str, bool]:
    raw = (dotenv.get(s.env) or "").strip()
    if not raw:
        return ("never asks you" if default is None else _fmt(s, default)), True
    try:
        return _fmt(s, int(raw)), False
    except ValueError:
        return f"{raw!r} (not a number: the runtime uses its default)", False


def rows(dotenv: dict[str, str], name: str = "NAME", rounds: str = "unknown") -> list[dict[str, Any]]:
    """Every setting as a row: its current value, whether that is the default, what it changes."""
    from emporia import autonomy, features as F, networks as N, playbook as P, wake as W
    defaults = _defaults(dotenv)
    out: list[dict[str, Any]] = []
    for i, s in enumerate(SETTINGS, 1):
        value, is_default, change = "", False, ""
        if s.kind in ("count", "cents") and s.key != "unattended_spend_cents" and s.key != "games_per_operator":
            value, is_default = _number(s, dotenv, defaults[s.key])
        elif s.key in ("games_per_operator", "unattended_spend_cents"):
            try:
                pb = P.resolve(dotenv)
                n = (pb.games_per_operator_per_day if s.key == "games_per_operator"
                     else pb.autonomous_spend_cents_per_day)
                value, is_default = _fmt(s, n), not (dotenv.get(s.env) or "").strip()
            except P.PlaybookError as e:
                value = f"unreadable ({e})"
        elif s.key == "new_rooms_per_day":
            value = f"{autonomy.MAX_NEW_ROOMS_PER_DAY} a day (read-only)"
        elif s.kind == "networks":
            nets = N.resolve_list(dotenv.get(s.env) or "")
            value, is_default = (", ".join(N.label(n) for n in nets) or "none (pays nowhere)"), not nets
        elif s.key == "mainnet":
            value = "unlocked (real money)" if N.mainnet_unlocked(dotenv) else "locked (testnets only)"
            change = s.how
        elif s.kind == "wake":
            try:
                value, is_default = W.resolve(dotenv), not (dotenv.get(W.SETTING) or "").strip()
            except W.WakeError as e:
                value = f"unreadable ({e})"
        elif s.kind == "rounds":
            value, is_default = rounds, rounds == "off"
        elif s.key == "features":
            try:
                value = F.summary(F.enabled_from(dotenv))
            except F.FeatureError as e:
                value = f"unreadable ({e})"
            change = s.how
        elif s.kind == "preset":
            value, is_default = (dotenv.get("EMPORIA_PLAYBOOK") or "custom").strip().lower(), not (
                dotenv.get("EMPORIA_PLAYBOOK") or "").strip()
        elif s.key == "playbook_detail":
            try:
                pb = P.resolve(dotenv)
                games = pb.games if isinstance(pb.games, str) else ", ".join(g.split(":")[1] for g in pb.games)
                value = (f"games {games}; answers {pb.answers}; sells {'yes' if pb.sells else 'no'}; "
                         f"buys {'yes' if pb.buys else 'no'}; runs on {pb.runs}; pledges {'yes' if pb.pledges else 'no'}")
            except P.PlaybookError as e:
                value = f"unreadable ({e})"
            change = s.how
        out.append({"number": i, "key": s.key, "group": s.group, "label": s.label, "value": value,
                    "default": is_default, "about": s.about, "editable": s.editable, "env": s.env,
                    "allowed": s.rule, "recommended": s.recommended,
                    "change": (change or s.how).replace("NAME", name)})
    return out


def render(table: list[dict[str, Any]], name: str) -> str:
    """The numbered list, grouped. A read-only row says which command changes it."""
    lines = [f"Settings for {name}: its own limits and behaviour. They are yours to change, never the agent's.", ""]
    group = ""
    for r in table:
        if r["group"] != group:
            group = r["group"]
            lines += [group]
        mark = " (default)" if r["default"] else ""
        lines.append(f" {r['number']:>2}) {r['label']}: {r['value']}{mark}")
        lines.append(f"      {r['about']}" + ("" if r["editable"] or not r["change"] else f" Change: {r['change']}"))
    return "\n".join(lines)


def restart_command(profile_name: str) -> str:
    return "hermes gateway restart" if profile_name == "default" else f"hermes -p {profile_name} gateway restart"


def rounds_value(raw: str) -> str:
    """`off` or an interval, checked by the rounds code itself."""
    from emporia import rounds as R
    text = (raw or "").strip().lower()
    if text == "off":
        return "off"
    try:
        R.hours(text)
    except R.RoundsError as e:
        raise SettingError(f"Rounds: {raw!r} is not allowed. Allowed: {BY_KEY['rounds'].rule} ({e}).") from None
    return text


def plan(key: str, raw: str, dotenv: dict[str, str], name: str = "NAME") -> dict[str, str]:
    """The `.env` changes that setting this one to `raw` makes, after the runtime's own check of the result.
    `default` (or an empty value) puts it back. Rounds is not a .env setting: use `rounds_value`."""
    from emporia import networks as N, playbook as P, wake as W
    s = BY_KEY.get(key)
    if s is None:
        raise SettingError(f"{key!r} is not a setting. Keys: {', '.join(EDITABLE)}.")
    if not s.editable:
        raise SettingError(f"{s.label} is read-only here. " + (f"Change it with: {s.how.replace('NAME', name)}"
                                                                 if s.how else "It is fixed in Emporia's code."))
    if s.kind == "rounds":
        raise SettingError("rounds is not stored in the .env: use rounds_value")
    text = str(raw).strip()
    reset = text.lower() in RESET_WORDS or not text
    if s.kind in ("count", "cents"):
        updates = {s.env: "" if reset else str(_count(s, text))}
    elif s.kind == "wake":
        mode = ""
        if not reset:
            try:
                mode = W.resolve({W.SETTING: text.lower()})
            except W.WakeError:
                raise SettingError(f"{s.label}: {raw!r} is not allowed. Allowed: {s.rule}.") from None
        updates = {s.env: mode}
    elif s.kind == "preset":
        if text.lower() not in P.PRESETS:
            raise SettingError(f"{s.label}: {raw!r} is not allowed. Allowed: {s.rule}.")
        updates = {s.env: text.lower(), **{var: "" for var in P.SETTINGS}}    # a preset starts clean
    else:                                                                     # networks
        nets = [] if reset or text.lower() == "none" else N.resolve_list(text)
        unknown = [n for n in nets if N.family(n) is None]
        if unknown:
            raise SettingError(f"{s.label}: unknown network {', '.join(unknown)}. Allowed: {s.rule}.")
        live = [n for n in nets if not N.is_testnet(n)]
        if live and not N.mainnet_unlocked(dotenv):
            raise SettingError(f"{s.label}: {', '.join(N.label(n) for n in live)} is mainnet (real money), locked "
                               f"for {name}. In your own terminal: emporia wallet mainnet {name}")
        updates = {s.env: ",".join(dict.fromkeys(nets))}
    try:
        P.resolve({**dotenv, **updates})
    except P.PlaybookError as e:
        raise SettingError(str(e)) from None
    return updates
