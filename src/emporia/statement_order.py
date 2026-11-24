"""Ordering of an agent's signed statements about where it is paid (protocol 3.5a).

The +-10 minute window of `routes.agents._signed_at` alone lets a statement the agent signed minutes ago be posted
again, with its session token, after it signed a different one. Like an encryption key, a wallet or an
accept-networks list is refused (409) when its `at` is not later than the one held and the value differs. The same
value may be signed again at any time.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from emporia import wire

_WHY = "a later statement of yours is already held here; sign a new one (its at must be later)"


def _older(held_at: str, at: str) -> bool:
    held, new = wire.when_of(held_at), wire.when_of(at)
    return bool(held and new and new <= held)


def refuse_older_wallet(registry: Any, agent_id: str, network_family: str, address: str, at: str) -> None:
    """409 when `at` is not later than the held wallet statement for the family and the address differs."""
    held = ((registry.payee_statement(agent_id) or {}).get("wallets") or {}).get(network_family) or {}
    if held.get("address") and held["address"] != address and _older(str(held.get("at") or ""), at):
        raise HTTPException(409, f"the wallet statement is older than {_WHY}")


def refuse_older_networks(registry: Any, agent_id: str, networks: list[str], at: str) -> None:
    """409 when `at` is not later than the held accept-networks list and the list differs."""
    held = registry.accepts_networks_proof(agent_id) or {}
    if held and sorted(held.get("networks") or []) != sorted(networks) and _older(str(held.get("at") or ""), at):
        raise HTTPException(409, f"the networks statement is older than {_WHY}")
