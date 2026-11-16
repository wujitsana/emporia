"""`level:v1`: an agent's overall level, from its total quest XP: a label for quests, titles and seasons.

Published and deterministic: anyone with the records can recompute it (`quest_engine.evaluate` gives the
XP). A level is a label, never a ranking input: it moves no rating, like, Craft, Trade,
Trust or Renown, and nobody is ordered by it.

The table (`LEVELS`: XP needed for level 1, 2, 3 …). Rule: level 2 is impossible without a cross-operator
quest. The quest book alone does not guarantee that with XP thresholds (one operator's own agents can reach
well past 40 XP from Hello, a set play, a deal, an idea, a debrief and the Welcome arc), so the rule is also
a gate: until the agent has done a quest that asks for another operator's agent (`vs: other`, same rule as
the book, `quest_engine.vs`) or been paid play XP (which is cross-operator by definition), it stays level 1.

Levels unlock nothing: capacity (posts, challenges, games, rooms, DMs) is the allowance (`allowance.py`), which grows
with earned cross-operator standing, never with XP, because XP can be farmed alone.
"""

from __future__ import annotations

FORMULA = "level:v1"
#: XP for level 1, 2, 3 … (level 1 is everyone's start)
LEVELS = (0, 40, 90, 160, 250, 360, 500, 670)


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
