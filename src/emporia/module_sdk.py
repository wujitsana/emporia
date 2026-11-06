"""Generalized Interaction Module Interface for Emporia (emporia).

Defines the standard interface any turn-based collaboration must implement.
Games, code reviews, research, supply chain — all use this same interface.

Terminology:
  - "module" / InteractionModule: the pluggable ruleset (chess, code_review, …)
  - "capability" is reserved for Emporia's own `emporia:*:v1` capability
    namespace (see identity.build_relay_descriptor) — unrelated to A2A
"""

from __future__ import annotations

import importlib
import json
import re
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


STAKE_WAGER = "wager"
STAKE_PURCHASE = "purchase"


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
    #: P133: the rules, for agents, in plain text — served by the relay's
    #: /modules, so an agent on any system learns a game from the relay that
    #: hosts it, with nothing to install. Say how to win, how to move, the
    #: clock, and what a draw does.
    RULES: str = ""
    MIN_PARTICIPANTS: int = 2
    MAX_PARTICIPANTS: int = 8

    PAYMENT_RULES: PaymentRules = PaymentRules()

    # Who can read a session's actions/state/audit when it has no linked room to inherit
    # visibility from (see relay_server.py's _session_viewer_allowed()):
    #   "public"       — anyone (default; matches the pre-existing behavior for modules that
    #                     don't override this, e.g. spectator/replay use cases like chess)
    #   "participants" — only session participants (or the relay operator). Modules whose
    #                     PAYMENT_RULES charge for the work should set this — the session's
    #                     action payload/artifacts *are* the delivered product.
    DEFAULT_VISIBILITY: str = "public"

    # P72-2: can this module decide a contest on its own?
    #
    # A bounty pays a winner out of escrow, so something has to name one. Only
    # a module that reaches a terminal state with a `winner` can do that
    # without a judge, which is the whole reason the first bounty lane ships
    # before any voting exists.
    #
    # Declared here rather than kept as a list in the bounty code, because a
    # list of module names in another file is a second copy of a rule that
    # already lives in the module — and this codebase has now been bitten
    # three times by exactly that (P55, P59, P70). A new module answers for
    # itself; nothing needs updating elsewhere.
    #
    # False by default: a module that has not thought about it cannot judge
    # money. `research` and `code_review` reach terminal states but name no
    # winner, and so are correctly excluded without either file changing.
    DECLARES_WINNER: bool = False

    # P95: who stakes, and who can be paid. The session routes used to apply
    # one rule to every module — "creator joins free, challenger pays" — which
    # was wrong both ways it could be: a wager's creator risked nothing, and a
    # service session charged the *seller* (the joiner) while the buyer paid
    # nothing at all.
    #
    #   STAKE_WAGER     every participant holds the stake; the winner, decided
    #                   later, is paid. Stripe holds only: an x402 authorization
    #                   names its payee when signed and reserves nothing.
    #   STAKE_PURCHASE  participants[0] (the buyer) pays participants[1] (the
    #                   seller). The payee is known once the seller joins, so
    #                   the hold is a destination charge to the seller's own
    #                   Stripe account and never sits on the platform balance.
    #   None            takes no stake; a session that asks for one is refused.
    STAKE_MODEL: str | None = None

    #: P149: payload fields holding free text that, in a private session whose
    #: players all publish encryption keys, must arrive sealed to the players
    #: (crypto_box.session_aad) — the relay stores and relays ciphertext only.
    #: Moves stay readable: the relay must check them against the rules.
    PRIVATE_TEXT_FIELDS: tuple[str, ...] = ()

    #: P136: False for simultaneous-move games — every player may act in any
    #: order, so the relay skips its own "Not your turn" check (the module's
    #: validate_action decides) and tells every player in state.data["pending"]
    #: when a phase opens. True (the default): strictly current_agent's move.
    TURN_ORDER: bool = True

    #: P135: True when an engine decides the game (chess, Go): nothing the relay
    #: sees can tell an agent's own play from an engine's, so money on it pays
    #: whoever runs the stronger engine. Such a module takes no wager stake and
    #: no bounty prize unless the operator opts in (EMPORIA_ENGINE_GAME_STAKES=1).
    ENGINE_DECIDABLE: bool = False

    #: P131: True when is_terminal() can end a session by the clock alone, so
    #: the relay's deadline sweep has to ask it (not only submit_action).
    TIMED: bool = False

    #: Calls (gamify): the module type that replaced this one, when it is retired. A
    #: retired type creates no new session (POST /sessions refuses it, naming the
    #: successor); one already running finishes under its own rules.
    RETIRED_FOR: str | None = None

    #: Review pass 2, P1 (decided 11-06): False when one player's gain is not the other's
    #: loss (Bargain: a good deal raises both). Such a game keeps its own Craft ladder and
    #: board, but its rating feeds neither the Craft level, the class, nor a ladder
    #: challenge: a non-zero-sum rating drifts upward with play (`progression:v2`).
    ZERO_SUM: bool = True

    #: Bargain: True when the relay deals each player hidden information (its
    #: own priorities) from a seed nobody can choose — the relay's secret,
    #: committed to in the game's config before anyone joins, mixed with each
    #: player's signature. `state.data["deal"]` then has the shape `new_deal()`
    #: makes: the relay fills `contributions` at the start, and `relay_seed`,
    #: `seed` and `hands` only for its own calls into the module during play —
    #: they are stripped from everything stored or served (`conceal`) until the
    #: game ends, when the whole deal is published for anyone to check. The
    #: relay holds the hands while the game runs, so such a game takes no stake
    #: and no bounty prize: whoever runs the relay could tip one side.
    DEALS_HIDDEN: bool = False

    #: True when table talk is part of the game: a session is created with
    #: `chat` on unless its creator turned it off.
    CHAT_BY_DEFAULT: bool = False

    #: Action types whose payload is a move `move_problem(data, payload)` can
    #: check before it is signed and sent (the MCP tools ask it first).
    CHECKED_ACTIONS: tuple[str, ...] = ()

    def deal(self, seed: str, participants: list[str], config: dict[str, Any]) -> dict[str, Any]:
        """DEALS_HIDDEN: each player's hidden information from the game's seed and
        settings — pure and deterministic, so anyone can redo it after the reveal."""
        raise NotImplementedError

    def private_view(self, data: dict[str, Any], agent_id: str) -> dict[str, Any] | None:
        """DEALS_HIDDEN: what `agent_id` may read of its own hand (served to it alone)."""
        hands = (data.get("deal") or {}).get("hands") or {}
        return hands.get(agent_id)

    def validate_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        """Check a session's settings at creation, before anyone stakes on them
        (P131). Return (valid, error_message); the default accepts anything."""
        return True, ""

    def ready_to_start(self, participants: list[str], config: dict[str, Any]) -> bool:
        """P136: whether the session starts with these participants. Default: as
        soon as the minimum has joined. A game for a chosen number of players
        waits for all of them."""
        return len(participants) >= self.MIN_PARTICIPANTS

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
# Hidden deals (DEALS_HIDDEN modules)
# ============================================================================
#
# The relay picks a secret `relay_seed` when the game is made and publishes only
# `deal_commitment(relay_seed)` in its config. Each player contributes a
# signature it cannot vary: the creator its signed creation (made before the
# commitment existed), the joiner its signature over the commitment itself
# (`wire.signed_deal_seed_payload`, deterministic Ed25519). The game's seed is
# `deal_seed(relay_seed, [p0's, p1's])`. The relay cannot steer it (it fixed its
# seed before the joiner's signature existed, and that signature binds the
# commitment); a player cannot (it never sees the relay's seed until the end).

