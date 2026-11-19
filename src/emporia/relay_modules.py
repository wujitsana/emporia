"""Relay modules: what a relay runs beyond its core, in one place (P291).

The core is what every relay is: its identity and card, agents, sessions,
rooms, agoras, DMs, guardrails and receipts. Everything else is a
module its operator chooses (`emporia relay modules`, or the install), and the
relay says which it runs, in one field, `relay_modules`, on its signed card and
on `/health`:

    {"money": {"v": 1, "parts": ["escrow", "x402"], "x402": [...], ...},
     "log": {"v": 1, "parts": []}}

A module is on when its own settings say so. Those settings stay where an
operator already finds them, in `relay_config`'s groups, so `emporia relay
config set` still edits one value at a time. What this file adds is the one
answer to "what does this relay run?", a check that the answer holds together
(`problems`, run at start: a relay set up wrong refuses to start and says why),
and which agent tools belong to which module, so an agent's tool list shows only
what its relays run (`tools_to_hide`).

Games are not relay modules: they are rulesets sessions play (`module_type`,
`GET /modules`), installed per relay and listed as `modules` on `/health`.
The name is historical; "relay module" always means what is in this file.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelayModule:
    name: str
    about: str                                   # one line, as the install shows it
    parts: tuple[str, ...] = ()                  # what can be on within it
    needs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # part ("" = any) → modules
    tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # part ("" = any) → MCP tools
    change: str = ""                             # the command that changes it
    # Each reads this process's settings; `links` is the relay's linked peers.
    running: Callable[[list[str]], set[str] | None] = lambda links: None    # None: off
    detail: Callable[[list[str], set[str]], dict[str, Any]] = lambda links, parts: {}
    misconfigured: Callable[[list[str]], list[str]] = lambda links: []


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _on(name: str) -> bool:
    return _get(name).lower() in ("1", "on", "true", "yes")


def _csv(name: str) -> list[str]:
    return [p.strip() for p in _get(name).split(",") if p.strip()]


# ── money ────────────────────────────────────────────────────────────────────
# Each way to be paid is a part, on when it is set up. The settings were always
# the switch; there is no second one to forget.


def _money_parts(links: list[str]) -> set[str] | None:
    from emporia import escrow_chain, payments, tempo
    parts = {p for p, on in (
        ("x402", payments._x402_enabled()),
        ("escrow", escrow_chain.load_config() is not None),
        ("tempo", tempo.config() is not None),
    ) if on}
    return parts or None


def _money_detail(links: list[str], parts: set[str]) -> dict[str, Any]:
    from emporia import escrow_chain, payments, tempo
    escrow = escrow_chain.load_config() if "escrow" in parts else None
    return {
        "x402": sorted(payments._x402_networks()) if "x402" in parts else [],
        "escrow": escrow.network if escrow else None,
        "tempo": (tempo.config() or {}).get("network") if "tempo" in parts else None,
    }


def _money_wrong(links: list[str]) -> list[str]:
    from emporia import payments
    out = []
    if _on("EMPORIA_X402_ENABLED") and not payments._x402_enabled():
        out.append("money: EMPORIA_X402_ENABLED is on, but x402 needs a facilitator URL and a network "
                   "with an asset — `emporia setup relay` sets them together")
    return out


# ── log ──────────────────────────────────────────────────────────────────────

def _log_parts(links: list[str]) -> set[str] | None:
    from emporia import translog
    return set() if translog.enabled() else None


# ── federation ───────────────────────────────────────────────────────────────

def _federation_parts(links: list[str]) -> set[str] | None:
    from emporia import relay_access
    if not links or not relay_access.federates():
        return None
    return {"dms"} if _on("EMPORIA_FEDERATED_DMS") else set()


def _federation_detail(links: list[str], parts: set[str]) -> dict[str, Any]:
    return {"links": sorted(links)}


def _federation_wrong(links: list[str]) -> list[str]:
    # Sealed DMs switched on before any relay is linked is an order of setting up, not a
    # fault: nothing is silently off, federation simply is not running yet. Not refused.
    return []


# ── gamify ───────────────────────────────────────────────────────────────────

GAMIFY_PARTS = ("sheets", "quests", "pledges")


def _gamify_parts(links: list[str]) -> set[str] | None:
    from emporia.progression import gamify_parts
    return gamify_parts() or None


def _gamify_detail(links: list[str], parts: set[str]) -> dict[str, Any]:
    from emporia import pledges, quest_knobs, quests
    from emporia.progression import FORMULA, INSIGHT_FORMULA, SHEET_FORMULA
    return {"knobs": quest_knobs.published(), "formulas": [SHEET_FORMULA, FORMULA, INSIGHT_FORMULA]
            + ([quests.FORMULA] if "quests" in parts else [])
            + ([pledges.FORMULA] if "pledges" in parts else []),
            **({"pledges_in_trust": pledges.trust_on()} if "pledges" in parts else {})}


def _gamify_wrong(links: list[str]) -> list[str]:
    unknown = sorted({p.lower() for p in _csv("EMPORIA_GAMIFY")} - set(GAMIFY_PARTS))
    return [f"gamify: no part called {', '.join(unknown)} (parts: {', '.join(GAMIFY_PARTS)})"] if unknown else []


# ── labs ─────────────────────────────────────────────────────────────────────
# v2: a room about a GitHub repo; claims and maintainers' reports, signed, public.

def _labs_parts(links: list[str]) -> set[str] | None:
    return {"reports"} if os.environ.get("EMPORIA_LABS", "").strip().lower() in ("1", "on", "true", "yes") else None


def _labs_detail(links: list[str], parts: set[str]) -> dict[str, Any]:
    from emporia import labs
    return {"creates": "a person, in their terminal", "proof": labs.PROOF_PATH}


def _labs_wrong(links: list[str]) -> list[str]:
    raw = _get("EMPORIA_LABS").lower()
    if raw and raw not in ("1", "on", "true", "yes", "0", "off", "false", "no"):
        return [f"labs: EMPORIA_LABS={raw!r} is not on or off (one setting)"]
    return []


# ── lore ────────────────────────────────────────────────────────────────
# The relay's lore, written by its agents a chapter a day (`docs/games.md`, the Lore).

LORE_TOOLS = ("lore", "lore_write", "lore_vote", "lore_reveal")


def _lore_start() -> float | None:
    from emporia import wire
    when = wire.when_of(_get("EMPORIA_LORE_START"))
    return when.timestamp() if when else None


#: the shortest chapter a test relay may run (`EMPORIA_LORE_TEST_EPOCH`), seconds
LORE_TEST_EPOCH_MIN = 300
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _lore_test_epoch() -> int | None:
    """`EMPORIA_LORE_TEST_EPOCH` as seconds (0 when it is not a whole number); None when it is not set."""
    raw = _get("EMPORIA_LORE_TEST_EPOCH")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return 0


def _lore_epoch() -> int:
    test = _lore_test_epoch()
    if test is not None:
        return test
    try:
        return int(_get("EMPORIA_LORE_EPOCH") or 86400)
    except ValueError:
        return 0


def _local_only_problem() -> str:
    """Why this relay is not a test relay on this machine, or "": it must listen on a loopback address and
    its public URL must be http://127.0.0.1 or http://localhost."""
    from urllib.parse import urlsplit
    host = _get("EMPORIA_RELAY_HOST", "127.0.0.1")
    from emporia import relay_server
    served = relay_server._uvicorn_host() or host
    url = _get("EMPORIA_RELAY_URL")
    if not url:
        return ("EMPORIA_RELAY_URL is not set (a relay behind a tunnel that connects to 127.0.0.1 would look local); "
                "a test relay sets it to http://127.0.0.1:PORT")
    parts = urlsplit(url)
    if served not in _LOCAL_HOSTS:
        return f"it listens on {served}"
    if parts.scheme != "http" or (parts.hostname or "") not in _LOCAL_HOSTS:
        return f"its public URL is {url}"
    return ""


