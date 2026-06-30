"""Stripe payment processing for Emporia.

Payment modes supported:
  free        — no payment required
  stripe_spt  — Shared Payment Token (spt_xxx) from Stripe Link / link-cli.
                Agent calls `link-cli mpp pay`; relay validates via
                GET /v1/shared_payment/granted_tokens/{spt_id}, then creates a
                PaymentIntent with payment_method_data[shared_payment_granted_token].
                Requires a US Stripe Link account on the paying agent.
                Docs: https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens
  stripe_pi   — Standard Stripe PaymentIntent (pi_xxx). Agent or relay creates the PI;
                relay verifies status == "succeeded" or "requires_capture".
  mpp         — Machine Payments Protocol (future). Relay issues HTTP 402 with
                WWW-Authenticate: Payment ... challenge; agent wallet retries.
                Kept as enum value; full MPP server wiring is post-hackathon.

Payment split: 97.5% winner / 2.5% platform (OPERATOR_FEE_BPS=250 default).
STRIPE_SECRET_KEY must come from env — never hardcoded.

Stripe does NOT provide A2A agent identity. The SPT object exposes only:
card brand, last-four, usage limits (currency/max_amount/expires_at), deactivation
status. Agent identity on Emporia stays with Ed25519 + Nous — Stripe is payment only.
"""

from __future__ import annotations

import base64
import json
import math
import os
import secrets
from typing import Any

import httpx

def platform_fee(amount_cents: int) -> int:
    """2.5% platform fee in cents. Only collected when the rounded amount is ≥ 1¢.
    Transactions where 2.5% rounds to 0 pay no fee — payout goes 100% to the recipient.
    """
    if amount_cents <= 0:
        return 0
    fee = round(amount_cents * OPERATOR_FEE_BPS / 10000)
    return fee if fee >= 1 else 0

STRIPE_API_BASE = os.getenv("STRIPE_API_BASE", "https://api.stripe.com/v1")
OPERATOR_FEE_BPS = int(os.getenv("OPERATOR_FEE_BPS", "250"))  # 2.5%
REQUEST_TIMEOUT = float(os.getenv("EMPORIA_HTTP_TIMEOUT", "30"))
# Machine Payment Profile ID for the Stripe fiat MPP rail.
# Set via Stripe dashboard → Machine Payments → Profile. Used in mppStripe.charge().
STRIPE_PROFILE_ID = os.getenv("STRIPE_PROFILE_ID", "")

# Per-process secret for binding MPP challenges (32 random bytes).
# The mppx library uses this to sign challenge payloads so they can't be replayed
# across relay instances. Generate once per process — challenges expire at request end.
_MPP_SECRET = secrets.token_bytes(32)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ─── MPP / 402 helpers ───────────────────────────────────────────────────────
# Stripe MPP spec: https://docs.stripe.com/payments/machine/mpp
# mppx library reference: https://github.com/stripe-samples/machine-payments
#
# Stripe MPP challenge-response (fiat/SPT path):
#   1. Agent sends request with no payment credential
#   2. Relay responds 402 with:
#      WWW-Authenticate: Payment id="<chal_id>", method="stripe",
#                        intent="charge", request="<b64url_payload>"
#   3. Agent wallet (link-cli) reads the challenge, generates spt_xxx
#   4. Agent retries with:
#      Authorization: Payment <b64url_credential>
#   5. Relay extracts spt_xxx from credential, calls retrieve_spt() + confirm_spt()
#
# For agents that provide SPTs proactively (no 402 retry needed), skip to step 5.

