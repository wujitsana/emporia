"""Bounty rules (P72-2). Pure — no relay, no database, no network.

The Sybil test is the one that matters. Everything else here is a guard on a
guard; `one_nous_account_gets_one_entry` is the rule the money rests on.
"""
from datetime import UTC, datetime, timedelta

import pytest

from emporia.bounties import (
    MIN_PRIZE_CENTS,
    BountyRejected,
    judging_modules,
    may_enter,
    validate_spec,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
SOON = NOW + timedelta(hours=6)


class _Judges:
    DECLARES_WINNER = True
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2


class _CannotJudge:
    DECLARES_WINNER = False
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 4


REGISTRY = {"emporia:chess:v1": _Judges, "emporia:research:v1": _CannotJudge}


def _spec(**kw):
    return validate_spec(
        prize_cents=kw.get("prize_cents", 5000),
        module_type=kw.get("module_type", "emporia:chess:v1"),
        entry_deadline=kw.get("entry_deadline", SOON),
        funder_id=kw.get("funder_id", "funder"),
        registry=REGISTRY, now=NOW,
    )


def _enter(**kw):
    return may_enter(
        agent_id=kw.get("agent_id", "entrant"),
        agent_nous_user_id=kw.get("agent_nous_user_id", "nous_1"),
        is_nous_verified=kw.get("is_nous_verified", True),
        spec=kw.get("spec") or _spec(),
        funder_nous_user_id=kw.get("funder_nous_user_id", "nous_funder"),
        existing_entrant_nous_ids=kw.get("existing_entrant_nous_ids", set()),
        now=kw.get("now", NOW),
    )


class TestSpec:
    def test_a_module_that_names_no_winner_cannot_judge(self):
        """The lane settles on the module's own result. research reaches a
        terminal state but names no winner, so it drops out on its own terms —
        no list in this file to keep in sync."""
        assert judging_modules(REGISTRY) == ["emporia:chess:v1"]
        with pytest.raises(BountyRejected, match="cannot decide a bounty"):
            _spec(module_type="emporia:research:v1")

    def test_a_prize_below_the_payment_floor_is_refused_at_creation(self):
        """Refused now, not after an entrant has done the work.

        P74: the floor is the *rail's*, not the bounty lane's own. Asserted as
        an identity rather than only relative to itself — the original version
        compared `MIN_PRIZE_CENTS - 1` against `MIN_PRIZE_CENTS`, which holds
        for any value and so could not notice this drifting away from what
        Stripe will actually accept. A bounty floor below the rail's is a prize
        that validates here and cannot be paid.
        """
        from emporia.payments import STRIPE_MIN_CHARGE_CENTS

        assert MIN_PRIZE_CENTS == STRIPE_MIN_CHARGE_CENTS, (
            "the bounty floor must BE the rail's floor, not a copy of its value — "
            "a second definition is free to drift below what Stripe accepts"
        )
        with pytest.raises(BountyRejected, match="at least"):
            _spec(prize_cents=MIN_PRIZE_CENTS - 1)
        assert _spec(prize_cents=MIN_PRIZE_CENTS).prize_cents == MIN_PRIZE_CENTS

    def test_a_deadline_in_the_past_holds_money_for_nothing(self):
        with pytest.raises(BountyRejected, match="not in the future"):
            _spec(entry_deadline=NOW - timedelta(seconds=1))


class TestWhoMayEnter:
    def test_one_nous_account_gets_one_entry(self):
        """THE rule. nous_user_id is one-to-many by design — the index on it is
        deliberately non-unique because "one Nous user may operate multiple
        agent personas". Counting entries per agent_id would let one operator
        buy a contest in lottery tickets for the price of one Nous account."""
        ok, _ = _enter(agent_id="persona_a", agent_nous_user_id="nous_7")
        assert ok
        # A second, entirely distinct agent — different agent_id, different
        # keypair, genuinely nous_verified — but the same human behind it.
        ok, why = _enter(
            agent_id="persona_b", agent_nous_user_id="nous_7",
            existing_entrant_nous_ids={"nous_7"},
        )
        assert not ok and "one per account" in why

    def test_the_funder_cannot_win_their_own_prize(self):
        ok, why = _enter(agent_id="funder")
        assert not ok and "own prize" in why

    def test_nor_under_a_second_persona(self):
        """The wash trade the agent_id check alone would miss: a different
        agent_id sharing the funder's Nous account."""
        ok, why = _enter(agent_id="funders_other_face", agent_nous_user_id="nous_funder")
        assert not ok and "shares a Nous account" in why

    def test_key_only_agents_are_not_entrants(self):
        ok, why = _enter(is_nous_verified=False)
        assert not ok and "nous_verified" in why

    def test_verified_but_unattributable_is_refused_not_waved_through(self):
        """nous_verified with no account id cannot be counted against an
        account, so it cannot be Sybil-limited. Refused rather than admitted as
        an unlimited entrant — 'could not tell' is not 'yes'."""
        ok, why = _enter(agent_nous_user_id=None)
        assert not ok and "cannot be counted" in why

    def test_entry_closes_at_the_deadline(self):
        ok, why = _enter(now=SOON)
        assert not ok and "closed" in why

    def test_a_clean_entrant_still_gets_in(self):
        """The guard has to admit somebody, or it is just a wall."""
        ok, why = _enter(agent_id="stranger", agent_nous_user_id="nous_stranger")
        assert ok and why == ""


def test_the_real_module_registry_agrees_with_this_lane():
    """The tests above use stub classes, which proves the rule and nothing
    about the modules actually shipped. This binds to the real registry.

    If a future module gains or loses the ability to name a winner, this is
    where it surfaces — deliberately asserting the *set*, not just membership,
    so a module quietly gaining DECLARES_WINNER (and with it the right to
    decide who gets paid) cannot pass unnoticed.
    """
    # Registration happens in relay_server's import block, not in
    # emporia.modules — whose __init__ is a comment. Importing the package
    # registers nothing, which this test found the honest way.
    import emporia.relay_server  # noqa: F401
    from emporia.bounties import judging_modules
    from emporia.module_sdk import MODULE_REGISTRY

    judges = judging_modules(MODULE_REGISTRY)
    # P132: blotto decides by its rules (rounds won, or the clock), like chess.
    assert judges == ["emporia:blotto:v1", "emporia:chess:v1", "emporia:lowest-unique:v1",
                      "emporia:service:v1", "emporia:two-thirds:v1"], (
        f"modules that may decide a bounty changed: {judges}. If that is intended, "
        f"update this test deliberately — DECLARES_WINNER grants the right to decide "
        f"who receives escrowed money."
    )
    # And the ones that reach a terminal state without naming a winner stay out.
    assert "emporia:research:v1" in MODULE_REGISTRY
    assert "emporia:research:v1" not in judges
    assert "emporia:code-review:v1" not in judges


class TestCapacity:
    """P72-6: a contest that accepts entries it cannot seat is taking people's
    time under false pretences — the same defect as advertising a prize with no
    hold behind it, which P72-5 fixed. Both judging modules seat exactly two."""

    def test_seats_come_from_the_module_not_the_funder(self):
        """A bounty cannot be posted for more players than its judge can
        referee, so the funder does not get to choose."""
        spec = _spec()
        assert (spec.min_entrants, spec.max_entrants) == (2, 2)

    def test_entry_closes_when_the_seats_are_taken(self):
        ok, why = _enter(existing_entrant_nous_ids={"nous_a", "nous_b"})
        assert not ok and "full" in why

    def test_the_last_seat_is_still_available(self):
        """The capacity check must admit the entrant who fills the contest, not
        refuse them — an off-by-one here silently halves every bounty."""
        ok, why = _enter(existing_entrant_nous_ids={"nous_a"})
        assert ok and why == ""


class TestReadyToRun:
    def test_a_full_contest_starts_without_waiting_for_the_deadline(self):
        from emporia.bounties import is_ready_to_run
        assert is_ready_to_run(_spec(), 2, now=NOW)

    def test_an_empty_contest_never_starts(self):
        from emporia.bounties import is_ready_to_run
        assert not is_ready_to_run(_spec(), 0, now=NOW)
        # Not even at the deadline: the module cannot run one-sided.
        assert not is_ready_to_run(_spec(), 0, now=SOON)
        assert not is_ready_to_run(_spec(), 1, now=SOON)

    def test_before_the_deadline_a_short_contest_waits(self):
        """One entrant with time left is not a failed contest, it is an open
        one. Starting it would lock out anyone still deciding."""
        from emporia.bounties import is_ready_to_run
        assert not is_ready_to_run(_spec(), 1, now=NOW)


def test_seats_have_one_definition_not_two():
    """P72-8: `validate_spec` read seats from the module registry while the
    route's `_spec_from_row` rebuilt a spec and took BountySpec's defaults —
    two implementations of one rule, agreeing only because both judging
    modules happen to seat exactly two.

    Asserted against a module that does NOT seat two, because that is the only
    shape where the two answers differ. With a (2, 2) module the bug is
    invisible, which is exactly why it survived.
    """
    from emporia.bounties import BountySpec, seats_for

    class _FourSeater:
        DECLARES_WINNER = True
        MIN_PARTICIPANTS = 3
        MAX_PARTICIPANTS = 4

    registry = {"emporia:four:v1": _FourSeater}
    assert seats_for("emporia:four:v1", registry) == (3, 4)

    spec = validate_spec(
        prize_cents=5000, module_type="emporia:four:v1", entry_deadline=SOON,
        funder_id="funder", registry=registry, now=NOW,
    )
    assert (spec.min_entrants, spec.max_entrants) == (3, 4)
    # And the defaults a rebuilt spec would silently fall back to are not these.
    assert (BountySpec(0, "", SOON, "f").min_entrants,
            BountySpec(0, "", SOON, "f").max_entrants) == (2, 2), \
        "if the defaults ever match a real module, this test stops proving anything"


class TestRails:
    """P75 asked whether a bounty can be on any rail and answered "Stripe only,
    because x402 signs and settles in one step". **P77 corrects that.**

    That described `_verify_x402_payment()`, which calls the facilitator's
    /verify and /settle back to back — not the protocol, which defines them as
    separate endpoints. An EIP-3009 authorization carries validBefore: a signed,
    time-bounded instruction anyone can submit later. Deferring the settle is
    exactly what a bounty needs.

    What survives from P75 is the *guarantee*, not the capability. Stripe's
    manual capture takes funds out of the payer's reach; an authorization
    reserves nothing, so the funder can move the USDC and the settle fails. Held
    versus pledged — both real, not the same promise.
    """

    def test_both_rails_can_carry_a_prize(self):
        from emporia.bounties import rail_can_hold_a_prize
        from emporia.payments import RAIL_STRIPE, RAIL_X402

        assert rail_can_hold_a_prize(RAIL_STRIPE)
        assert rail_can_hold_a_prize(RAIL_X402), (
            "x402 defers settlement by design — /verify and /settle are separate "
            "facilitator calls and an EIP-3009 authorization is valid until "
            "validBefore"
        )

    def test_an_unknown_rail_raises_rather_than_defaulting(self):
        """A default here would read as a guarantee nobody made."""
        import pytest as _pytest

        from emporia.bounties import escrow_strength

        with _pytest.raises(ValueError, match="cannot carry a bounty prize"):
            escrow_strength("lightning")

    def test_every_bounty_rail_is_still_a_known_rail(self):
        from emporia.bounties import BOUNTY_RAILS
        from emporia.payments import KNOWN_RAILS

        assert BOUNTY_RAILS <= KNOWN_RAILS, (
            "a bounty cannot be funded on a rail the settlement ledger does not "
            "recognise — PaymentRef would carry a tag nothing can read"
        )


class TestPromisedPrizes:
    """P79: x402 bounties are paid after the result, not before.

    P78 established that a pledge is impossible on this rail — `to` is inside
    the signed EIP-3009 struct, so the recipient is fixed at signing and the
    winner does not exist at funding. Custody was declined, which leaves paying
    once a winner is known. That is a promise, and the only thing backing it is
    what happens when it is broken.

    So these rules ARE the product. A promised bounty with a soft default rule
    is worth nothing, and an entrant who spends an hour on one has taken a
    credit risk nobody priced.
    """

    def test_the_two_strengths_are_not_the_same_promise(self):
        from emporia.bounties import ESCROW_HELD, ESCROW_PROMISED, escrow_strength
        from emporia.payments import RAIL_STRIPE, RAIL_X402

        assert escrow_strength(RAIL_STRIPE) == ESCROW_HELD
        assert escrow_strength(RAIL_X402) == ESCROW_PROMISED
        assert ESCROW_HELD != ESCROW_PROMISED

    def test_one_unpaid_default_disqualifies(self):
        """Not a count, not a ratio. A funder who owes a winner money is not
        entitled to ask another agent to work on credit — softening this makes
        the whole mechanism decorative."""
        from emporia.bounties import funder_may_promise

        ok, why = funder_may_promise(unpaid_defaults=1, outstanding_promises=0)
        assert not ok and "unpaid bounty default" in why

    def test_outstanding_promises_are_capped(self):
        """Defaults only bite after a contest resolves, so without a cap a
        funder posts unlimited promised bounties before the first comes due and
        defaults on all of them at once."""
        from emporia.bounties import funder_may_promise

        ok, _ = funder_may_promise(unpaid_defaults=0, outstanding_promises=2, max_outstanding=3)
        assert ok, "under the cap must be allowed, or nobody can promise at all"
        ok, why = funder_may_promise(unpaid_defaults=0, outstanding_promises=3, max_outstanding=3)
        assert not ok and "limit is 3" in why

    def test_a_clean_funder_may_promise(self):
        from emporia.bounties import funder_may_promise

        ok, why = funder_may_promise(unpaid_defaults=0, outstanding_promises=0)
        assert ok and why == ""

    def test_payment_is_due_a_bounded_time_after_the_result(self):
        """A winner must not be left waiting on a promise indefinitely, and the
        deadline has to arrive on its own — the flag is the only thing backing
        the prize, so it cannot wait for someone to complain."""
        from datetime import timedelta

        from emporia.bounties import PAYMENT_WINDOW, payment_due_at

        resolved = NOW
        assert payment_due_at(resolved) == resolved + PAYMENT_WINDOW
        assert PAYMENT_WINDOW >= timedelta(hours=24), (
            "shorter than a day punishes an agent woken on a daily schedule for "
            "being asleep rather than for defaulting"
        )


# P82 ------------------------------------------------------------------------

def test_a_deadline_without_a_timezone_is_refused_not_a_crash():
    """Comparing a naive datetime to an aware one raises TypeError, which the
    route surfaced as a 500."""
    with pytest.raises(BountyRejected, match="no timezone"):
        _spec(entry_deadline=SOON.replace(tzinfo=None))


def test_a_held_prize_cannot_outlive_its_hold():
    """The hold is taken at funding and lapses after about seven days. A
    contest that closes entries later would be played for money that is gone."""
    from emporia.bounties import MAX_HELD_ENTRY_WINDOW

    too_late = NOW + MAX_HELD_ENTRY_WINDOW + timedelta(minutes=1)
    with pytest.raises(BountyRejected, match="at most 5 days"):
        validate_spec(prize_cents=5000, module_type="emporia:chess:v1",
                      entry_deadline=too_late, funder_id="f", registry=REGISTRY,
                      rail="stripe", now=NOW)
    # A promise holds nothing, so nothing can lapse.
    spec = validate_spec(prize_cents=5000, module_type="emporia:chess:v1",
                         entry_deadline=too_late, funder_id="f", registry=REGISTRY,
                         rail="x402", now=NOW)
    assert spec.entry_deadline == too_late
