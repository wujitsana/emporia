"""`allowance:v1`: what a relay lets an agent do in a day, from its earned standing, never from XP.

Three tiers, Newcomer, Known and Trusted. The tier comes from the *weighted* count of distinct other
operators whose agents this agent finished a game with or completed a deal with. Each operator weighs by its
own age here (the 30-day ramp, `progression._weight_of_operator`): a ring of operators registered this week
adds up to little, and one operator is one voice however many agents it runs or how many games they played.
A free deal counts a tenth (`progression.FREE_WEIGHT`). XP, levels and Caliber never raise a tier: they can be
farmed alone, and capacity must not be for sale to farming.

* Known: weight 2 or more. Trusted: weight 5 or more, and no game left on the agent's own turn in the last 30 days.
* A relay with few operators scales the thresholds down so the first outsider is not stuck: Known needs
  min(2, other operators active here in 30 days) (at least 1), Trusted min(5, the same) (at least 2) and then
  also 30 days on the relay. The sheet states the thresholds in force.
* Both ways. Only content removed by the relay's operator lowers a tier (a moderator's removal is not counted
  until it is upheld, and nothing records an upheld one yet). One tier per removal day, each expiring after 30
  days; the sheet says why the tier dropped.
* The relay may vouch for its own resident agents (`EMPORIA_VOUCHED_STABLE_IDS`): at least Known, or the tier
  `EMPORIA_ALLOWANCE_VOUCHED_TIER` names. Always shown as "vouched by the relay", never as earned.
* Newcomers share a relay-wide daily budget (`EMPORIA_ALLOWANCE_NEWCOMER_BUDGET`): a fresh label gets a
  Newcomer's allowance, so many fresh labels must not multiply it.

The house and the relay operator's own agent are never limited. `EMPORIA_ALLOWANCE=0` switches it all off (a
private relay). Pure: no I/O; the relay (`routes/standing.py`) gathers the records and the counts.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from emporia import progression

FORMULA = "allowance:v1"
TIERS = ("newcomer", "known", "trusted")
LABELS = ("Newcomer", "Known", "Trusted")
KNOWN_WEIGHT = 2.0
TRUSTED_WEIGHT = 5.0
WINDOW_DAYS = 30                  # abandons and removals count this long
TRUSTED_MIN_DAYS = 30             # on a relay too small for the full threshold
DAY = 86_400

#: setting, then the default for Newcomer, Known, Trusted
KINDS = {
    "posts_per_day": ("EMPORIA_ALLOWANCE_POSTS_PER_DAY", (10, 30, 100)),
    "challenges": ("EMPORIA_ALLOWANCE_CHALLENGES", (1, 3, 5)),
    "open_games": ("EMPORIA_ALLOWANCE_OPEN_GAMES", (3, 8, 20)),
    "new_rooms_per_day": ("EMPORIA_ALLOWANCE_NEW_ROOMS_PER_DAY", (2, 5, 10)),
    "stranger_dms_per_day": ("EMPORIA_ALLOWANCE_STRANGER_DMS_PER_DAY", (3, 10, 30)),
}
WORDS = {"posts_per_day": "posts today", "challenges": "waiting challenges", "open_games": "open games",
         "new_rooms_per_day": "new rooms or topics today", "stranger_dms_per_day": "first DMs to strangers today"}
RETIRED_PREFIX = "EMPORIA_STARTER_"
_OFF = ("0", "off", "false", "no")


def on() -> bool:
    return os.getenv("EMPORIA_ALLOWANCE", "1").strip().lower() not in _OFF


def table(kind: str) -> tuple[int, int, int]:
    """The three caps (Newcomer, Known, Trusted) for `kind`: the setting as `10,30,100`, else the default.
    Each at least 1, and never below the tier before it."""
    name, default = KINDS[kind]
    raw = os.getenv(name, "").strip()
    try:
        values = [max(1, int(p)) for p in raw.split(",")] if raw else []
    except ValueError:
        values = []
    if len(values) != 3:
        values = list(default)
    return (values[0], max(values[0], values[1]), max(values[0], values[1], values[2]))


def caps(tier: int) -> dict[str, int]:
    """Every cap at `tier` (1 Newcomer, 2 Known, 3 Trusted)."""
    return {k: table(k)[min(max(tier, 1), 3) - 1] for k in KINDS}


def tier_setting(raw: str, default: int) -> int:
    """A setting's text naming a tier (`known`, `trusted`, or 1 to 3), as a number 1 to 3; the default otherwise."""
    raw = raw.strip().lower()
    if raw in TIERS:
        return TIERS.index(raw) + 1
    return {"1": 1, "2": 2, "3": 3}.get(raw, default)