def build_mpp_challenge(amount_cents: int, resource: str, currency: str = "usd") -> dict[str, str]:
    """Build HTTP 402 challenge headers per the official Stripe MPP spec.

    Emits the `Payment` authentication scheme required by Stripe's MPP protocol
    (https://docs.stripe.com/payments/machine/mpp). The `request` field is a
    base64url-encoded JSON payload with amount, currency, and resource.

    Challenge ID is relay-generated (prefix chal_). The mppx library on the
    agent side uses the `id` to correlate the challenge with its credential response.

    WWW-Authenticate format:
      Payment id="chal_xxx", method="stripe", intent="charge", request="<b64url>"

    Agent Authorization response:
      Authorization: Payment <b64url_credential>
    """
    chal_id = f"chal_{secrets.token_hex(12)}"
    request_payload = _b64url(json.dumps({
        "amount": amount_cents,
        "currency": currency,
        "resource": resource,
    }).encode())
    profile_part = f', profile_id="{STRIPE_PROFILE_ID}"' if STRIPE_PROFILE_ID else ""
    return {
        "WWW-Authenticate": (
            f'Payment id="{chal_id}", method="stripe", intent="charge"'
            f', request="{request_payload}"{profile_part}'
        ),
        "X-MPP-Version": "1",
        "X-MPP-Network": "stripe",
    }


def extract_mpp_token(authorization_header: str | None) -> str | None:
    """Extract SPT from Authorization header per Stripe MPP spec.

    Expected format (https://docs.stripe.com/payments/machine/mpp):
      Authorization: Payment <token_or_b64url_credential>

    The credential from mppx is base64url-encoded JSON containing the spt_xxx.
    A bare spt_xxx (from direct link-cli usage) is returned as-is.
    """
    if not authorization_header:
        return None
    header = authorization_header.strip()
    if not header.startswith("Payment "):
        return None
    parts = header.split(None, 1)
    if len(parts) != 2:
        return None
    raw = parts[1].strip()
    # Try to decode as base64url JSON credential (mppx format)
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        if isinstance(decoded, dict):
            return decoded.get("token") or decoded.get("spt") or decoded.get("credential")
    except Exception:
        pass
    return raw or None


async def retrieve_spt(spt_id: str) -> dict[str, Any]:
    """Retrieve and validate a Stripe Shared Payment Token before charging.

    Per https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens:
      GET /v1/shared_payment/granted_tokens/{spt_id}

    Returns usage limits (max_amount, currency, expires_at) and deactivation
    status. Check this before confirm_spt() to validate the token is still live
    and covers the required amount.

    Note: SPTs carry NO agent/customer identity — only card brand + last-four +
    usage limits. Agent identity on Emporia is Ed25519-based, not Stripe-based.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{STRIPE_API_BASE}/shared_payment/granted_tokens/{spt_id}",
            auth=(key, ""),
            headers={"Stripe-Version": "2026-04-22.preview"},
        )
        resp.raise_for_status()
        spt = resp.json()
        limits = spt.get("usage_limits", {})
        max_amt = limits.get("max_amount")
        return {
            "spt_id": spt_id,
            "active": spt.get("deactivated_at") is None,
            "deactivated_reason": spt.get("deactivated_reason"),
            "currency": limits.get("currency", "usd"),
            "max_amount_cents": int(max_amt * 100) if isinstance(max_amt, (int, float)) else 0,
            "expires_at": limits.get("expires_at"),
        }


async def confirm_spt(
    spt_token: str,
    amount_cents: int,
    session_id: str,
    agent_id: str,
    service_type: str = "emporia:session",
) -> dict[str, Any]:
    """Create and confirm a PaymentIntent from a Stripe SPT (spt_xxx).

    Uses the correct field per Stripe docs:
      payment_method_data[shared_payment_granted_token] = spt_xxx

    Stripe clones a PaymentMethod from the grant; subsequent refunds/reporting
    behave as if the PaymentMethod was provided directly.

    Call retrieve_spt() first to validate the token is active and covers the amount.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents",
            auth=(key, ""),
            headers={"Stripe-Version": "2026-04-22.preview"},
            data={
                "amount": str(amount_cents),
                "currency": "usd",
                "confirm": "true",
                "payment_method_data[shared_payment_granted_token]": spt_token,
                "metadata[session_id]": session_id,
                "metadata[buyer_agent_id]": agent_id,
                "metadata[service_type]": service_type,
                "metadata[protocol]": "emporia:v1+spt",
            },
        )
        if not resp.is_success:
            # Fallback: if caller passed a pi_xxx directly, treat as pre-confirmed
            if spt_token.startswith("pi_"):
                verify_resp = await client.get(
                    f"{STRIPE_API_BASE}/payment_intents/{spt_token}",
                    auth=(key, ""),
                )
                if verify_resp.is_success:
                    pi = verify_resp.json()
                    return {
                        "status": pi.get("status"),
                        "payment_intent_id": spt_token,
                        "receipt": pi.get("latest_charge") or pi.get("id"),
                        "via": "pi_direct",
                    }
            resp.raise_for_status()
        pi = resp.json()
        return {
            "status": pi.get("status"),
            "payment_intent_id": pi["id"],
            "receipt": pi.get("latest_charge") or pi.get("id"),
            "via": "spt",
        }


