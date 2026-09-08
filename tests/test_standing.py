"""Standing (P72-3) — pure. No relay, no database.

The refund test is the one that matters. Everything else guards it.
"""
from emporia.standing import compute_standings


def _settled(winner, cents=1000, transfer="transferred"):
    return {"winner_id": winner, "status": "settled",
            "winner_payout_cents": cents, "transfer_status": transfer}


def _refunded(winner):
    """What the dispute/timeout path actually writes: a nominal winner, a
    'refunded' status and a zero payout."""
    return {"winner_id": winner, "status": "refunded",
            "winner_payout_cents": 0, "transfer_status": "refunded"}


def _played(agent, module="emporia:chess:v1"):
    return {"agent_id": agent, "module_type": module}


def test_a_refunded_session_is_not_a_win():
    """THE test. A cancelled session still records a winner_id, so
    `COUNT(*) WHERE winner_id = ?` — which is what both agent endpoints did
    before this pass — scores an abandoned contest as a victory, rewarding
    exactly the behaviour a contest should discourage."""
    s = compute_standings([_refunded("a"), _settled("a")], [_played("a"), _played("a")])
    assert len(s) == 1
    assert s[0].wins == 1, "only the settled row is a win"
    assert s[0].entries == 2


def test_winning_is_not_being_paid():
    """A settled row carries one of four transfer states and three of them mean
    the prize is owed, not delivered — the condition P51's reconciler chases.
    Collapsing them would tell an agent money is in an account that is empty."""
    s = compute_standings(
        [_settled("a", 5000, "transferred"), _settled("a", 3000, "pending_connect")],
        [_played("a"), _played("a")],
    )
    assert s[0].wins == 2
    assert s[0].prize_awarded_cents == 8000
    assert s[0].prize_paid_cents == 5000
    assert s[0].prize_owed_cents == 3000, "owed is a payout to chase, not a rounding artefact"


def test_money_is_absent_from_the_public_shape():
    """A leaderboard is a public read. Per-agent earnings are not competitive
    standing, and N6 was already a finding about settlement detail reaching
    agents who should not see it."""
    s = compute_standings([_settled("a")], [_played("a")])[0]
    public = s.to_dict()
    assert "prize_awarded_cents" not in public
    assert "prize_paid_cents" not in public
    assert public["wins"] == 1 and public["entries"] == 1
    assert "prize_awarded_cents" in s.to_dict(include_money=True)


def test_agents_who_have_only_lost_still_appear():
    """Losing is a position on a table. Dropping the losers makes it a trophy
    cabinet and removes the reason to enter a second time."""
    s = compute_standings([_settled("winner")],
                          [_played("winner"), _played("loser"), _played("loser")])
    ids = [x.agent_id for x in s]
    assert "loser" in ids
    loser = next(x for x in s if x.agent_id == "loser")
    assert loser.wins == 0 and loser.entries == 2 
    assert loser.to_dict()['win_rate'] == 0.0


def test_order_is_total_so_equal_rows_do_not_reshuffle():
    """Wins, then money delivered, then entries, then agent_id. Without the
    last key two equal agents could swap places between polls for no reason."""
    rows = compute_standings(
        [_settled("zeta"), _settled("alpha")],
        [_played("zeta"), _played("alpha")],
    )
    assert [r.agent_id for r in rows] == ["alpha", "zeta"]


def test_an_agent_with_no_entries_has_no_win_rate_rather_than_zero():
    """None is 'no games', 0.0 is 'played and never won'. Rendering the first
    as the second is the same collapse the integrity badges fixed."""
    s = compute_standings([], [])
    assert s == []
    only_win = compute_standings([_settled("ghost")], [])[0]
    assert only_win.entries == 0
    assert only_win.to_dict()["win_rate"] is None
