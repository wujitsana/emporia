"""Agent service marketplace module. Only imports: stdlib.

Flow:
  Session participants: [buyer, seller]
  1. seller  → "accept"   : acknowledges the job
  2. seller  → "deliver"  : submits deliverable
  3. buyer   → "confirm"  : accepts delivery → terminal, winner=seller, seller gets paid
     buyer   → "dispute"  : rejects delivery → terminal, winner=buyer, hold released

Timeout logic in is_terminal(): if deadline passes without delivery, buyer is refunded;
if past deadline after delivery but buyer hasn't responded, auto-confirm for seller.

outcome dict shape:
  winner:       agent_id of the winning party
  outcome_type: "won" (capture + transfer to winner) | "refund" (cancel holds)
  status:       human-readable terminal status
  reason:       why it ended
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from emporia.module_sdk import (
    InteractionModule,
    PaymentRules,
    SessionAction,
    SessionResult,
    SessionState,
    register_module,
)

_DEFAULT_DEADLINE_HOURS = 24
_BUYER_CONFIRM_WINDOW_HOURS = 6  # auto-confirm for seller after delivery + this window


@register_module
class ServiceModule(InteractionModule):
    """Agent service marketplace: buyer commissions work, seller delivers, buyer confirms."""

    MODULE_TYPE = "emporia:service:v1"
    MIN_PARTICIPANTS = 2
    MAX_PARTICIPANTS = 2

    PAYMENT_RULES = PaymentRules(
        mode="stripe_link",
        stake_per_participant="10.00",
        currency="USD",
        platform_fee_bps=250,
        payout_distribution={"seller": 0.975},
    )

    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        buyer = participants[0]
        seller = participants[1] if len(participants) > 1 else ""
        deadline_hours = config.get("deadline_hours", _DEFAULT_DEADLINE_HOURS)
        now = datetime.now(UTC)
        return SessionState(
            data={
                "buyer": buyer,
                "seller": seller,
                "description": config.get("description", ""),
                "requirements": config.get("requirements", []),
                "status": "pending_acceptance",
                "deliverable": None,
                "delivered_at": None,
                "deadline": (now + timedelta(hours=deadline_hours)).isoformat(),
                "dispute_reason": None,
            },
            current_agent=seller,
            step_number=0,
            metadata={"participants": participants, "created_at": now.isoformat()},
        )

    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        status = state.data.get("status")
        agent = action.agent_id
        buyer = state.data["buyer"]
        seller = state.data["seller"]

        if action.action_type == "accept":
            if agent != seller:
                return False, "Only the seller can accept the job"
            if status != "pending_acceptance":
                return False, f"Cannot accept in status '{status}'"
        elif action.action_type == "deliver":
            if agent != seller:
                return False, "Only the seller can deliver"
            if status not in ("accepted", "pending_acceptance"):
                return False, f"Cannot deliver in status '{status}'"
            if not action.payload.get("deliverable"):
                return False, "'deliverable' required in payload"
        elif action.action_type == "confirm":
            if agent != buyer:
                return False, "Only the buyer can confirm delivery"
            if status != "delivered":
                return False, f"Nothing to confirm — status is '{status}'"
        elif action.action_type == "dispute":
            if agent != buyer:
                return False, "Only the buyer can dispute"
            if status != "delivered":
                return False, f"Nothing to dispute — status is '{status}'"
            if not action.payload.get("reason"):
                return False, "'reason' required when disputing"
        else:
            return False, f"Unknown action type: '{action.action_type}'"

        return True, ""

    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        buyer = state.data["buyer"]
        seller = state.data["seller"]
        new_data = dict(state.data)

        if action.action_type == "accept":
            new_data["status"] = "accepted"
            next_agent = seller
        elif action.action_type == "deliver":
            new_data["status"] = "delivered"
            new_data["deliverable"] = action.payload.get("deliverable")
            new_data["delivered_at"] = datetime.now(UTC).isoformat()
            next_agent = buyer
        elif action.action_type == "confirm":
            new_data["status"] = "confirmed"
            next_agent = seller
        elif action.action_type == "dispute":
            new_data["status"] = "disputed"
            new_data["dispute_reason"] = action.payload.get("reason")
            next_agent = buyer
        else:
            next_agent = state.current_agent

        new_state = SessionState(
            data=new_data,
            current_agent=next_agent,
            step_number=state.step_number + 1,
            metadata=state.metadata,
        )
        return SessionResult(
            success=True,
            new_state=new_state,
            artifacts={"status": new_data["status"]},
        )

    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        status = state.data.get("status")
        buyer = state.data["buyer"]
        seller = state.data["seller"]
        now = datetime.now(UTC).isoformat()

        if status == "confirmed":
            return True, {
                "winner": seller,
                "outcome_type": "won",
                "status": "confirmed",
                "reason": "Buyer confirmed delivery — seller receives payment",
            }

        if status == "disputed":
            return True, {
                "winner": buyer,
                "outcome_type": "refund",
                "status": "disputed",
                "reason": state.data.get("dispute_reason", "Buyer disputed delivery"),
            }

        deadline = state.data.get("deadline", "")
        delivered_at = state.data.get("delivered_at")

        if deadline and now > deadline:
            if status == "delivered" and delivered_at:
                # Seller delivered; buyer window for confirmation has also passed
                confirm_deadline = (
                    datetime.fromisoformat(delivered_at.replace("Z", "+00:00"))
                    + timedelta(hours=_BUYER_CONFIRM_WINDOW_HOURS)
                ).isoformat()
                if now > confirm_deadline:
                    return True, {
                        "winner": seller,
                        "outcome_type": "won",
                        "status": "timeout_auto_confirmed",
                        "reason": "Delivery window passed without buyer response — auto-confirmed",
                    }
            elif status in ("pending_acceptance", "accepted"):
                # Seller never delivered
                return True, {
                    "winner": buyer,
                    "outcome_type": "refund",
                    "status": "timeout_no_delivery",
                    "reason": "Deadline passed without delivery — buyer refunded",
                }

        return False, {}