def _stripe_key() -> str:
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not set. "
            "Export it before running paid sessions. "
            "Use payment_mode='free' for free play."
        )
    return key


async def create_stake_intent(
    session_id: str,
    amount_cents: int,
    buyer_id: str,
    seller_id: str,
    service_type: str,
    mode: str = "stripe_pi",
) -> dict[str, Any]:
    """Create a Stripe PaymentIntent for a session stake using manual capture (escrow).

    capture_method=manual: funds are authorized (held) at join time but NOT captured
    until the session completes and a winner is determined. If the session never
    finishes, the hold expires in 5–7 days with automatic zero-platform refund.

    mode controls the accepted payment method:
      "stripe_pi"  — standard card (pm_card_visa in test, real card in live)
      "stripe_spt" — Shared Payment Token from Stripe Link / link-cli
      "mpp"        — same as stripe_spt for now; full MPP wiring is post-v1

    Capture happens in settle() when winner is confirmed.
    """
    key = _stripe_key()
    if mode in ("stripe_spt", "mpp"):
        pm_types = "link"
    else:
        pm_types = "card"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents",
            auth=(key, ""),
            data={
                "amount": str(amount_cents),
                "currency": "usd",
                "payment_method_types[]": pm_types,
                "capture_method": "manual",
                "metadata[session_id]": session_id,
                "metadata[buyer_agent_id]": buyer_id,
                "metadata[seller_agent_id]": seller_id,
                "metadata[service_type]": service_type,
                "metadata[mode]": mode,
                "metadata[protocol]": "emporia:v1+escrow",
            },
        )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": "created",
            "payment_intent_id": pi["id"],
            "client_secret": pi["client_secret"],
            "amount_cents": amount_cents,
            "capture_method": "manual",
            "mode": mode,
        }