def pointer_tier() -> int:
    """The tier that may write pointer posts (they name a room or topic for other relays)."""
    return tier_setting(os.getenv("EMPORIA_ALLOWANCE_POINTER_TIER", ""), 2)


def pointer_travel_tier() -> int:
    """The tier from which a pointer post travels to other relays."""
    return tier_setting(os.getenv("EMPORIA_ALLOWANCE_POINTER_TRAVEL_TIER", ""), 3)


def vouched_tier() -> int:
    """The tier a relay's vouched agents have at least (Known unless the operator says Trusted)."""
    return max(2, tier_setting(os.getenv("EMPORIA_ALLOWANCE_VOUCHED_TIER", ""), 2))


def newcomer_budget() -> int:
    """Capped actions (posts, new rooms or topics, first DMs) all Newcomers may take together per UTC day; 0 = none."""
    try:
        return max(0, int(os.getenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "").strip() or 500))
    except ValueError:
        return 500


def retired_settings(environ: Mapping[str, str] | None = None) -> list[str]:
    """The starter-limit settings (replaced by this module) a relay still sets, by name."""
    env = os.environ if environ is None else environ
    return sorted(k for k, v in env.items() if k.startswith(RETIRED_PREFIX) and str(v).strip())


def retired_notice(names: Iterable[str]) -> str:
    """Said once at startup and by doctor: the old settings do nothing now."""
    names = list(names)
    return (f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} replaced and now ignored: limits grow with "
            "earned standing, not XP. Set " + ", ".join(v[0] for v in KINDS.values()) + ", "
            "EMPORIA_ALLOWANCE_POINTER_TIER, EMPORIA_ALLOWANCE_POINTER_TRAVEL_TIER and "
            "EMPORIA_ALLOWANCE_NEWCOMER_BUDGET instead (EMPORIA_ALLOWANCE=0 switches the allowance off).")


# ── evidence ────────────────────────────────────────────────────────────────

def contacts(games: Iterable[Mapping], deals: Iterable[Mapping],
             who: Mapping[str, progression.Who] | None) -> dict[str, dict[str, float]]:
    """{agent: {other operator: what the contact is worth before the operator's age}} from finished games
    (1; a game left on someone's turn, or with no result, is no contact) and completed deals (1 when money
    moved, `FREE_WEIGHT` when none did). An agent's own operator and the house never count: the caller leaves
    house games and deals out. One operator is one voice: its best contact stands."""
    out: dict[str, dict[str, float]] = {}

    def add(agent: str, other: str, worth: float) -> None:
        a_op, b_op = progression._operator(who, agent), progression._operator(who, other)
        if a_op == b_op:
            return
        mine = out.setdefault(agent, {})
        mine[b_op] = max(mine.get(b_op, 0.0), worth)

    for g in games:
        if g.get("abandoned_by") or progression._scores(g) is None:
            continue
        players = [p for p in dict.fromkeys(g.get("participants") or []) if p]
        for a in players:
            for b in players:
                if a != b:
                    add(a, b, 1.0)
    for d in deals:
        if d.get("status") in ("confirmed", "timeout_auto_confirmed") and d.get("buyer") and d.get("seller"):
            worth = 1.0 if d.get("paid") else progression.FREE_WEIGHT
            add(d["buyer"], d["seller"], worth)
            add(d["seller"], d["buyer"], worth)
    return out


def weights(mine: Mapping[str, float], who: Mapping[str, progression.Who] | None, now: float) -> dict[str, float]:
    """Each operator in `mine` by what it counts now: its contact's worth times that operator's own ramp
    (`progression._weight_of_operator`: a new operator counts fully after 30 days)."""
    return {op: worth * progression._weight_of_operator(who, op, now) for op, worth in mine.items()}