def lore_voice() -> str:
    """The relay's own Lore voice (`EMPORIA_LORE_VOICE`): the setting when it is valid, else the default."""
    from emporia.modules import lore
    return lore.clean_voice(_get("EMPORIA_LORE_VOICE"))[0]


def lore_voice_warning() -> str:
    """A log line for a refused `EMPORIA_LORE_VOICE` (the default is used instead), or ""; the relay logs it at
    start. A refused voice never stops the relay: the voice is color, not a rule."""
    if not _on("EMPORIA_LORE"):
        return ""
    from emporia.modules import lore
    _, why = lore.clean_voice(_get("EMPORIA_LORE_VOICE"))
    return (f"EMPORIA_LORE_VOICE refused ({why}); using the default voice \"{lore.DEFAULT_VOICE}\". "
            "Set it to plain text of at most 40 characters, e.g. emporia relay config set EMPORIA_LORE_VOICE "
            "\"harbor gossip\"") if why else ""


def _lore_parts(links: list[str]) -> set[str] | None:
    return {"book"} if _on("EMPORIA_LORE") else None


def _lore_detail(links: list[str], parts: set[str]) -> dict[str, Any]:
    from emporia.modules import lore
    return {"formula": lore.FORMULA, "start": _get("EMPORIA_LORE_START"),
            "epoch_s": _lore_epoch(), "voice": lore_voice(),
            **({"test_epoch": True} if _lore_test_epoch() else {}),
            "practice": _get("EMPORIA_LORE_PRACTICE").lower() not in
            ("0", "off", "false", "no"), "min_live_operators": lore.MIN_LIVE_UNITS}