async def verify_payment_intent(payment_intent_id: str) -> dict[str, Any]:
    """Fetch a PaymentIntent from Stripe and return its status.

    The relay calls this to verify the agent has already paid — relay does NOT
    create or confirm the payment, it only checks it. The agent is the payer;
    the relay is the merchant verifying receipt.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}",
            auth=(key, ""),
        )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": pi.get("status"),
            "payment_intent_id": payment_intent_id,
            "amount": pi.get("amount"),
            "currency": pi.get("currency"),
            "receipt": pi.get("latest_charge") or pi.get("id"),
        }


async def confirm_stripe_intent(
    payment_intent_id: str,
    payment_method_id: str | None = None,
) -> dict[str, Any]:
    """Confirm a Stripe PaymentIntent server-side.

    Two modes:
    - Test mode (sk_test_...): uses test_helpers endpoint — no payment method needed.
    - Live mode (sk_live_...): uses standard confirm endpoint with payment_method_id.
      Pass pm_card_visa for automated test confirms, or a real pm_* for production.

    For autonomous agent payments in production, agents supply a payment method via
    Stripe Link (link-cli --test for sandbox, real Link account for production) or
    a stored payment method token. MPP is the correct long-term protocol.
    """
    key = _stripe_key()
    is_test_key = key.startswith("sk_test_")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        if is_test_key:
            # Test mode: auto-confirm via test helper, no payment method needed
            resp = await client.post(
                f"{STRIPE_API_BASE}/test_helpers/payment_intents/{payment_intent_id}/confirm",
                auth=(key, ""),
                data={"metadata[protocol]": "emporia:v1"},
            )
        else:
            # Live mode: server-side confirm requires a payment method
            pm = payment_method_id or "pm_card_visa"
            resp = await client.post(
                f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/confirm",
                auth=(key, ""),
                data={
                    "payment_method": pm,
                    "metadata[protocol]": "emporia:v1",
                },
            )
        resp.raise_for_status()
        pi = resp.json()
        return {
            "status": pi.get("status"),
            "payment_intent_id": payment_intent_id,
            "receipt": pi.get("latest_charge") or pi.get("id"),
        }


async def capture_payment_intent(payment_intent_id: str) -> dict[str, Any]:
    """Capture a manually-authorized PaymentIntent (escrow → platform balance).

    Called at session end to convert authorized holds into captured funds.
    The relay must capture before it can transfer winnings via Connect.
    Safe to call on already-captured PIs (Stripe is idempotent on capture).
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/capture",
            auth=(key, ""),
        )
        if resp.status_code == 400:
            data = resp.json()
            code = data.get("error", {}).get("code", "")
            if code in ("payment_intent_unexpected_state",):
                # Already captured or succeeded — treat as success
                return {"payment_intent_id": payment_intent_id, "status": "already_captured"}
        resp.raise_for_status()
        pi = resp.json()
        return {"payment_intent_id": payment_intent_id, "status": pi.get("status")}


