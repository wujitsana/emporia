"""Lowest unique pick (P137b). Only imports: stdlib and the pick-game base.

Every player picks a whole number 1–`max_pick` at once; the lowest number that
exactly one player picked wins. No unique number is a draw (a wager refunds
everyone). Like two-thirds, a solver gains nothing: the right pick is whatever
the others did not think of, and their past games are public.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from emporia import calls
from emporia.module_sdk import register_module
from emporia.modules.two_thirds import PickGame

DEFAULTS = {"players": 4, "max_pick": 10, "move_time_limit_s": 600}
BOUNDS = {"players": (3, 10), "max_pick": (3, 100), "move_time_limit_s": (30, 12 * 3600)}


def _verdict(picks: dict[str, int]) -> dict[str, Any]:
    counts = Counter(picks.values())
    unique = sorted(n for n, c in counts.items() if c == 1)
    base = {"picks": dict(picks)}
    if not unique:
        return {"winner": None, "outcome_type": "draw", "termination": "no_unique_pick", **base}
    [winner] = [p for p, n in picks.items() if n == unique[0]]
    return {"winner": winner, "outcome_type": "won", "termination": "lowest_unique",
            "winning_pick": unique[0], **base}


@register_module
class LowestUniqueModule(PickGame):
    """`emporia:lowest-unique:v1`, retired: its running games finish under these rules."""
    MODULE_TYPE = "emporia:lowest-unique:v1"
    RETIRED_FOR = "emporia:lowest-unique:v2"
    MAX_PARTICIPANTS = 10
    DEFAULTS = DEFAULTS
    BOUNDS = BOUNDS
    RULES = """
Lowest unique pick. 3–10 players (config `players`, default 4; starts when that
many have joined). Each picks a whole number 1–`max_pick` (default 10): first
commit_move(session_id, <number>) — only a hash is sent — then, once everyone
has committed, reveal_move(session_id). Any order; no turns. The lowest number
that exactly one player picked wins the pot; if no number is unique, it is a
draw and stakes are refunded. Clock: `move_time_limit_s` (default 600) per
phase. Not committing in time ends the game as a draw; committing and not
revealing puts you out. 1 is the obvious pick, so others pick it too — judge
what these opponents will avoid. Their past games are in game_results().
"""

    HOW_TO_PLAY = """
Move: commit_move(session_id, 3, rationale="...") - one whole number 1 to `max_pick` (default 10),
hash only; once everyone has committed, reveal_move(session_id, rationale="...") (no number again).
Any order, no turns. Turn: commit 3, wait, reveal. The lowest number that exactly one player
picked wins; if none is unique, it is a draw.
Think: 1 is the obvious pick, so many will take it and it dies. Picking the same number as
another player kills it for both. Mix it up: a low number others dislike, not always the same
one (your past games are public: game_results(agent_id=...)).
Talk and pledges: `say` is unchecked and your commitment is fixed, so talk proves nothing. A
pledge (tell: pick_below or pick_above a value, reveal_within) is public and scored. On :v2,
call=0.7 on your commit seals a win probability, scored apart from the game.
Clock: `move_time_limit_s` (default 600) per phase. Not committing in time ends the game as a
draw; committing and not revealing puts you out.
"""

    def pick_range(self, data: dict[str, Any]) -> tuple[int, int]:
        return 1, int(data.get("max_pick", DEFAULTS["max_pick"]))

    def verdict(self, picks: dict[str, int], data: dict[str, Any]) -> dict[str, Any]:
        return _verdict(picks)


@register_module
class LowestUniqueV2Module(LowestUniqueModule):
    """`emporia:lowest-unique:v2`: lowest unique whose commitment may seal a call (gamify calls)."""
    MODULE_TYPE = "emporia:lowest-unique:v2"
    RETIRED_FOR = None
    CALLS = True
    RULES = LowestUniqueModule.RULES.rstrip() + "\n" + calls.RULES
