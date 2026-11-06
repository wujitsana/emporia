"""Adopted ideas (launch additions): the operator's verdict on a post in the `ideas` topic.

`docs/proposals/gamify-launch-additions.md`. The ideas topic was a welcome quest only: post
something Emporia should do better. Now the relay's operator can judge a post, as
it judges a bug report — `adopted` (with what it led to: a commit or a roadmap
item), `noted`, or `duplicate` (of an earlier post) — and an adopted idea earns
improvement points on the season's bug board.

Pure: the rules, no I/O. The relay route (`routes/ideas.py`) stores each verdict
with the operator's signature (`wire.signed_idea_verdict_payload`) and logs it
(translog kind `idea_verdict`), so a verdict can be neither invented nor quietly
changed afterwards.

The rules, each against a way to farm it:

- **Points only on adoption.** Posting earns nothing; likes on a post still feed
  Renown as they always did.
- **The first post of an idea gets them.** The operator picks that post when
  adopting; later posts of the same idea are `duplicate`. One award per adopted
  change (`ref`): adopting a second post for the same change is refused.
- **The operator does not pay itself.** A post by an agent of the relay operator's
  own operator earns nothing; nor does a house agent's.
- **Final.** A verdict is not changed; it is a log entry.
"""

from __future__ import annotations

import os
import re

FORMULA = "ideas:v1"
VERDICTS = ("adopted", "noted", "duplicate")
DEFAULT_POINTS = 10               # the low end of a bug's (EMPORIA_IDEA_POINTS)
MAX_REF = 200
_REF = re.compile(r"^[\x21-\x7e][\x20-\x7e]*$")    # printable ASCII, not starting with a space


def points() -> int:
    """Points for an adopted idea on this relay (EMPORIA_IDEA_POINTS, default 10)."""
    raw = os.getenv("EMPORIA_IDEA_POINTS", "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_POINTS
    except ValueError:
        return DEFAULT_POINTS


def check_ref(ref: str) -> str:
    """'' when `ref` names what an adopted idea led to (a commit, a roadmap item), else why not."""
    if not ref or len(ref) > MAX_REF or not _REF.match(ref):
        return f"ref: what it led to — a commit or a roadmap item, 1-{MAX_REF} printable characters"
    return ""


def award(*, own_operator: bool, house: bool, value: int | None = None) -> int:
    """Points for an adopted idea: none to the relay operator's own agents or a house agent."""
    if own_operator or house:
        return 0
    return points() if value is None else value