async def cancel_payment_hold(payment_intent_id: str) -> dict[str, Any]:
    """Cancel an authorized hold, releasing funds back to the agent.

    Used when a session is abandoned (both agents disconnect, timeout) so neither
    agent is charged. Manual-capture PIs auto-expire in 5–7 days, but explicit
    cancel is faster and provides a clear paper trail.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}/cancel",
            auth=(key, ""),
        )
        if resp.status_code == 400:
            data = resp.json()
            code = data.get("error", {}).get("code", "")
            if code in ("payment_intent_unexpected_state",):
                return {"payment_intent_id": payment_intent_id, "status": "already_finalized"}
        resp.raise_for_status()
        pi = resp.json()
        return {"payment_intent_id": payment_intent_id, "status": pi.get("status")}


async def settle(
    session_id: str,
    winner_id: str,
    total_stake_cents: int,
    payment_intent_ids: list[str],
    winner_stripe_account: str | None = None,
) -> dict[str, Any]:
    """Capture all escrowed stakes, then transfer the winner's net payout.

    Escrow flow:
    1. All agent PIs were created with capture_method=manual (holds, not charges).
    2. On winner determination, relay captures all PIs → funds land in platform balance.
    3. Relay transfers 97.5% to winner's connected account; platform keeps 2.5%.
    4. If game was never played / abandoned, call cancel_payment_hold() instead.

    Platform fee: OPERATOR_FEE_BPS basis points (default 250 = 2.5%), minimum 1¢.
    Requires total_stake_cents ≥ STRIPE_MIN_CHARGE_CENTS (50¢) — enforced upstream.
    """
    fee = platform_fee(total_stake_cents)
    platform_fee_cents = fee
    winner_payout_cents = total_stake_cents - platform_fee_cents

    # Step 1: Capture all authorized holds → platform balance
    capture_results = []
    for pi_id in payment_intent_ids:
        if pi_id:
            try:
                result = await capture_payment_intent(pi_id)
                capture_results.append(result)
            except Exception as e:
                capture_results.append({"payment_intent_id": pi_id, "status": f"capture_failed: {e}"})

    transfer_id: str | None = None
    transfer_status = "pending_connect"

    # Step 2: Transfer winner's net share from platform balance to their connected account
    if winner_stripe_account and winner_payout_cents > 0:
        try:
            transfer = await payout_winner(
                winner_id=winner_id,
                stripe_account_id=winner_stripe_account,
                amount_cents=winner_payout_cents,
                session_id=session_id,
            )
            transfer_id = transfer["transfer_id"]
            transfer_status = "transferred"
        except Exception as e:
            transfer_status = f"transfer_failed: {e}"

    return {
        "session_id": session_id,
        "winner_id": winner_id,
        "total_stake_cents": total_stake_cents,
        "platform_fee_cents": platform_fee_cents,
        "winner_payout_cents": winner_payout_cents,
        "platform_fee_bps": OPERATOR_FEE_BPS,
        "payment_intent_ids": payment_intent_ids,
        "capture_results": capture_results,
        "transfer_id": transfer_id,
        "transfer_status": transfer_status,
        "status": "settled",
    }


async def create_connected_account(agent_id: str) -> dict[str, Any]:
    """Create a Stripe Custom connected account for an agent (test mode).

    In test mode Stripe skips verification — the account is immediately usable
    for receiving transfers. Stored per-agent so settlement can transfer the winner's
    share directly to their account rather than just recording a number.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/accounts",
            auth=(key, ""),
            data={
                "type": "custom",
                "country": "US",
                "email": f"{agent_id}@emporia.local",
                "capabilities[card_payments][requested]": "true",
                "capabilities[transfers][requested]": "true",
                "tos_acceptance[date]": str(int(__import__("time").time())),
                "tos_acceptance[ip]": "127.0.0.1",
                "metadata[agent_id]": agent_id,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        acct = resp.json()
        return {
            "stripe_account_id": acct["id"],
            "agent_id": agent_id,
            "status": "created",
        }


async def payout_winner(
    winner_id: str,
    stripe_account_id: str,
    amount_cents: int,
    session_id: str,
) -> dict[str, Any]:
    """Transfer winner payout to their Stripe Connected Account.

    Platform keeps OPERATOR_FEE_BPS; this function transfers only the winner's
    net share. Requires the platform balance to have sufficient funds — in test mode
    the confirmed PaymentIntents fund the platform balance automatically.
    """
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/transfers",
            auth=(key, ""),
            data={
                "amount": str(amount_cents),
                "currency": "usd",
                "destination": stripe_account_id,
                "transfer_group": session_id,
                "description": f"Emporia session payout: {session_id}",
                "metadata[winner_id]": winner_id,
                "metadata[session_id]": session_id,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        transfer = resp.json()
        return {
            "transfer_id": transfer["id"],
            "winner_id": winner_id,
            "stripe_account_id": stripe_account_id,
            "amount_cents": amount_cents,
            "status": "transferred",
        }


async def arbitrate_and_refund(
    session_id: str,
    charge_id: str,
    reason: str = "fraudulent",
) -> dict[str, Any]:
    """Issue a Stripe refund when cheat detection or signature verification fails."""
    key = _stripe_key()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}/refunds",
            auth=(key, ""),
            data={
                "charge": charge_id,
                "reason": reason,
                "metadata[session_id]": session_id,
                "metadata[protocol]": "emporia:v1",
            },
        )
        resp.raise_for_status()
        refund = resp.json()
        return {
            "session_id": session_id,
            "refund_id": refund["id"],
            "charge_id": charge_id,
            "status": refund.get("status"),
            "reason": reason,
        }


async def retry_with_stripe_auth(
    url: str,
    payload: dict[str, Any],
    payment_intent_id: str,
) -> dict[str, Any]:
    """Shared 402-retry utility: confirm intent, then retry request with auth header.

    Used by turn submission, lobby sync, peer discovery — one function, not duplicated.
    """
    confirmation = await confirm_stripe_intent(payment_intent_id)
    receipt = confirmation.get("receipt") or payment_intent_id
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {receipt}",
                "X-Payment": receipt,
            },
        )
        return {
            "ok": resp.is_success,
            "status_code": resp.status_code,
            "paid": True,
            "receipt": receipt,
            "response": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
        }
