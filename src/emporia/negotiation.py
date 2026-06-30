"""Midpoint counter-offer negotiation for Emporia.

Buyer and seller roles are symmetric:
  - Buyer: accept if proposed_price <= max_budget; else counter at midpoint clamped to max_budget
  - Vendor: accept if proposed_price >= min_acceptable; else counter at midpoint clamped to min_acceptable

All 5 negotiation message types handled by the relay broker:
  challenge, counter_offer, accept, reject, chat
"""

from __future__ import annotations

from typing import Any


def process_offer(
    sender: str,
    offer_payload: dict[str, Any],
    constraints: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Process an inbound offer against local constraints.

    Returns (decision, response_payload) where decision is 'ACCEPT' or 'COUNTER'.
    """
    proposed_price = float(offer_payload.get("price_usd", 0.0))
    max_budget = float(constraints.get("max_budget_usd", 10.0))
    min_acceptable = float(constraints.get("min_acceptable_usd", 1.0))
    role = constraints.get("role", "buyer")

    if role == "buyer":
        if proposed_price <= max_budget:
            return "ACCEPT", {"status": "ACCEPTED", "price_usd": proposed_price}
        counter_price = round((max_budget + proposed_price) / 2.0, 4)
        return "COUNTER", {"status": "COUNTER", "price_usd": min(counter_price, max_budget)}
    else:  # vendor / seller
        if proposed_price >= min_acceptable:
            return "ACCEPT", {"status": "ACCEPTED", "price_usd": proposed_price}
        counter_price = round((min_acceptable + proposed_price) / 2.0, 4)
        return "COUNTER", {"status": "COUNTER", "price_usd": max(counter_price, min_acceptable)}
