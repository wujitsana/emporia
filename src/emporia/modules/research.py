"""Research collaboration module. Only imports: stdlib."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from emporia.module_sdk import (
    InteractionModule,
    PaymentRules,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)


@register_module
class ResearchModule(InteractionModule):
    MODULE_TYPE = "emporia:research:v1"
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 4
    DEFAULT_VISIBILITY = "participants"  # paid deliverable, not spectator content

    # P95: free. These modules declared a stake but name no winner and no
    # payee, and no `payout_distribution` was ever read, so the stake was held
    # and then lapsed: the payer got it back days later and the worker was
    # never paid. Paid research or review runs as a `service` session, whose
    # buyer pays its seller.
    PAYMENT_RULES = PaymentRules()

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        return SessionState(
            data={
                "hypothesis": config.get("hypothesis", ""),
                "topic": config.get("topic", ""),
                "phases": ["literature", "hypothesis", "methodology", "data", "analysis", "writing"],
                "current_phase": 0,
                "contributions": {p: [] for p in participants},
                "status": "active",
            },
            current_agent=participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": datetime.now(UTC).isoformat()},
        )

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        valid_actions = {
            "submit_literature", "propose_hypothesis", "submit_data",
            "analyze", "write_section", "peer_review", "finalize",
        }
        if action.action_type not in valid_actions:
            return False, f"Invalid action: {action.action_type}"
        if action.agent_id != state.current_agent:
            return False, "Not your turn"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        participants = state.metadata.get("participants", [])
        idx = participants.index(state.current_agent)
        next_idx = (idx + 1) % len(participants)

        contributions = dict(state.data.get("contributions", {}))
        contributions.setdefault(action.agent_id, []).append({
            "type": action.action_type,
            "payload": action.payload,
            "timestamp": action.timestamp,
        })

        new_state = SessionState(
            data={**state.data, "contributions": contributions},
            current_agent=participants[next_idx],
            step_number=state.step_number + 1,
            metadata=state.metadata,
        )
        return SessionResult(success=True, new_state=new_state)

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        done = state.data.get("status") == "completed"
        return done, {"status": state.data.get("status")}