DEAL_FIELDS_HIDDEN = ("relay_seed", "seed", "hands")


def deal_commitment(relay_seed: str) -> str:
    import hashlib
    return hashlib.sha256(f"emporia:deal-commitment:v1:{relay_seed}".encode()).hexdigest()


def deal_seed(relay_seed: str, contributions: list[str]) -> str:
    """The game's seed: the relay's secret and every player's contribution, in seat order."""
    import hashlib
    return hashlib.sha256(":".join(["emporia:deal-seed:v1", relay_seed, *contributions]).encode()).hexdigest()


def new_deal(commitment: str | None) -> dict[str, Any]:
    return {"commitment": commitment, "contributions": {}, "relay_seed": None, "seed": None, "hands": None}


def with_deal(state: SessionState, **fields: Any) -> SessionState:
    """`state` with these fields of its deal replaced (a new state; the old is untouched)."""
    deal = {**(state.data.get("deal") or new_deal(None)), **fields}
    return SessionState(data={**state.data, "deal": deal}, current_agent=state.current_agent,
                        step_number=state.step_number, metadata=state.metadata)


def conceal(state: SessionState) -> SessionState:
    """`state` as it may be stored and served while the game runs: no seed, no hands."""
    if "deal" not in state.data:
        return state
    return with_deal(state, **{k: None for k in DEAL_FIELDS_HIDDEN})


def check_deal(module: InteractionModule, participants: list[str], config: dict[str, Any],
               deal: dict[str, Any]) -> str:
    """"" when a published deal is what its commitment and contributions give, else why not.
    The signatures themselves are checked by the caller against the players' keys."""
    if not deal or not deal.get("relay_seed"):
        return "the deal has not been revealed"
    if deal_commitment(deal["relay_seed"]) != deal.get("commitment") or \
            deal.get("commitment") != (config or {}).get("deal_commitment"):
        return "the revealed relay seed does not match the commitment published before the game"
    contrib = deal.get("contributions") or {}
    if any(p not in contrib for p in participants):
        return "a player's contribution to the seed is missing"
    seed = deal_seed(deal["relay_seed"], [contrib[p]["signature"] for p in participants])
    if seed != deal.get("seed"):
        return "the game's seed is not the one its relay seed and contributions give"
    if module.deal(seed, participants, config or {}) != deal.get("hands"):
        return "the dealt hands are not the ones the seed gives"
    return ""


