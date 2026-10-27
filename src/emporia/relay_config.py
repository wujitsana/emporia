"""A relay's settings, as its operator changes them (P199).

A relay is configured by environment variables, kept in its `.env` (this
profile's, or a hosted relay's folder). That file stays the one place: edit it by
hand, or with `emporia relay config set NAME VALUE`, which checks the value
first. This module is the list `emporia relay config` shows and checks against.

Secrets are not set here — a value typed on a command line lands in shell
history. Each has its own way in (`emporia setup stripe`, the operator key file).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class Setting:
    name: str
    group: str
    about: str
    kind: str = "text"          # bool | int | url | urls | enum | address | keys | text
    default: str = ""
    choices: tuple[str, ...] = ()
    live: bool = False          # applies without a restart
    secret: str = ""            # if set: how to set it instead
    extra: dict = field(default_factory=dict)


SETTINGS: tuple[Setting, ...] = (
    # Where it is and what it is called
    Setting("EMPORIA_RELAY_URL", "Identity", "The address others reach it at (https:// for others).",
            "url", "http://localhost:8088"),
    Setting("EMPORIA_RELAY_PORT", "Identity", "The port it listens on.", "int", "8088"),
    Setting("EMPORIA_RELAY_HOST", "Identity", "The address it listens on — 127.0.0.1; anything else only in a "
            "container.", "text", "127.0.0.1"),
    Setting("EMPORIA_RELAY_ID", "Identity", "Its name to other relays; set one before linking.", "text"),
    Setting("EMPORIA_AGENT_ID", "Identity", "Its operator: the agent (or owner key) that sees its settings and "
            "activity and may remove anything.", "text"),
    # Who may use it
    Setting("EMPORIA_RELAY_ACCESS", "Access", "open: any agent. read-only: anyone browses, members act. closed: "
            "members only, no federation.", "enum", "open", ("open", "read-only", "closed"), live=True),
    Setting("EMPORIA_RELAY_MEMBERS", "Access", "Members' public keys, comma-separated (emporia relay members add).",
            "keys", live=True),
    Setting("EMPORIA_REQUIRE_NOUS", "Access", "Only agents with a verified Nous sign-in may register.", "bool", "0"),
    Setting("EMPORIA_WRITE_REQUIRES_NOUS", "Access", "Agents without Nous verification may only read.", "bool", "0"),
    Setting("RATE_LIMIT_PER_MINUTE", "Access", "Requests per agent per minute.", "int", "60"),
    # Other relays
    Setting("FEDERATED_RELAYS", "Federation", "Relays it shares listings with (emporia relay link).", "urls"),
    Setting("EMPORIA_FEDERATED_DMS", "Federation", "Sealed DMs with relays linked both ways; each agent opts in.",
            "bool", "0"),
    Setting("EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS", "Federation", "How often it pulls from linked relays.",
            "int", "300"),
    # Money
    Setting("OPERATOR_FEE_BPS", "Money", "Its fee, in basis points (250 = 2.5%).", "int", "250",
            extra={"max": 2000}),
    Setting("EMPORIA_X402_ENABLED", "Money", "Takes USDC over x402 (emporia setup relay sets it up).", "bool", "0"),
    Setting("EMPORIA_X402_NETWORKS", "Money", "Networks it takes USDC on (CAIP-2, comma-separated).", "text"),
    Setting("EMPORIA_X402_FACILITATOR_URL", "Money", "The x402 facilitator it settles through.", "url"),
    Setting("EMPORIA_ESCROW_FEE_RECEIVER", "Money", "The wallet its fee is paid to.", "address"),
    Setting("EMPORIA_ESCROW_NETWORK", "Money", "Where it holds USDC in escrow.", "text"),
    Setting("EMPORIA_CUSTODIAL_CARDS", "Money", "Card money held on the operator's balance (opt-in).", "bool", "0"),
    Setting("EMPORIA_CARD_GAME_STAKES", "Money", "Card payments for game stakes (opt-in).", "bool", "0"),
    Setting("STRIPE_SECRET_KEY", "Money", "Its Stripe key.", secret="emporia setup stripe (typed hidden)"),
    Setting("STRIPE_WEBHOOK_SECRET", "Money", "Its Stripe webhook secret.", secret="emporia setup stripe"),
    Setting("EMPORIA_OPERATOR_PRIVATE_KEY", "Money", "The escrow operator key.",
            secret="emporia setup relay (written to its own 0600 file)"),
    # Games
    Setting("EMPORIA_ENGINE_GAME_STAKES", "Games", "Stakes on games an engine wins (chess); off by default.",
            "bool", "0"),
    Setting("EMPORIA_SESSION_IDLE_HOURS", "Games", "A game nobody moves in is abandoned after this.", "int", "24"),
    Setting("EMPORIA_MODULES", "Games", "Extra game modules to load, after review.", "text"),
    # Running it
    Setting("EMPORIA_GUARDRAILS_MODE", "Operations", "The inbound content scan: enforce, warn or off.", "enum",
            "enforce", ("enforce", "warn", "off")),
    Setting("EMPORIA_LOG_LEVEL", "Operations", "Its log level.", "enum", "INFO",
            ("DEBUG", "INFO", "WARNING", "ERROR")),
    Setting("EMPORIA_SERVE_UI", "Operations", "Serve the built-in dashboard at /ui/ though others can reach it.",
            "bool", "0"),
    Setting("SENTRY_DSN", "Operations", "Error tracking, if you use Sentry.", "url"),
)

BY_NAME = {s.name: s for s in SETTINGS}


def check(setting: Setting, value: str) -> str:
    """The value as it will be written, or ValueError saying why not."""
    value = value.strip()
    if setting.kind == "bool":
        if value.lower() in ("1", "true", "yes", "on"):
            return "1"
        if value.lower() in ("0", "false", "no", "off", ""):
            return "0"
        raise ValueError("on or off (1 or 0)")
    if setting.kind == "int":
        if not re.fullmatch(r"\d+", value):
            raise ValueError("a whole number")
        if "max" in setting.extra and int(value) > setting.extra["max"]:
            raise ValueError(f"at most {setting.extra['max']}")
        return value
    if setting.kind == "enum":
        if value not in setting.choices:
            raise ValueError(f"one of {', '.join(setting.choices)}")
        return value
    if setting.kind in ("url", "urls"):
        urls = [u.strip().rstrip("/") for u in value.split(",") if u.strip()] if setting.kind == "urls" else [value]
        for u in urls:
            parts = urlparse(u)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError(f"{u!r} is not an http(s) URL")
        return ",".join(urls)
    if setting.kind == "address":
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            raise ValueError("an address, 0x and 40 hex")
        return value
    if setting.kind == "keys":
        keys = [k.strip().lower() for k in value.split(",") if k.strip()]
        if not all(re.fullmatch(r"[0-9a-f]{64}", k) for k in keys):
            raise ValueError("public keys, 64 hex each, comma-separated")
        return ",".join(keys)
    if "\n" in value:
        raise ValueError("one line")
    return value
