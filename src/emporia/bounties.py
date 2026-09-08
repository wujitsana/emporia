"""Bounty rules — one funder, many entrants, a module that decides (P72).

Every settlement this relay shipped before now was a **wager**: N participants
each escrow a stake, one wins, the pot moves. A bounty is the asymmetric shape.
One agent funds a prize, entrants risk nothing but their time, and the funder is
guaranteed *not* to be the winner. P72-1 proved `payments.settle()` already
expresses that — it captures whatever holds it is handed and pays whatever
account it is handed, and the two are independent arguments.

So this module is not payment code. It is the set of rules that decide whether a
bounty is well-formed and who may enter one, kept in a single place for the
reason this codebase has now learned three times (P55, P59, P70): a rule with two
copies is a rule with two answers.

**Everything here is pure.** No I/O, no database, no `httpx`. Callers pass in the
facts; this decides. That is what makes the Sybil rule below testable without a
relay, and it is why the caller — not this module — reads `nous_user_id` from
whatever store it has.

The rules, and why each exists:

* **Only `nous_verified` may enter.** Raises entry from "generate a keypair"
  (free, unlimited) to "hold a Nous account". The gate already exists.

* **One entry per `nous_user_id`, never per `agent_id`.** `nous_user_id` is
  one-to-many *by design* — the index on it is deliberately non-unique, and
  `relay_server`'s own note says why: "one Nous user may operate multiple agent
  personas." Counting entries per persona would let one operator buy a contest
  in lottery tickets for the price of a Nous account. Counting per account is
  the whole difference between a gate and a decoration.

* **The funder can never win their own bounty.** Otherwise it is a wash trade
  that farms standing for the cost of the operator fee.

* **The prize is fixed at funding.** A funder who can lower it after entries land
  has taken the work for less than advertised.

* **Only a module that names a winner may judge.** `InteractionModule.
  DECLARES_WINNER`, asked of the module itself rather than kept as a list here.

* **A prize below the payment floor is refused at creation**, not at payout when
  the work is already done. See `MIN_PRIZE_CENTS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

BOUNTY_LISTING_TYPE = "bounty"

# P72-2: `payments.settle()`'s docstring has always claimed "Requires
# total_stake_cents >= STRIPE_MIN_CHARGE_CENTS (50c) — enforced upstream", and
# no such constant exists anywhere in this codebase. Nothing enforces a floor;
# the only amount guard in payments.py is `amount_cents <= 0`. Stripe itself
# rejects a sub-minimum PaymentIntent, so the practical failure is a raw Stripe
# error at escrow time rather than a clear refusal at creation — and for a
# bounty that is the difference between "we won't take this" and an entrant
# discovering the prize was never payable.
#
# Named here rather than in payments.py deliberately: this is the value the
# bounty flow refuses on, and claiming to fix payments.py's phantom guard for
# every caller is a wider change than this pass measured.
MIN_PRIZE_CENTS = 50


@dataclass(frozen=True)
class BountySpec:
    """A well-formed bounty. Construct via `validate_spec`, never directly."""
    prize_cents: int
    module_type: str
    entry_deadline: datetime
    funder_id: str


class BountyRejected(ValueError):
    """A bounty that must not be created, with a reason an agent can act on."""


def judging_modules(registry: dict[str, type]) -> list[str]:
    """Module types that can decide a bounty without a judge.

    Derived from each module's own `DECLARES_WINNER`, so adding a module that
    can judge needs no edit here. `research` and `code_review` reach terminal
    states but name no winner, and drop out on their own terms.
    """
    return sorted(
        m for m, cls in registry.items() if getattr(cls, "DECLARES_WINNER", False)
    )


def validate_spec(
    *,
    prize_cents: int,
    module_type: str,
    entry_deadline: datetime,
    funder_id: str,
    registry: dict[str, type],
    now: datetime | None = None,
) -> BountySpec:
    """Refuse a bounty that cannot be settled, before anyone does the work."""
    now = now or datetime.now(UTC)
    if prize_cents < MIN_PRIZE_CENTS:
        raise BountyRejected(
            f"A bounty prize must be at least {MIN_PRIZE_CENTS} cents; got {prize_cents}. "
            "Below the payment floor the prize cannot be paid out at all, so this is "
            "refused now rather than after an entrant has done the work."
        )
    judges = judging_modules(registry)
    if module_type not in judges:
        raise BountyRejected(
            f"{module_type!r} cannot decide a bounty. This lane settles on a module's own "
            f"result, with no judge and no vote, so the module must name a winner. "
            f"Available: {', '.join(judges) or '(none)'}."
        )
    if entry_deadline <= now:
        raise BountyRejected(
            f"The entry deadline ({entry_deadline.isoformat()}) is not in the future. "
            "A bounty nobody can enter holds the funder's money for nothing."
        )
    if not funder_id:
        raise BountyRejected("A bounty needs a funder.")
    return BountySpec(prize_cents, module_type, entry_deadline, funder_id)


def may_enter(
    *,
    agent_id: str,
    agent_nous_user_id: str | None,
    is_nous_verified: bool,
    spec: BountySpec,
    funder_nous_user_id: str | None,
    existing_entrant_nous_ids: set[str],
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Who may enter this bounty — the detector, written as a question about the
    caller rather than a comparison of values.

    Returns `(allowed, reason)`. `reason` is empty when allowed, and is written
    for an agent to read, since an agent is what will receive it.

    The Sybil rule is the load-bearing one: entry is counted against
    `agent_nous_user_id`, so five personas under one Nous account get one entry
    between them, and a funder cannot enter their own bounty under a second
    persona either.
    """
    now = now or datetime.now(UTC)
    if now >= spec.entry_deadline:
        return False, "Entry for this bounty has closed."
    if not is_nous_verified:
        return False, (
            "Entering a bounty requires nous_verified. A keypair is free and unlimited; "
            "a Nous account is the cost of entry."
        )
    if not agent_nous_user_id:
        return False, (
            "This agent is marked nous_verified but carries no Nous account id, so its "
            "entry cannot be counted against an account. Re-register with a Nous JWT."
        )
    if agent_id == spec.funder_id:
        return False, "The agent funding a bounty cannot compete for its own prize."
    if funder_nous_user_id and agent_nous_user_id == funder_nous_user_id:
        return False, (
            "This agent shares a Nous account with the bounty's funder. One account cannot "
            "both fund a prize and compete for it, whatever personas it operates."
        )
    if agent_nous_user_id in existing_entrant_nous_ids:
        return False, (
            "This Nous account has already entered. Entry is one per account, not one per "
            "agent, because one Nous account may operate any number of personas."
        )
    return True, ""
