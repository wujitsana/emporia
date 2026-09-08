"""Standing — what an agent has actually won (P72-3).

The gamification gap. `win_count` today is recomputed on each profile read and
shown on a page nobody visits; there is no table an agent can lose position on,
which is the thing that makes it worth entering a second contest. Results have
existed all along — nothing here creates data, it reads what settlement already
wrote.

Pure, like `bounties`: rows in, standings out, no I/O. The SQL belongs to the
route; the *rule* belongs here, in one place.

Two distinctions this module refuses to collapse, both found by reading what
settlement actually writes rather than what a leaderboard would like to assume:

**A refunded settlement still carries a `winner_id`.** The dispute/timeout path
records `{"winner_id": winner_id, "status": "refunded", "winner_payout_cents": 0}`
— a nominal winner of a contest where every hold was cancelled and nobody was
paid. `COUNT(*) WHERE winner_id = ?` counts those as victories. A leaderboard
built that way rewards abandoning a session, which is precisely backwards.

**Winning is not being paid.** A `settled` row carries one of four transfer
states, and three of them (`pending_connect`, `pending_connect_wrong_mode`,
`pending_connect_not_active`) mean the prize is owed and has not moved — the
condition P51's reconciler exists to chase. So `prize_awarded_cents` and
`prize_paid_cents` are separate fields and always will be. Showing only the
first would tell an agent it earned money that is not in its account; showing
only the second would erase a real win because the winner never finished Stripe
onboarding.

**Scope.** A standing is only ever "as far as this relay knows". Settlements are
local — a peer's results reach us as signed attestations, which is a later phase
— so `Standing.scope` is carried explicitly rather than left for a reader to
assume. A local table presented as a network ranking is the same class of lie as
a failed check rendered as an empty one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# A settlement counts toward standing only in this status. Everything else —
# "refunded" above all — is an outcome, not a win.
SETTLED_STATUS = "settled"

# The one transfer state in which the winner actually holds the money.
PAID_TRANSFER_STATUS = "transferred"


@dataclass
class Standing:
    agent_id: str
    wins: int = 0
    entries: int = 0
    prize_awarded_cents: int = 0
    prize_paid_cents: int = 0
    modules: set[str] = field(default_factory=set)

    @property
    def prize_owed_cents(self) -> int:
        """Won but not in their account. Non-zero here is a payout to chase,
        not a rounding artefact."""
        return self.prize_awarded_cents - self.prize_paid_cents

    def to_dict(self, *, include_money: bool = False) -> dict:
        """Standing, with the money fields off by default.

        A leaderboard is a public read, and per-agent earnings are not the same
        thing as competitive standing. N6 was a finding about settlement detail
        reaching agents who should not see it; a public table of who has earned
        how much would be a wider version of that, added on purpose. So the
        public shape is wins, entries and modules — what you need to know who is
        winning — and `include_money` is for the agent's own row or the relay
        operator, who may already see this through the settlements endpoints.
        """
        out = {
            "agent_id": self.agent_id,
            "wins": self.wins,
            "entries": self.entries,
            "win_rate": round(self.wins / self.entries, 3) if self.entries else None,
            "modules": sorted(self.modules),
        }
        if include_money:
            out |= {
                "prize_awarded_cents": self.prize_awarded_cents,
                "prize_paid_cents": self.prize_paid_cents,
                "prize_owed_cents": self.prize_owed_cents,
            }
        return out


def compute_standings(
    settlements: Iterable[Mapping],
    participations: Iterable[Mapping],
) -> list[Standing]:
    """Standings, best first.

    `settlements` rows need `winner_id`, `status`, `winner_payout_cents`,
    `transfer_status`. `participations` rows need `agent_id` and `module_type`
    — one row per (agent, session) actually joined.

    An agent with entries and no wins still appears. Being on the table having
    lost is the point of a table; dropping the losers would make it a trophy
    cabinet.
    """
    by_agent: dict[str, Standing] = {}

    def _get(agent_id: str) -> Standing:
        return by_agent.setdefault(agent_id, Standing(agent_id=agent_id))

    for row in participations:
        agent_id = row["agent_id"]
        if not agent_id:
            continue
        s = _get(agent_id)
        s.entries += 1
        if row.get("module_type"):
            s.modules.add(row["module_type"])

    for row in settlements:
        winner = row.get("winner_id")
        # The refund trap: a cancelled session records a nominal winner who was
        # paid nothing. Only a genuinely settled row is a win.
        if not winner or row.get("status") != SETTLED_STATUS:
            continue
        s = _get(winner)
        s.wins += 1
        awarded = int(row.get("winner_payout_cents") or 0)
        s.prize_awarded_cents += awarded
        if row.get("transfer_status") == PAID_TRANSFER_STATUS:
            s.prize_paid_cents += awarded

    return sorted(
        by_agent.values(),
        # Wins first, then money actually delivered, then entries as the
        # tie-break that rewards showing up. agent_id last so the order is
        # total and a redeploy cannot reshuffle equal rows.
        key=lambda s: (-s.wins, -s.prize_paid_cents, -s.entries, s.agent_id),
    )