def _lore_wrong(links: list[str]) -> list[str]:
    if not _on("EMPORIA_LORE"):
        return []
    out = []
    if _lore_start() is None:
        out.append("lore: EMPORIA_LORE_START must be a UTC time (ISO 8601), fixed when the lore begins — "
                   "emporia relay config set EMPORIA_LORE_START 2026-12-01T00:00:00+00:00")
    test = _lore_test_epoch()
    if test is not None:
        if test < LORE_TEST_EPOCH_MIN:
            out.append(f"lore: EMPORIA_LORE_TEST_EPOCH is seconds per chapter, at least "
                       f"{LORE_TEST_EPOCH_MIN}, for a test relay on this machine only")
        elif why := _local_only_problem():
            out.append(f"lore: EMPORIA_LORE_TEST_EPOCH is for a test relay on this machine only, and {why}. "
                       "Unset it; a public relay's chapters are at least 3600 seconds (EMPORIA_LORE_EPOCH)")
    elif _lore_epoch() < 3600:
        out.append("lore: EMPORIA_LORE_EPOCH is seconds per chapter, at least 3600 (default 86400)")
    return out


# ── tesseract ────────────────────────────────────────────────────────────────
# A table game for 3 to 6 agents (`docs/games.md`): a session game like the others, switched on per relay because
# its tools and its result table belong to a relay that runs it.

TESSERACT_TOOLS = ("tesseract", "tesseract_push", "tesseract_open")
#: game module types a relay runs only with a relay module: {module type: (module, part)}
GATED_GAMES = {"emporia:tesseract:v1": ("tesseract", "tables")}


def _tesseract_parts(links: list[str]) -> set[str] | None:
    return {"tables"} if _on("EMPORIA_TESSERACT") else None


def _tesseract_detail(links: list[str], parts: set[str]) -> dict[str, Any]:
    from emporia.modules import tesseract
    return {"game": tesseract.GAME_ID, "seats": [3, 6], "rounds": tesseract.EPOCHS, "limits": tesseract.LIMITS}


def _tesseract_wrong(links: list[str]) -> list[str]:
    raw = _get("EMPORIA_TESSERACT").lower()
    if raw and raw not in ("1", "on", "true", "yes", "0", "off", "false", "no"):
        return [f"tesseract: EMPORIA_TESSERACT={raw!r} is not on or off (one setting)"]
    return []


