"""How long a client waits before it retries: exponential, with full jitter, capped.

The rule, and why. Agents behind one address share the relay's per-address budgets, and a gateway restart
starts every agent's bridge in the same second. Plain doubling keeps them in step (all retry together, all
refused together); full jitter (a random wait between none and the current ceiling) spreads them out. A
floor stops a tiny draw from becoming a tight loop. An agent the relay does not know (404 at sign-in) cannot
get in by retrying, so it waits much longer and the wait is never short; a 429 waits at least as long as the
relay asked (`Retry-After`). Pure: no I/O and no sleeping; the caller sleeps.
"""

from __future__ import annotations

import random
from collections.abc import Callable

MIN_WAIT = 0.5                 # seconds: the floor of any wait (or the ceiling itself when that is smaller)
UNREGISTERED_FLOOR = 60.0      # seconds: an unregistered agent never retries sooner than this
UNREGISTERED_CAP = 900.0       # seconds: and at most about fifteen minutes (plus the jitter) between tries
RATE_LIMITED_CAP = 120.0       # seconds: the jitter ceiling after a 429, on top of what the relay asked


def full_jitter(attempt: int, base: float = 1.0, cap: float = 60.0,
                rng: Callable[[], float] = random.random) -> float:
    """A wait drawn uniformly between 0 and min(cap, base * 2**attempt), never below `MIN_WAIT` (or the
    ceiling, when that is smaller). `attempt` counts from 0."""
    ceiling = min(cap, base * (2 ** max(0, min(attempt, 30))))
    return max(min(MIN_WAIT, ceiling), rng() * ceiling)


def refusal_wait(status: int, attempt: int, retry_after: float | None = None, *, base: float = 1.0,
                 cap: float = 60.0, rng: Callable[[], float] = random.random) -> float:
    """The wait after a refused sign-in or connect.

    404 (the relay does not know this agent): `UNREGISTERED_FLOOR` plus a jittered growth to `UNREGISTERED_CAP`.
    429: the relay's `Retry-After` (when given) plus a jittered share, so agents it refused together do not
    return together. Anything else: `full_jitter(attempt, base, cap)`."""
    if status == 404:
        return UNREGISTERED_FLOOR + full_jitter(attempt, UNREGISTERED_FLOOR, UNREGISTERED_CAP - UNREGISTERED_FLOOR, rng)
    if status == 429:
        return max(retry_after or 0.0, 0.0) + full_jitter(attempt, 5.0, RATE_LIMITED_CAP, rng)
    return full_jitter(attempt, base, cap, rng)
