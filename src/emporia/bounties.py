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

* **Entry closes when the contest is full.** The module decides how many can
  play, and both judging modules today seat exactly two. P72-4 shipped without
  this: fifty agents could enter a chess bounty with two seats, and forty-eight
  of them would have done nothing wrong and received nothing. A contest that
  accepts entries it cannot seat is taking people's time under false pretences,
  which is the same defect as advertising a prize with no hold behind it —
  P72-5 fixed that one, this fixes its twin.

  Capacity counts *entrants*, not participants-including-funder: the funder does
  not compete, so a two-seat module means two entrants playing for the funder's
  prize.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

BOUNTY_LISTING_TYPE = "bounty"

# Which rails can carry a bounty, and on what terms.
#
# **P75 got the reason wrong.** It said x402 "signs and settles in one step with
# nothing parked in between". That describes `_verify_x402_payment()`, which
# calls the facilitator's /verify and /settle back to back — not the protocol,
# which defines them as separate endpoints. An EIP-3009 authorization carries
# `validAfter`/`validBefore`: it is a signed, time-bounded instruction anyone
# can submit *later*. Deferring the settle is exactly what a bounty needs.
#
# So x402 can carry a prize. What it cannot do is *reserve* one, and that
# difference is the whole design:
#
#   HELD      Stripe manual capture authorises funds and takes them out of the
#             payer's reach. Capture on a decided result cannot fail for lack of
#             money. This is escrow in the strict sense.
#
#   PLEDGED   An EIP-3009 authorization reserves nothing. The funder can move
#             the USDC before we settle, and the settle then fails on
#             insufficient balance. The prize is a signed promise with an
#             expiry, not a hold.
#
# A pledge is worth more than plain trust and worth less than a hold: it is
# non-repudiable (the funder signed it), bounded (`validBefore`), and its
# failure is provable on chain rather than one party's word. So it is offered,
# labelled, and a funder who defects is recorded — not quietly treated as if the
# money had been there.
#
# The distinction is on the wire and in the UI because an entrant deciding
# whether to spend an hour on a contest is entitled to know which of the two
# backs the prize.

ESCROW_HELD = "held"
ESCROW_PLEDGED = "pledged"

#: rail -> how strongly the prize is backed.
BOUNTY_RAIL_STRENGTH: dict[str, str] = {
    "stripe": ESCROW_HELD,
    "x402": ESCROW_PLEDGED,
}

BOUNTY_RAILS = frozenset(BOUNTY_RAIL_STRENGTH)


def rail_can_hold_a_prize(rail: str) -> bool:
    """Can this rail carry a bounty prize at all?

    Not "is this rail known" — `payments.KNOWN_RAILS` answers that. This asks
    whether a prize funded on it can be settled after the contest resolves,
    which is the question a bounty depends on. Both rails can; they differ in
    strength, which `escrow_strength()` answers.
    """
    return rail in BOUNTY_RAILS


def escrow_strength(rail: str) -> str:
    """ESCROW_HELD or ESCROW_PLEDGED — how much the prize is actually worth as a
    promise. Raises for a rail that cannot carry a bounty, rather than returning
    a default that would read as a guarantee nobody made."""
    try:
        return BOUNTY_RAIL_STRENGTH[rail]
    except KeyError:
        raise ValueError(
            f"{rail!r} cannot carry a bounty prize. Known bounty rails: "
            f"{', '.join(sorted(BOUNTY_RAILS))}."
        ) from None

# P74: one definition, imported rather than restated. P72-2 defined this as a
# local 50 because `payments.settle()` cited a STRIPE_MIN_CHARGE_CENTS that did
# not exist, and declined to invent it for every caller in a bounty pass. It
# exists now and is enforced where PaymentIntents are created, so a second copy
# here would be exactly the divergence this file's own rules keep catching.
from emporia.payments import STRIPE_MIN_CHARGE_CENTS as MIN_PRIZE_CENTS


@dataclass(frozen=True)
class BountySpec:
    """A well-formed bounty. Construct via `validate_spec`, never directly."""
    prize_cents: int
    module_type: str
    entry_deadline: datetime
    funder_id: str
    # Seats, taken from the module rather than chosen by the funder — a bounty
    # cannot be posted for more players than its judge can referee.
    min_entrants: int = 2
    max_entrants: int = 2


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


def seats_for(module_type: str, registry: dict[str, type]) -> tuple[int, int]:
    """(min, max) entrants for a module — the one place that answers it.

    P72-8: `validate_spec` derived these from the registry while the route's
    `_spec_from_row` rebuilt a spec from the stored row and silently took the
    dataclass defaults. Two copies of one rule, agreeing only because both
    judging modules happen to seat exactly two: the first module with a
    different capacity would have been validated at its real size and then
    enforced at 2, closing entry halfway through its own contest.

    Same shape as the defect P70 fixed in `_payment_rails` and P72-3 fixed in
    `win_count`. A rule with two implementations has two answers.
    """
    cls = registry[module_type]
    return cls.MIN_PARTICIPANTS, cls.MAX_PARTICIPANTS


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
    lo, hi = seats_for(module_type, registry)
    return BountySpec(prize_cents, module_type, entry_deadline, funder_id,
                      min_entrants=lo, max_entrants=hi)


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
    if len(existing_entrant_nous_ids) >= spec.max_entrants:
        return False, (
            f"This contest is full ({spec.max_entrants} of {spec.max_entrants} seats taken). "
            f"Accepting an entry that cannot be seated would take your time for nothing."
        )
    return True, ""


def is_ready_to_run(spec: BountySpec, entrant_count: int, *, now: datetime | None = None) -> bool:
    """Should this contest start?

    Full, or past its deadline with enough entrants to satisfy the module. A
    bounty that reaches its deadline short of `min_entrants` cannot run at all
    and its hold is released instead — the funder is not charged for a contest
    that never happened.
    """
    now = now or datetime.now(UTC)
    if entrant_count >= spec.max_entrants:
        return True
    return now >= spec.entry_deadline and entrant_count >= spec.min_entrants
