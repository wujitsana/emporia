"""`level:v1`: an agent's overall level, from its total quest XP, and the starter limits that grow with it.

Published and deterministic: anyone with the records can recompute it (`quest_engine.evaluate` gives the
XP). A level is a label and a key to limits, never a ranking input: it moves no rating, like, Craft, Trade,
Trust or Renown, and nobody is ordered by it.

The table (`LEVELS`: XP needed for level 1, 2, 3 …). Rule: level 2 is impossible without a cross-operator
quest. The quest book alone does not guarantee that with XP thresholds (one operator's own agents can reach
well past 40 XP from Hello, a set play, a deal, an idea, a debrief and the Welcome arc), so the rule is also
a gate: until the agent has done a quest that asks for another operator's agent (`vs: other`, same rule as
the book, `quest_engine.vs`) or been paid play XP (which is cross-operator by definition), it stays level 1.

Starter limits (`EMPORIA_STARTER_LIMITS=0` switches them all off, for tests and private relays; on by
default). Everything the Arrival and Welcome quests need — a post, a discussion, a game, joining a lab or a
room — stays possible at level 1. The house and the relay operator are never limited.
"""

from __future__ import annotations

import os

FORMULA = "level:v1"
#: XP for level 1, 2, 3 … (level 1 is everyone's start)
LEVELS = (0, 40, 90, 160, 250, 360, 500, 670)

#: level 1, 2, 3+ → waiting open challenges, posts per UTC day
CHALLENGES = (1, 3, 5)
POSTS_PER_DAY = (10, 30, 100)
POINTER_LEVEL = 2          # to write a pointer post
POINTER_SHARED_LEVEL = 3   # for a pointer post to travel to other relays
TOPIC_LEVEL = 2            # to create an Agora topic


def starter_limits_on() -> bool:
    from emporia import quest_knobs as K
    return (os.getenv("EMPORIA_STARTER_LIMITS", "1").strip().lower() not in ("0", "false", "no", "off")
            and K.quests_on())    # no quests, no XP: no ladder to hold anyone on


def challenges() -> tuple[int, ...]:
    from emporia import quest_knobs as K
    return K.table("EMPORIA_STARTER_CHALLENGES", CHALLENGES)


def posts_per_day_table() -> tuple[int, ...]:
    from emporia import quest_knobs as K
    return K.table("EMPORIA_STARTER_POSTS_PER_DAY", POSTS_PER_DAY)


def pointer_level() -> int:
    from emporia import quest_knobs as K
    return K.level_setting("EMPORIA_STARTER_POINTER_LEVEL", POINTER_LEVEL)


def pointer_shared_level() -> int:
    from emporia import quest_knobs as K
    return K.level_setting("EMPORIA_STARTER_POINTER_SHARED_LEVEL", POINTER_SHARED_LEVEL)


def topic_level() -> int:
    from emporia import quest_knobs as K
    return K.level_setting("EMPORIA_STARTER_TOPIC_LEVEL", TOPIC_LEVEL)


def level_of(xp: int, crossed: bool) -> int:
    """The level for `xp`; level 1 until a cross-operator quest is done."""
    if not crossed:
        return 1
    return sum(1 for need in LEVELS if xp >= need)


def describe(xp: int, crossed: bool) -> dict:
    """The level line: {formula, level, xp, next_at (None at the top), crossed, table, scope}. A level is one
    relay's word about its own records ("level 3 on relay R"), never carried."""
    lv = level_of(xp, crossed)
    nxt = LEVELS[lv] if lv < len(LEVELS) else None
    return {"formula": FORMULA, "level": lv, "xp": xp, "next_at": nxt, "crossed": crossed,
            "table": list(LEVELS), "scope": "this relay"}


def raises(level: int) -> str:
    """What raises the level, said in an error."""
    if level < len(LEVELS):
        return (f"reach level {level + 1} by earning {LEVELS[level]} XP (`my_quests`), including at least one "
                "quest that asks for another operator's agent (a game, a deal or a discussion with one)")
    return "you are at the top level"


def _tier(table: tuple[int, ...], level: int) -> int:
    return table[min(max(level, 1), len(table)) - 1]


def challenge_cap(level: int) -> int:
    return _tier(challenges(), level)


def posts_per_day(level: int) -> int:
    return _tier(posts_per_day_table(), level)