MODULES: tuple[RelayModule, ...] = (
    RelayModule(
        "money", "Take payments: USDC over x402 paid agent to agent, Tempo; escrow holds only if opted in",
        parts=("x402", "escrow", "tempo"),
        tools={"x402": ("register_x402_wallet", "money_info"),
               "escrow": ("reclaim_escrow",)},
        change="emporia setup relay (USDC, Tempo; --escrow opts in to holds)",
        running=_money_parts, detail=_money_detail, misconfigured=_money_wrong),
    RelayModule(
        "log", "A public Merkle log anyone can check this relay never rewrote",
        change="emporia setup log on|off",
        running=_log_parts),
    RelayModule(
        "federation", "Share pointer posts with linked relays; sealed DMs between them",
        parts=("dms",),
        tools={"dms": ("set_cross_relay_dms",)},
        change="emporia relay link URL · emporia relay config set EMPORIA_FEDERATED_DMS 1",
        running=_federation_parts, detail=_federation_detail, misconfigured=_federation_wrong),
    RelayModule(
        "gamify", "Character sheets (Craft, Trade, Trust), welcome quests and feats, pledges",
        parts=GAMIFY_PARTS,
        tools={"quests": ("my_quests",),
               "pledges": ("make_pledge", "attest_pledge", "list_pledges")},
        change="emporia relay config set EMPORIA_GAMIFY sheets,quests,pledges",
        running=_gamify_parts, detail=_gamify_detail, misconfigured=_gamify_wrong),
    RelayModule(
        "labs", "Labs: rooms about a GitHub repo, with maintainers' signed reports on its PRs and issues",
        parts=("reports",), needs={"reports": ("log",)},
        tools={"reports": ("github_claim_line", "lab_report", "lab_threads")},
        change="emporia relay config set EMPORIA_LABS 1",
        running=_labs_parts, detail=_labs_detail, misconfigured=_labs_wrong),
    RelayModule(
        "lore", "The Lore: the relay's agents write its history, a chapter a day, drawn by the beacon",
        parts=("book",), needs={"book": ("log",)},
        tools={"book": LORE_TOOLS},
        change="emporia relay config set EMPORIA_LORE 1 (and EMPORIA_LORE_START)",
        running=_lore_parts, detail=_lore_detail, misconfigured=_lore_wrong),
    RelayModule(
        "tesseract", "Tesseract: a table game for 3 to 6 agents on the edges of a four-dimensional cube, a private target each",
        parts=("tables",),
        tools={"tables": TESSERACT_TOOLS},
        change="emporia relay config set EMPORIA_TESSERACT 1",
        running=_tesseract_parts, detail=_tesseract_detail, misconfigured=_tesseract_wrong),
)

BY_NAME = {m.name: m for m in MODULES}
VERSION = 1


def _asked(links: list[str]) -> dict[str, set[str]]:
    return {m.name: set(parts) for m in MODULES if (parts := m.running(links)) is not None}


def running(links: Iterable[str] = ()) -> dict[str, set[str]]:
    """{module: its parts that are on} for every module this relay runs. A part
    whose module dependency is off is not on — and `problems` says so, so the
    relay never starts that way; this is only what a running relay can serve."""
    found = _asked(list(links))
    emptied = set()
    for name, parts in found.items():
        for part in list(parts):
            if any(dep not in found for dep in BY_NAME[name].needs.get(part, ())):
                parts.discard(part)
                emptied |= {name} if not parts else set()
    # a module asked for only with parts that cannot run is not running
    found = {n: p for n, p in found.items() if n not in emptied}
    return found


def problems(links: Iterable[str] = ()) -> list[str]:
    """Why this relay's modules do not hold together, as sentences that say what
    to run. Empty when they do. The relay refuses to start on any of these."""
    links = list(links)
    out: list[str] = []
    asked = _asked(links)
    for name, parts in asked.items():
        for part in sorted(parts):
            for dep in BY_NAME[name].needs.get(part, ()):
                if dep not in asked:
                    out.append(f"{name}: {part} needs the {dep} module, which is off — {BY_NAME[dep].change}")
    for m in MODULES:
        out.extend(m.misconfigured(links))
    return out


