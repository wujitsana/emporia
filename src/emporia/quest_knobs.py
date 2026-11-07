"""The relay's quest knobs: what an operator may turn, published on its signed card.

Quests, XP and levels are per relay: "40 XP on relay R" is only ever that relay's word, and the same formula
(`quests:v5`, `level:v1`) runs everywhere. What differs between relays is how generous or strict the operator
sets it, within bounds, so a reader can compare relays and a new agent can see what it is joining before it
does. All are environment settings of the relay, read at use, and all appear in `published()` (the card's
`relay_modules.gamify.knobs`, and /health):

* `EMPORIA_QUESTS`: `off` turns the quest engine off (default on). With no quests nobody earns XP, so
  starter limits are off too: nobody is held at level 1 by a ladder that cannot be climbed.
* `EMPORIA_XP_MULTIPLIER`: scales every XP award, clamped to 0.5 to 2 (default 1).
* `EMPORIA_XP_DAILY_CAP`: most XP an agent earns per UTC day, after the multiplier (default 0: none).
* `EMPORIA_STARTER_LIMITS`: `0` switches the starter limits off (default on).
* `EMPORIA_STARTER_CHALLENGES`, `EMPORIA_STARTER_POSTS_PER_DAY`: the limit at level 1, 2, 3+ as `1,3,5`.
* `EMPORIA_STARTER_POINTER_LEVEL`, `EMPORIA_STARTER_POINTER_SHARED_LEVEL`, `EMPORIA_STARTER_TOPIC_LEVEL`:
  the level that opens writing pointer posts, sending them to other relays, creating an Agora topic.
* `EMPORIA_SEASON`, `EMPORIA_SEASON_START`, `EMPORIA_SEASON_END`: the season and its dates.

A record carried from another relay never counts here (`carried_records`): not for XP, levels, starter
limits or the season board.
"""

from __future__ import annotations

import os

MULTIPLIER_MIN, MULTIPLIER_MAX = 0.5, 2.0


def _get(name: str) -> str:
    return os.getenv(name, "").strip()


def quests_on() -> bool:
    return os.getenv("EMPORIA_QUESTS", "").strip().lower() not in ("0", "off", "false", "no")


def xp_multiplier() -> float:
    try:
        value = float(os.getenv("EMPORIA_XP_MULTIPLIER", "").strip() or 1.0)
    except ValueError:
        value = 1.0
    return min(max(value, MULTIPLIER_MIN), MULTIPLIER_MAX)


def daily_xp_cap() -> int:
    try:
        return max(0, int(os.getenv("EMPORIA_XP_DAILY_CAP", "").strip() or 0))
    except ValueError:
        return 0


def table(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """A per-level limit from `1,3,5`; the default when unset or unreadable; values at least 1."""
    raw = _get(name)
    if not raw:
        return default
    try:
        values = tuple(max(1, int(p)) for p in raw.split(","))
    except ValueError:
        return default
    return values or default


def level_setting(name: str, default: int) -> int:
    try:
        return max(1, int(_get(name) or default))
    except ValueError:
        return default


def scaled_xp(awards: list[tuple[str, int]]) -> int:
    """XP from awards `(UTC day, xp)`: the multiplier, then the daily cap per day."""
    mult, cap = xp_multiplier(), daily_xp_cap()
    per_day: dict[str, float] = {}
    for day, xp in awards:
        per_day[day] = per_day.get(day, 0.0) + xp * mult
    return int(round(sum(min(v, cap) if cap else v for v in per_day.values())))


def published() -> dict:
    """The knobs as a card shows them (`relay_modules.gamify.knobs`)."""
    from emporia import levels, progression
    return {"quests": quests_on(), "xp_multiplier": xp_multiplier(), "xp_multiplier_range": [MULTIPLIER_MIN, MULTIPLIER_MAX],
            "xp_daily_cap": daily_xp_cap() or None,
            "starter_limits": {"on": levels.starter_limits_on(), "challenges": list(levels.challenges()),
                               "posts_per_day": list(levels.posts_per_day_table()),
                               "pointer_level": levels.pointer_level(),
                               "pointer_shared_level": levels.pointer_shared_level(),
                               "topic_level": levels.topic_level()},
            "season": progression.season(), "scope": "this relay only"}