# ============================================================================
# Module Registry
# ============================================================================

MODULE_REGISTRY: dict[str, type[InteractionModule]] = {}

#: `<namespace>:<name>:v<N>` — lowercase. `emporia:` is the bundled games' own.
MODULE_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*:v[0-9]+")
BUNDLED_NAMESPACE = "emporia"


def register_module(cls: type[InteractionModule]) -> type[InteractionModule]:
    """Add a module to the registry. P133: a type is registered once — a
    second class under the same name used to replace the first silently, so any
    import could swap out `emporia:chess:v1`, which decides who is paid."""
    mt = cls.MODULE_TYPE
    if not MODULE_TYPE_RE.fullmatch(mt or ""):
        raise ValueError(f"MODULE_TYPE {mt!r} must look like 'namespace:name:v1' (lowercase)")
    existing = MODULE_REGISTRY.get(mt)
    if existing is not None and existing is not cls and (
            existing.__module__, existing.__qualname__) != (cls.__module__, cls.__qualname__):
        raise ValueError(f"{mt} is already registered by {existing.__module__}.{existing.__qualname__}")
    MODULE_REGISTRY[mt] = cls
    return cls


def load_bundled_modules() -> None:
    """Register every bundled game here too (P150). Only the relay imported
    them, so in an agent's own process — where verify_session replays a
    game's rules — the registry was empty and every verdict "unchecked"."""
    import pkgutil

    import emporia.modules
    for m in pkgutil.iter_modules(emporia.modules.__path__):
        importlib.import_module(f"emporia.modules.{m.name}")


def engine_game_stakes_allowed() -> bool:
    """P135: the operator's opt-in to money on ENGINE_DECIDABLE games."""
    import os
    return os.environ.get("EMPORIA_ENGINE_GAME_STAKES", "").strip().lower() in ("1", "true", "yes")


ENGINE_GAME_REFUSAL = (
    "{mt} is decided by engines: nothing the relay sees can tell an agent's own play "
    "from an engine's, so money on it pays whoever runs the stronger engine. This relay "
    "takes no stakes or prizes on it — play it free, or stake on a game an engine cannot "
    "simply win (emporia:blotto:v2). Operators can allow it with EMPORIA_ENGINE_GAME_STAKES=1."
)


def load_operator_modules(spec: str) -> list[str]:
    """Import an operator's own game modules (P133): `spec` is a comma-separated
    list of importable Python modules (EMPORIA_MODULES), each registering its
    classes with @register_module. Returns the module types they added.

    Explicit and loud on purpose: a module that declares a winner decides who
    receives staked money, so nothing loads by being installed, and a
    configured module that fails to import stops the relay rather than
    quietly serving without it. Operator modules name their own namespace — `emporia:`
    is reserved for the bundled games.
    """
    added: list[str] = []
    for name in (n.strip() for n in spec.split(",")):
        if not name:
            continue
        before = set(MODULE_REGISTRY)
        importlib.import_module(name)
        new = sorted(set(MODULE_REGISTRY) - before)
        for mt in new:
            if mt.split(":", 1)[0] == BUNDLED_NAMESPACE:
                del MODULE_REGISTRY[mt]
                raise ValueError(f"{name}: operator module {mt} uses the reserved "
                                 f"'{BUNDLED_NAMESPACE}:' namespace — name your own")
        if not new:
            raise ValueError(f"{name} registered no module (decorate the class with @register_module)")
        added += new
    return added


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
    for mod_type, cls in sorted(MODULE_REGISTRY.items()):
        result.append({
            "module_type": mod_type,
            "min_participants": cls.MIN_PARTICIPANTS,
            "max_participants": cls.MAX_PARTICIPANTS,
            "payment_mode": cls.PAYMENT_RULES.mode,
            # P133: what an agent needs to choose and play a game it has never seen.
            "stake_model": cls.STAKE_MODEL,
            "declares_winner": cls.DECLARES_WINNER,
            "timed": cls.TIMED,
            "engine_decidable": cls.ENGINE_DECIDABLE,
            "stakes_allowed": cls.STAKE_MODEL is not None and (
                not cls.ENGINE_DECIDABLE or engine_game_stakes_allowed()),
            "bundled": mod_type.split(":", 1)[0] == BUNDLED_NAMESPACE,
            **({"retired_for": cls.RETIRED_FOR} if cls.RETIRED_FOR else {}),
            "rules": cls.RULES.strip(),
        })
    return result


def retired_refusal(module_type: str, successor: str) -> str:
    """Why a retired module type takes no new session (calls, gamify)."""
    return (f"{module_type} is retired: create {successor} instead (the same game; its commitment also "
            f"seals an optional call). Games already running under {module_type} finish under its rules.")
