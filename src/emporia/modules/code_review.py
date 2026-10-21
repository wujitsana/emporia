"""Code review interaction module. Only imports: stdlib."""

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
class CodeReviewModule(InteractionModule):
    MODULE_TYPE = "emporia:code-review:v1"
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 5
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
                "repository": config.get("repository", ""),
                "pr_number": config.get("pr_number", 0),
                "files": config.get("files", {}),
                "reviews": {},
                "pending_reviewers": participants[1:],
                "status": "pending",
            },
            # P89: the session route builds the initial state from the creator
            # alone, then rebuilds it once a reviewer joins. Indexing
            # participants[1] unconditionally made every code-review session a
            # 500 at creation; until a reviewer exists, the creator holds the
            # turn and nobody can act (validate_action requires a reviewer).
            current_agent=participants[1] if len(participants) > 1 else participants[0],
            step_number=0,
            metadata={"participants": participants, "created_at": datetime.now(UTC).isoformat()},
        )

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        if action.action_type == "submit_review":
            if action.agent_id not in state.data.get("pending_reviewers", []):
                return False, "Not your turn to review"
            if "comments" not in action.payload:
                return False, "Review must include comments"
        elif action.action_type == "approve":
            if action.agent_id not in state.data.get("pending_reviewers", []):
                return False, "Not your turn to approve"
        else:
            return False, f"Unknown action: {action.action_type}"
        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        pending = list(state.data.get("pending_reviewers", []))
        reviews = dict(state.data.get("reviews", {}))

        if action.action_type in ("submit_review", "approve"):
            reviews[action.agent_id] = {
                "comments": action.payload.get("comments", "Approved"),
                "approved": action.payload.get("approved", action.action_type == "approve"),
                "timestamp": action.timestamp,
            }
            pending.remove(action.agent_id)

        next_agent = pending[0] if pending else state.metadata.get("participants", [])[0]

        new_state = SessionState(
            data={
                **state.data,
                "pending_reviewers": pending,
                "reviews": reviews,
                "status": "complete" if not pending else "in_review",
            },
            current_agent=next_agent,
            step_number=state.step_number + 1,
            metadata=state.metadata,
        )
        return SessionResult(
            success=True,
            new_state=new_state,
            artifacts={"outcome": {"status": "complete" if not pending else "in_review"}},
        )

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        pending = state.data.get("pending_reviewers", [])
        if not pending:
            reviews = state.data.get("reviews", {})
            all_approved = all(r.get("approved", False) for r in reviews.values())
            return True, {"status": "approved" if all_approved else "rejected", "reviews": reviews}
        return False, {}
