"""Generalized Interaction Module Interface for Emporia (emporia).

Defines the standard interface any turn-based collaboration must implement.
Games, code reviews, research, supply chain — all use this same interface.

Terminology:
  - "module" / InteractionModule: the pluggable ruleset (chess, code_review, …)
  - "capability" is reserved for A2A Agent Card usage only
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class SessionState:
    """Immutable state for any turn-based collaboration."""
    data: dict[str, Any] = field(default_factory=dict)
    current_agent: str = ""
    step_number: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "current_agent": self.current_agent,
            "step_number": self.step_number,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionState:
        return cls(
            data=d.get("data", {}),
            current_agent=d.get("current_agent", ""),
            step_number=d.get("step_number", 0),
            metadata=d.get("metadata", {}),
        )


@dataclass(frozen=True)
class SessionAction:
    """An action/step in the collaboration."""
    agent_id: str
    action_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "action_type": self.action_type,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


@dataclass
class SessionResult:
    """Result of applying an action."""
    success: bool
    new_state: SessionState | None = None
    error: str | None = None
    artifacts: dict[str, Any] | None = None


class PaymentRules(BaseModel):
    """Declarative payment configuration. No blockchain/wallet fields."""
    mode: str = "free"  # "free", "stripe_link", "mpp"
    stake_per_participant: str = "0"
    currency: str = "USD"
    platform_fee_bps: int = 250  # 2.5%
    payout_distribution: dict[str, float] = Field(default_factory=dict)


class InteractionModule(ABC):
    """Base class for any turn-based collaboration module.

    Modules must ONLY import their domain library (chess, stdlib, etc.).
    Zero HTTP, MCP, Stripe, or FastMCP imports — hard architectural boundary.
    """

    MODULE_TYPE: str = "emporia:generic:v1"
    MIN_PARTICIPANTS: int = 2
    MAX_PARTICIPANTS: int = 8

    PAYMENT_RULES: PaymentRules = PaymentRules()

    def validate_participants(self, participants: list[str]) -> tuple[bool, str]:
        n = len(participants)
        if n < self.MIN_PARTICIPANTS:
            return False, f"Need at least {self.MIN_PARTICIPANTS} participants, got {n}"
        if n > self.MAX_PARTICIPANTS:
            return False, f"Max {self.MAX_PARTICIPANTS} participants, got {n}"
        return True, ""

    @abstractmethod
    def initial_state(self, participants: list[str], config: dict[str, Any]) -> SessionState:
        """Create the initial collaboration state."""

    @abstractmethod
    def validate_action(self, state: SessionState, action: SessionAction) -> tuple[bool, str]:
        """Return (valid, error_message)."""

    @abstractmethod
    def apply_action(self, state: SessionState, action: SessionAction) -> SessionResult:
        """Apply action, return new state and any artifacts."""

    @abstractmethod
    def is_terminal(self, state: SessionState) -> tuple[bool, dict[str, Any]]:
        """Return (is_over, outcome_dict)."""

    def serialize_state(self, state: SessionState) -> str:
        return json.dumps(state.to_dict(), sort_keys=True)

    def deserialize_state(self, data: str) -> SessionState:
        return SessionState.from_dict(json.loads(data))


# ============================================================================
# Module Registry
# ============================================================================

MODULE_REGISTRY: dict[str, type[InteractionModule]] = {}


def register_module(cls: type[InteractionModule]) -> type[InteractionModule]:
    MODULE_REGISTRY[cls.MODULE_TYPE] = cls
    return cls


def get_interaction_module(module_type: str) -> InteractionModule:
    cls = MODULE_REGISTRY.get(module_type)
    if not cls:
        raise ValueError(f"Unknown module type: {module_type}")
    return cls()


# ============================================================================
# Session outcome
# ============================================================================

@dataclass(frozen=True)
class SessionOutcome:
    """Final outcome when collaboration terminates."""
    is_over: bool
    winner: str | None = None
    payouts: dict[str, str] | None = None
    artifacts: dict[str, Any] | None = None
    summary: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {k: v for k, v in asdict(self).items() if v is not None},
            sort_keys=True, separators=(",", ":")
        )


def list_available_modules() -> list[dict[str, Any]]:
    """Return list of available modules with metadata."""
    result = []
    for mod_type, cls in MODULE_REGISTRY.items():
        result.append({
            "module_type": mod_type,
            "min_participants": cls.MIN_PARTICIPANTS,
            "max_participants": cls.MAX_PARTICIPANTS,
            "payment_mode": cls.PAYMENT_RULES.mode,
        })
    return result
