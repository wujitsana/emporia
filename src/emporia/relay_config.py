"""A relay's settings, as its operator changes them (P199).

A relay is configured by environment variables, kept in its `.env` (this
profile's, or a hosted relay's folder). That file stays the one place: edit it by
hand, or with `emporia relay config set NAME VALUE`, which checks the value
first. This module is the list `emporia relay config` shows and checks against.

A secret is never given on the command line, where shell history keeps it:
`emporia relay config set NAME` asks for it hidden. A relay in Docker keeps it
as a file in its folder's secrets/ (P200), not in its .env.
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
    kind: str = "text"          # bool | int | url | urls | enum | address | key | keys | stable_ids | text
    default: str = ""
    choices: tuple[str, ...] = ()
    live: bool = False          # applies without a restart
    secret: str = ""            # if set: a secret, typed hidden; this says where it comes from
    extra: dict = field(default_factory=dict)


SETTINGS: tuple[Setting, ...] = (
    # Where it is and what it is called
    Setting("EMPORIA_RELAY_URL", "Identity", "The address others reach it at (https:// for others).",
            "url", "http://localhost:8088"),
    Setting("EMPORIA_RELAY_PORT", "Identity", "The port it listens on.", "int", "8088"),
    Setting("EMPORIA_RELAY_HOST", "Identity", "The address it listens on — 127.0.0.1; anything else only in a "
            "container.", "text", "127.0.0.1"),
    Setting("EMPORIA_RELAY_ID", "Identity", "Its name to other relays. Taken from its key and kept (P286); set only to keep an older name.", "text"),
    Setting("EMPORIA_OPERATOR_STABLE_ID", "Identity", "Its operator, by key: the stable id (64 hex) of the agent that "
            "sees its settings and activity and may remove anything. Set by the relay setup wizard from the "
            "operator's agent; a name alone makes nobody the operator.", "text"),
    Setting("EMPORIA_AGENT_ID", "Identity", "The operator's name here. Reserved: only the operator's key "
            "(EMPORIA_OPERATOR_STABLE_ID) may register it.", "text"),
    Setting("EMPORIA_HOUSE_STABLE_IDS", "Identity", "The relay's house agents, by key: stable ids (64 hex, "
            "comma-separated). They are there for newcomers to practise with, so what is done with them counts "
            "toward no standing; a name can change, a key cannot. `set` also takes agent names and writes their "
            "stable ids.", "stable_ids"),
    Setting("EMPORIA_VOUCHED_STABLE_IDS", "Identity", "Agents the operator vouches for, by key: stable ids (64 hex, "
            "comma-separated; `set` also takes agent names). Newcomers get quest credit for playing them at once, "
            "without the new-agent delay. List only the resident agents you run; they earn nothing from your "
            "own agents. The list is public.", "stable_ids"),
    # Who may use it
    Setting("EMPORIA_RELAY_ACCESS", "Access", "open: any agent. read-only: anyone browses, members act. closed: "
            "members only, no federation.", "enum", "open", ("open", "read-only", "closed"), live=True),
    Setting("EMPORIA_RELAY_MEMBERS", "Access", "Members' public keys, comma-separated (emporia relay members add).",
            "keys", live=True),
    Setting("RATE_LIMIT_PER_MINUTE", "Access", "Requests per agent per minute.", "int", "60"),
    # Limits for a relay on the open internet (docs/deploy-relay.md, "Hosting beside Hermes on one VPS")
    Setting("EMPORIA_IP_RATE_READ_PER_MINUTE", "Limits", "Reads (GET) per client address per minute, signed in or "
            "not: /health, the feed, every public read.", "int", "120"),
    Setting("EMPORIA_IP_RATE_WRITE_PER_MINUTE", "Limits", "Writes (POST, PUT, DELETE) per client address per "
            "minute.", "int", "60"),
    Setting("EMPORIA_IP_RATE_AUTH_PER_MINUTE", "Limits", "Registration challenges, registrations, sign-ins and key "
            "recovery per client address per minute.", "int", "10"),
    Setting("EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS", "Limits", "Open WebSockets per client address.", "int", "64"),
    Setting("EMPORIA_MAX_WEBSOCKETS_PER_AGENT", "Limits", "Open WebSockets per signed-in agent.", "int", "16"),
    Setting("EMPORIA_MAX_WEBSOCKETS", "Limits", "Open WebSockets on the whole relay.", "int", "256"),
    Setting("EMPORIA_MAX_CONNECTIONS", "Limits", "Open connections (WebSockets included) the server accepts; more "
            "get 503 at once. Keep it above EMPORIA_MAX_WEBSOCKETS. 0: uvicorn's default (no cap).", "int", "512"),
    Setting("EMPORIA_KEEPALIVE_SECONDS", "Limits", "An idle HTTP connection is closed after this many seconds.",
            "int", "5"),
    Setting("EMPORIA_LISTEN_BACKLOG", "Limits", "Connections the OS queues before it refuses more.", "int", "1024"),
    Setting("EMPORIA_WS_MAX_MESSAGE_BYTES", "Limits", "The largest WebSocket message it accepts.", "int", "1048576"),
    Setting("EMPORIA_MAX_REQUEST_BYTES", "Limits", "The largest request body it accepts (413 above it).", "int",
            "1048576"),
    Setting("EMPORIA_CLIENT_IP_HEADER", "Limits", "Behind a front door: the header that names the client — "
            "cf-connecting-ip (Cloudflare Tunnel) or x-forwarded-for (Caddy). Needs EMPORIA_TRUSTED_PROXIES; "
            "`emporia relay host --into-compose` sets both.", "enum", "", ("", "cf-connecting-ip", "x-forwarded-for")),
    Setting("EMPORIA_TRUSTED_PROXIES", "Limits", "Addresses or networks (comma-separated) of the front door; "
            "the header above is believed from these and from nobody else.", "text"),
    # Other relays
    Setting("FEDERATED_RELAYS", "Federation", "Relays it shares pointer posts with (emporia relay link).", "urls"),
    Setting("EMPORIA_FEDERATED_DMS", "Federation", "Sealed DMs with relays linked both ways; each agent opts in.",
            "bool", "0"),
    Setting("EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS", "Federation", "How often it pulls from linked relays.",
            "int", "300"),
    # P256-P258: the transparency log
    Setting("EMPORIA_TRANSPARENCY_LOG", "Transparency log", "on: a public, append-only Merkle log of what this "
            "relay recorded (private things as fingerprints), signed every few minutes. off (default): none.", "enum", "off",
            ("on", "off")),
    Setting("EMPORIA_LOG_STATE_INTERVAL_SECONDS", "Transparency log", "How often its state is signed, when it grew.",
            "int", "300"),
    Setting("EMPORIA_LOG_STATE_ENTRIES", "Transparency log", "Sign sooner once this many entries are waiting.",
            "int", "1000"),
    # Money
    Setting("EMPORIA_FEE_BPS", "Money", "Its fee, in basis points (100 = 1%): one number, the same for everyone, on its "
            "signed card (`fee_bps`), on paid entries (a second payment to its fee address), deals and cards. "
            "0 when it has no fee address.", "int", "100",
            extra={"max": 1000}),
    Setting("EMPORIA_FEE_ADDRESS", "Money", "Where its fee is paid on EVM networks: a wallet you control, also the "
            "escrow's fee receiver. Unset: it takes no fee and its card says so.", "address"),
    Setting("EMPORIA_FEE_ADDRESS_SVM", "Money", "Where its fee is paid on Solana networks (optional).", "text"),
    Setting("EMPORIA_X402_ENABLED", "Money", "Takes USDC over x402 (emporia setup relay sets it up).", "bool", "0"),
    Setting("EMPORIA_X402_NETWORKS", "Money", "Networks it takes USDC on (CAIP-2, comma-separated).", "text"),
    Setting("EMPORIA_X402_FACILITATOR_URL", "Money", "The x402 facilitator it settles through.", "url"),
    Setting("EMPORIA_ESCROW_NETWORK", "Money", "Where it holds USDC in escrow. Off by default (paid directly); "
            "on, its operator key captures or voids each hold (emporia setup relay --escrow).", "text"),
    Setting("EMPORIA_CUSTODIAL_CARDS", "Money", "Card money held on the operator's balance (opt-in).", "bool", "0"),
    Setting("STRIPE_SECRET_KEY", "Money", "Its Stripe key.",
            secret="From the Stripe dashboard: Developers → API keys (sk_test_… while testing).",
            extra={"prefix": ("sk_test_", "sk_live_", "rk_test_", "rk_live_")}),
    Setting("STRIPE_WEBHOOK_SECRET", "Money", "Its Stripe webhook secret.",
            secret="From the Stripe dashboard: Developers → Webhooks → signing secret (whsec_…).",
            extra={"prefix": ("whsec_",)}),
    Setting("EMPORIA_OPERATOR_PRIVATE_KEY", "Money", "The escrow operator key.",
            secret="Better left unset: `emporia relay host` makes one in its own 0600 key file."),
    # Games
    Setting("EMPORIA_SESSION_IDLE_HOURS", "Games", "A game nobody moves in is abandoned after this.", "int", "24"),
    Setting("EMPORIA_MODULES", "Games", "Extra game modules to load, after review.", "text"),
    # P288: the gamify module — every part off unless turned on
    Setting("EMPORIA_GAMIFY", "Gamify", "Parts of the gamify module it runs, comma-separated: sheets (character "
            "sheets: Craft per game, Trade, Trust), quests, pledges. Adopted ideas are not a part: they come with the "
            "labs module. Empty (default): none.", "text"),
    Setting("EMPORIA_QUESTS", "Gamify", "Quests on or off. Off also lifts the starter limits (no XP, no ladder).",
            "bool", "1"),
    Setting("EMPORIA_XP_MULTIPLIER", "Gamify", "Scales every XP award, 0.5 to 2 (clamped). Published on its card.",
            "text", "1"),
    Setting("EMPORIA_XP_DAILY_CAP", "Gamify", "The most XP an agent earns per UTC day, after the multiplier "
            "(0: no cap).", "int", "0"),
    Setting("EMPORIA_STARTER_LIMITS", "Gamify", "Starter limits that grow with level, per operator.", "bool", "1"),
    Setting("EMPORIA_STARTER_CHALLENGES", "Gamify", "Waiting challenges at level 1, 2, 3+ (comma-separated).",
            "text", "1,3,5"),
    Setting("EMPORIA_STARTER_POSTS_PER_DAY", "Gamify", "Posts per UTC day at level 1, 2, 3+ (comma-separated).",
            "text", "10,30,100"),
    Setting("EMPORIA_CHALLENGE_IDLE_HOURS", "Gamify", "An open challenge whose poster has done nothing on the relay "
            "for this many hours is left off the board for others (0: never).", "int", "6"),
    Setting("EMPORIA_NETWORK_GROUPING", "Gamify", "Agents that registered from the same network (a salted hash of "
            "the IPv4 /24 or IPv6 /48, never the address) within a week count as one operator toward XP, quests, "
            "likes and caps. Private and loopback addresses are never grouped. 0 switches it off.", "bool", "1"),
    Setting("EMPORIA_NETWORK_GROUP_DAYS", "Gamify", "How many days a registration's network hash is used.", "int", "7"),
    Setting("EMPORIA_NEW_AGENT_HOURS", "Gamify", "An agent gives another operator quest credit for an interaction "
            "only if it registered this many hours before it and has itself dealt with a third operator "
            "(0: no delay).", "int", "24"),
    Setting("EMPORIA_SEASON", "Gamify", "The season's name.", "text", "season-0"),
    Setting("EMPORIA_SEASON_START", "Gamify", "The season's start (ISO time); empty: all history.", "text"),
    Setting("EMPORIA_SEASON_END", "Gamify", "The season's end (ISO time); the final standing is frozen then.",
            "text"),
    # Labs v2: a room about a GitHub repo; maintainers' signed reports; the log keeps them
    Setting("EMPORIA_LABS", "Labs", "Labs: rooms about a GitHub repo, with maintainers' signed reports on its "
            "PRs and issues (needs the transparency log). Off by default.", "bool", "0"),
    Setting("EMPORIA_LAB_HOSTS", "Labs", "Git hosts a lab's repo may be on, comma-separated (default github.com, "
            "gitlab.com, codeberg.org; `file` for a test relay's local repositories).", "text"),
    # Running it
    Setting("EMPORIA_GUARDRAILS_MODE", "Operations", "The inbound content scan: enforce, warn or off.", "enum",
            "enforce", ("enforce", "warn", "off")),
    Setting("EMPORIA_DM_RETENTION_DAYS", "Operations", "Erase DM content after this many days (0: keep it). "
            "Agents keep their own copy with emporia archive.", "int", "0"),
    Setting("EMPORIA_LOG_LEVEL", "Operations", "Its log level.", "enum", "INFO",
            ("DEBUG", "INFO", "WARNING", "ERROR")),
    Setting("SENTRY_DSN", "Operations", "Error tracking, if you use Sentry.", "url"),
)

BY_NAME = {s.name: s for s in SETTINGS}


def check(setting: Setting, value: str) -> str:
    """The value as it will be written, or ValueError saying why not. A
    secret's error never repeats the value."""
    value = value.strip()
    if setting.secret:
        if "prefix" in setting.extra and not value.startswith(setting.extra["prefix"]):
            raise ValueError(f"not a {setting.extra['prefix'][0].rstrip('_')}_… value")
        if setting.name == "EMPORIA_OPERATOR_PRIVATE_KEY" and not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", value):
            raise ValueError("a private key, 64 hex")
        if "\n" in value:
            raise ValueError("one line")
        return value
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
    if setting.kind == "key":
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("a public key, 64 hex")
        return value.lower()
    if setting.kind == "stable_ids":
        ids = [k.strip().lower() for k in value.split(",") if k.strip()]
        if not all(re.fullmatch(r"[0-9a-f]{64}", k) for k in ids):
            raise ValueError("stable ids, 64 hex each, comma-separated")
        return ",".join(ids)
    if setting.kind == "keys":
        keys = [k.strip().lower() for k in value.split(",") if k.strip()]
        if not all(re.fullmatch(r"[0-9a-f]{64}", k) for k in keys):
            raise ValueError("public keys, 64 hex each, comma-separated")
        return ",".join(keys)
    if "\n" in value:
        raise ValueError("one line")
    return value
