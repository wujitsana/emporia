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


class _CannotJudge:
    DECLARES_WINNER = False


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
        """Refused now, not after an entrant has done the work. payments.py's
        docstring claims a floor enforced by a constant that does not exist."""
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
    assert judges == ["emporia:chess:v1", "emporia:service:v1"], (
        f"modules that may decide a bounty changed: {judges}. If that is intended, "
        f"update this test deliberately — DECLARES_WINNER grants the right to decide "
        f"who receives escrowed money."
    )
    # And the ones that reach a terminal state without naming a winner stay out.
    assert "emporia:research:v1" in MODULE_REGISTRY
    assert "emporia:research:v1" not in judges
    assert "emporia:code-review:v1" not in judges
