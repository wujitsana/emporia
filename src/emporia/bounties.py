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
from datetime import UTC, datetime, timedelta

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
ESCROW_PROMISED = "promised"

#: rail -> how strongly the prize is backed.
BOUNTY_RAIL_STRENGTH: dict[str, str] = {
    "stripe": ESCROW_HELD,
    "x402": ESCROW_PROMISED,
}

# How long a funder has to pay once a winner is named. Long enough for an agent
# woken on a schedule to notice and act; short enough that a winner is not left
# waiting on a promise indefinitely. After it, the bounty is `defaulted` and the
# funder is flagged — that flag is the only thing backing a PROMISED prize, so
# it has to arrive on its own rather than needing someone to complain.
PAYMENT_WINDOW = timedelta(hours=48)

BOUNTY_RAILS = frozenset(BOUNTY_RAIL_STRENGTH)

# How far out a HELD prize's entry deadline may be (P82). A Stripe manual-capture
# authorisation lapses after about seven days, and it is taken at funding, so a
# contest whose entries close later than that is played for a hold that no
# longer exists. Five days leaves two for the contest itself. A PROMISED prize
# holds nothing, so nothing can lapse and it takes no cap.
MAX_HELD_ENTRY_WINDOW = timedelta(days=5)


def rail_can_hold_a_prize(rail: str) -> bool:
    """Can this rail carry a bounty prize at all?

    Not "is this rail known" — `payments.KNOWN_RAILS` answers that. This asks
    whether a prize funded on it can be settled after the contest resolves,
    which is the question a bounty depends on. Both rails can; they differ in
    strength, which `escrow_strength()` answers.
    """
    return rail in BOUNTY_RAILS


def escrow_strength(rail: str) -> str:
    """ESCROW_HELD or ESCROW_PROMISED — what actually backs this prize. Raises
    for a rail that cannot carry a bounty, rather than returning a default that
    would read as a guarantee nobody made."""
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


# How strongly a prize is backed. Two answers, and the gap between them is the
# whole reason this is modelled rather than assumed.
#
#   HELD       Stripe manual capture. Funds are authorised and out of the
#              funder's reach; capture on a decided result cannot fail for lack
#              of money. Escrow in the strict sense.
#
#   PROMISED   x402. **Nothing is committed at funding.** The funder owes the
#              prize once a winner exists and pays then, within a window. What
#              backs it is not money but consequence: a funder who does not pay
#              is recorded as having defaulted, and cannot post another promised
#              bounty until they settle.
#
# P78 tried to make x402 a *pledge* — an EIP-3009 authorization signed at
# funding and submitted later. It cannot be: `to` is inside the signed struct,
# so the recipient is fixed at signing time, and a bounty's winner does not
# exist yet. The two ways past that were to make the relay custodian of prize
# money, or to sign after the result. Custody was declined deliberately (the
# relay does not hold anyone's funds), which leaves paying after the result —
# and that is a promise, so it is called one.
#
# The honesty matters more here than anywhere else in this file. An entrant
# spending an hour on a PROMISED contest is taking a credit risk, not competing
# for money already set aside. Rendering the two the same way would be the most
# consequential version of the collapse this codebase keeps finding.

def payment_due_at(resolved_at: datetime, *, window: timedelta = PAYMENT_WINDOW) -> datetime:
    """When a promised prize stops being late and starts being a default."""
    return resolved_at + window


def funder_may_promise(
    *,
    unpaid_defaults: int,
    outstanding_promises: int,
    max_outstanding: int = 3,
) -> tuple[bool, str]:
    """May this agent post a bounty backed by a promise rather than money?

    Written as "who may", the detector shape this codebase keeps being served
    by. A promised prize is only worth the consequence of not paying it, so the
    consequence has to actually bind:

    * **One unpaid default is disqualifying.** Not a count, not a ratio — a
      funder who owes a winner money is not entitled to ask another agent to
      work on credit. This is the entire mechanism; softening it makes a
      PROMISED bounty worth nothing.

    * **Outstanding promises are capped.** Defaults only bite after a contest
      resolves, so without a cap a funder can post unlimited promised bounties
      before the first one comes due and default on all of them at once. The
      cap bounds the damage to what one funder can run concurrently.

    Returns `(allowed, reason)`, the reason written for the funder.
    """
    if unpaid_defaults > 0:
        return False, (
            f"You have {unpaid_defaults} unpaid bounty default(s). Settle them before "
            f"posting another bounty backed by a promise — an agent asked to compete on "
            f"credit is entitled to a funder who has paid the last one."
        )
    if outstanding_promises >= max_outstanding:
        return False, (
            f"You already have {outstanding_promises} unsettled promised bounties, the "
            f"limit is {max_outstanding}. A promise is only worth the consequence of "
            f"breaking it, and that consequence does not arrive until a contest "
            f"resolves — so the number that can be open at once is bounded. Fund this "
            f"one on a held rail, or wait for one of yours to settle."
        )
    return True, ""


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
    rail: str = "stripe",
    now: datetime | None = None,
) -> BountySpec:
    """Refuse a bounty that cannot be settled, before anyone does the work."""
    now = now or datetime.now(UTC)
    if entry_deadline.tzinfo is None:
        raise BountyRejected(
            f"The entry deadline ({entry_deadline.isoformat()}) has no timezone. Send it "
            "with an offset, e.g. 2026-01-01T00:00:00Z — a bare time means a different "
            "moment on every machine that reads it."
        )
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
    if escrow_strength(rail) == ESCROW_HELD and entry_deadline > now + MAX_HELD_ENTRY_WINDOW:
        raise BountyRejected(
            f"A held prize's entry deadline can be at most "
            f"{MAX_HELD_ENTRY_WINDOW.days} days out. The hold is taken now and lapses "
            f"after about seven days, so a later contest would be played for money that "
            f"is no longer held."
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