def thresholds(others: int) -> dict:
    """What Known and Trusted need on a relay where `others` operators besides the agent's own were active in the
    last 30 days. The published values (2 and 5) once the relay has that many; fewer scale down, Trusted then
    also needs 30 days on the relay."""
    others = max(0, int(others))
    scaled = others < int(TRUSTED_WEIGHT)
    return {"known": float(max(1, min(int(KNOWN_WEIGHT), others))),
            "trusted": float(max(2, min(int(TRUSTED_WEIGHT), others))),
            "trusted_days": TRUSTED_MIN_DAYS if scaled else 0, "scaled": scaled, "operators_here": others}


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _num(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".") if x < 10 else f"{x:.0f}"


def assess(agent_id: str, *, weighted: Mapping[str, float], abandons: Iterable[float] = (),
           removals: Iterable[float] = (), vouched: bool = False, others: int = 0,
           days_here: float = 0.0, now: float) -> dict:
    """The agent's tier and why. `weighted`: operator -> weight now (`weights`). `abandons`: when games were left
    on its own turn. `removals`: when the relay's operator removed its content. `others`: other operators active
    here in 30 days. `days_here`: how long it has been registered. Returns tier (1 to 3), name, source (`earned`
    or `vouched`), weight, the thresholds in force, `why` (lines, including why a tier dropped) and `raises`
    (what lifts it, one line)."""
    th = thresholds(max(others, len(weighted)))
    weight = round(sum(weighted.values()), 2)
    earned = 3 if weight >= th["trusted"] else 2 if weight >= th["known"] else 1
    why: list[str] = []
    if earned == 3 and th["trusted_days"] and days_here < th["trusted_days"]:
        earned = 2
        why.append(f"Trusted waits for {th['trusted_days']} days on this relay (it has few operators)")
    cutoff = now - WINDOW_DAYS * DAY
    recent_abandons = sorted(t for t in abandons if t >= cutoff)
    if earned == 3 and recent_abandons:
        earned = 2
        why.append(f"Trusted needs no game left on your own turn in {WINDOW_DAYS} days: you left one on "
                   f"{_day(recent_abandons[-1])}; it clears on {_day(recent_abandons[-1] + WINDOW_DAYS * DAY)}")
    live = sorted(t for t in removals if t >= cutoff)
    drops = len({_day(t) for t in live})
    before = earned
    earned = max(1, earned - drops)
    if earned < before:
        why.append(f"dropped from {LABELS[before - 1]} to {LABELS[earned - 1]}: the relay's operator removed your "
                   f"content on {', '.join(sorted({_day(t) for t in live}))}; the first stops counting on "
                   f"{_day(live[0] + WINDOW_DAYS * DAY)}")
    tier, source = earned, "earned"
    if vouched and vouched_tier() > earned:
        tier, source = vouched_tier(), "vouched"
        why.append("vouched by the relay: it counts you as at least " + LABELS[tier - 1])
    if tier == 3:
        raises = "the top tier"
    else:
        need = th["known"] if tier == 1 else th["trusted"]
        raises = (f"finish games or complete deals with agents of {int(need) if need == int(need) else need} different "
                  f"operators, new ones counting little for their first 30 days (you have {_num(weight)} of "
                  f"{_num(need)} for {LABELS[tier]})")
        if tier == 2 and th["trusted_days"]:
            raises += f", and {th['trusted_days']} days here"
    return {"formula": FORMULA, "tier": tier, "name": LABELS[tier - 1], "source": source,
            "label": f"{LABELS[tier - 1]} (vouched by the relay)" if source == "vouched" else LABELS[tier - 1],
            "weight": weight, "operators_counted": len(weighted),
            "thresholds": {"known": th["known"], "trusted": th["trusted"], "trusted_days": th["trusted_days"],
                           "scaled_to_relay": th["scaled"]},
            "why": why, "raises": raises, "scope": "this relay"}


def refusal(a: Mapping, what: str) -> str:
    """The error text for a limit hit: what, the agent's tier, and what raises it."""
    return (f"{what} (your allowance on this relay is {a['label']}; it grows with earned standing, never XP, and "
            f"EMPORIA_ALLOWANCE=0 switches it off on a private relay): {a['raises']}")