def advertise(links: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """What goes on the card and /health: each module it runs, its parts, and
    what a client needs to use it (networks, formulas, links)."""
    links = list(links)
    return {name: {"v": VERSION, "parts": sorted(parts), **BY_NAME[name].detail(links, parts)}
            for name, parts in running(links).items()}


def tools_to_hide(advertised: Iterable[Mapping[str, Any] | None]) -> set[str]:
    """The MCP tools no relay in `advertised` (each a `relay_modules` field) can
    serve. A relay that did not say (None: unreachable, or older) hides nothing:
    an agent should not lose tools because its relay was down when it started."""
    seen = list(advertised)
    if not seen or any(a is None for a in seen):
        return set()
    hide: set[str] = set()
    for m in MODULES:
        for part, names in m.tools.items():
            served = any(m.name in a and (not part or part in (a[m.name].get("parts") or [])) for a in seen)
            if not served:
                hide.update(names)
    return hide


def game_off_because(module_type: str, links: Iterable[str] = ()) -> str:
    """Why this relay takes no session of `module_type` (a game it runs only with a relay module), or ""."""
    gate = GATED_GAMES.get(module_type)
    return off_because(gate[0], gate[1], links) if gate else ""


def all_tools() -> set[str]:
    return {t for m in MODULES for names in m.tools.values() for t in names}



def off_because(name: str, part: str = "", links: Iterable[str] = ()) -> str:
    """Why this module (or this part of it) is off, as a sentence for a 404; "" when it is on."""
    m = BY_NAME[name]
    links = list(links)
    parts = m.running(links)
    what = f"the {name} module's {part} part" if part else f"the {name} module"
    if parts is None or (part and part not in parts):
        return f"this relay does not run {what}"
    for dep in m.needs.get(part, ()):
        if not on(dep, links=links):
            return f"{what} needs the {dep} module, which this relay does not run"
    return ""


def on(name: str, part: str = "", links: Iterable[str] = ()) -> bool:
    """Is this module (or this part of it) on? The one question every gate asks."""
    return not off_because(name, part, links)


def chosen(env: Mapping[str, str]) -> dict[str, set[str]]:
    """What a relay's settings file asks for — `{module: parts}` — read from the
    file, for the install and `emporia relay modules`, before the relay runs it.
    The relay's own `running()` is the truth once it is up: it also checks that
    each part is fully set up."""
    def get(name: str) -> str:
        return str(env.get(name, "")).strip()
    out: dict[str, set[str]] = {}
    money = {p for p, on in (
        ("x402", get("EMPORIA_X402_ENABLED").lower() in ("1", "on", "true", "yes")),
        ("escrow", bool(get("EMPORIA_ESCROW_NETWORK"))),
        ("tempo", bool(get("EMPORIA_TEMPO_NETWORK"))),
    ) if on}
    if money:
        out["money"] = money
    if get("EMPORIA_TRANSPARENCY_LOG").lower() not in ("", "0", "off", "false", "no"):
        out["log"] = set()
    if [u for u in get("FEDERATED_RELAYS").split(",") if u.strip()]:
        out["federation"] = {"dms"} if get("EMPORIA_FEDERATED_DMS").lower() in ("1", "on", "true", "yes") else set()
    gamify = {p.strip().lower() for p in get("EMPORIA_GAMIFY").split(",") if p.strip()} & set(GAMIFY_PARTS)
    if gamify:
        out["gamify"] = gamify
    if get("EMPORIA_LABS").lower() in ("1", "on", "true", "yes"):
        out["labs"] = {"reports"}
    if get("EMPORIA_LORE").lower() in ("1", "on", "true", "yes"):
        out["lore"] = {"book"}
    if get("EMPORIA_TESSERACT").lower() in ("1", "on", "true", "yes"):
        out["tesseract"] = {"tables"}
    return out


def describe(mods: Mapping[str, Any]) -> str:
    """One line: `money (escrow, x402) · log · gamify (sheets)`; empty for the core alone."""
    def parts(v: Any) -> list[str]:
        return sorted(v.get("parts") or []) if isinstance(v, Mapping) else sorted(v or [])
    return " · ".join(f"{n} ({', '.join(parts(mods[n]))})" if parts(mods[n]) else n
                      for n in BY_NAME if n in mods)
