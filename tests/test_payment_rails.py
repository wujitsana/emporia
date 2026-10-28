"""Payment-rail details checked against the providers' own docs (P88).

Each test pins a fact about Stripe or x402 that the relay had wrong, so a
future edit that reintroduces it fails here rather than in production.
"""
import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from emporia import payments
from emporia.relay_server import app
from tests.test_emporia import _X402_ENV, _X402_EXTRA, _register, _signed_room


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216))


@pytest.fixture(autouse=True)
def _key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_rails"}):
        yield


def _resp(body):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = body
    r.raise_for_status = MagicMock()
    return r


# -- Stripe shared payment tokens --------------------------------------------

def _granted_token(max_amount):
    return _resp({"id": "spt_x", "deactivated_at": None,
                  "usage_limits": {"currency": "usd", "max_amount": max_amount,
                                   "expires_at": 1798761600}})


def test_spt_max_amount_is_already_in_cents():
    """Stripe's docs grant `usage_limits[max_amount]=1000` for a $10.00 charge.
    The relay multiplied by 100, so a $10 token passed for a $1,000 charge."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               return_value=_granted_token(1000)):
        spt = asyncio.run(payments.retrieve_spt("spt_x"))
    assert spt["max_amount_cents"] == 1000


def test_a_token_too_small_for_the_charge_is_refused_before_stripe_is_asked():
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               return_value=_granted_token(1000)), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        with pytest.raises(ValueError, match="max_amount too small"):
            asyncio.run(payments.confirm_spt("spt_x", 5000, "room_1", "payer"))
    post.assert_not_called()


def _confirm(resource_id, **kw):
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
               return_value=_granted_token(100000)), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_resp({"id": "pi_x", "status": "requires_capture"})) as post:
        asyncio.run(payments.confirm_spt("spt_x", 500, resource_id, "payer", **kw))
    return post.call_args.kwargs["headers"]["Idempotency-Key"]


def test_an_spt_charge_carries_a_stable_idempotency_key():
    """An SPT is consumed by its first PaymentIntent. Without a key, a timeout
    after Stripe charged left the payer charged and the relay with no record."""
    assert _confirm("room_1") == _confirm("room_1")
    assert _confirm("room_1") != _confirm("room_2"), "one key across two resources"
    assert _confirm("room_1") != _confirm("room_1", capture_method="automatic")


# -- x402 ---------------------------------------------------------------------

@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_an_evm_address_matches_in_any_letter_case(mock_verify, mock_settle, client):
    """Mixed case in an EVM address is only the EIP-55 checksum. A payer whose
    client checksummed `payTo` was refused for an exactly-right payment."""
    from emporia.relay_server import AGENT_REGISTRY, _verify_x402_payment

    creator = "p88_case_creator"
    _register(client, creator)
    wallet = "0x52908400098527886e0f7030069857d2e4169ee7"
    AGENT_REGISTRY.set_x402_pay_to(creator, "evm", wallet)
    mock_verify.return_value = {"isValid": True}
    mock_settle.return_value = {"success": True, "transaction": "0xtx_case"}

    with patch.dict(os.environ, _X402_ENV, clear=False):
        ref = asyncio.run(_verify_x402_payment(
            {"accepted": {
                "scheme": "exact", "network": "eip155:8453", "amount": "2500000",
                "asset": "0xusdcfake", "payTo": "0x52908400098527886E0F7030069857D2E4169EE7",
                "maxTimeoutSeconds": 300, "extra": _X402_EXTRA,
            }, "payload": {"authorization": {"from": "0xpayer", "nonce": "0x01",
                                             "validBefore": "9999999999"}}},
            creator_id=creator, amount_cents=250, currency="usd",
            challenge_resource="emporia:room:case", payer_id="p88_case_payer",
            resource_type="room", resource_id="case_room",
        ))
    assert ref.reference == "0xtx_case"


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_case_folding_does_not_let_a_different_address_through(mock_verify, mock_settle, client):
    from emporia.relay_server import AGENT_REGISTRY, _verify_x402_payment

    creator = "p88_case_creator_2"
    _register(client, creator)
    AGENT_REGISTRY.set_x402_pay_to(creator, "evm", "0x" + "aa" * 20)
    with patch.dict(os.environ, _X402_ENV, clear=False):
        with pytest.raises(HTTPException) as e:
            asyncio.run(_verify_x402_payment(
                {"accepted": {
                    "scheme": "exact", "network": "eip155:8453", "amount": "2500000",
                    "asset": "0xUSDCFAKE", "payTo": "0x" + "AB" * 20,
                    "maxTimeoutSeconds": 300, "extra": _X402_EXTRA,
                }, "payload": {}},
                creator_id=creator, amount_cents=250, currency="usd",
                challenge_resource="emporia:room:case2", payer_id="p88_case_payer_2",
                resource_type="room", resource_id="case_room_2",
            ))
    assert e.value.status_code == 402 and "payTo" in str(e.value.detail)
    mock_verify.assert_not_called()


# -- x402 PAYMENT-RESPONSE (P93) ------------------------------------------------

def _x402_room(client, owner, fee=400):
    from tests.test_emporia import _register_keyed as reg
    reg(client, owner)
    r = client.post(f"/agents/{owner}/x402-wallet",
                    json={"network_family": "evm", "address": "0x" + "dd" * 20},
                    headers={"x-emporia-agent-id": owner})
    assert r.status_code == 200, r.text
    r = client.post("/rooms", json=_signed_room(owner, {"name": "P93", "creator_id": owner, "room_type": "private",
                                    "gate_type": "paid", "entry_fee_cents": fee}),
                    headers={"x-emporia-agent-id": owner})
    return r.json()["room_id"]


def _x402_join(client, room_id, payer):
    from tests.test_emporia import _register as reg
    from tests.test_emporia import _x402_payment_signature_header
    reg(client, payer)
    header = _x402_payment_signature_header({
        "scheme": "exact", "network": "eip155:8453", "amount": "4000000",
        "asset": "0xUSDCFAKE", "payTo": "0x" + "dd" * 20, "maxTimeoutSeconds": 300,
        "extra": _X402_EXTRA,
    })
    with patch.dict(os.environ, _X402_ENV, clear=False):
        return client.post(f"/rooms/{room_id}/join", json={"agent_id": payer},
                           headers={"x-emporia-agent-id": payer, "PAYMENT-SIGNATURE": header})


def _receipt(resp):
    import base64
    raw = resp.headers.get("PAYMENT-RESPONSE")
    assert raw, f"no PAYMENT-RESPONSE on a {resp.status_code}"
    return json.loads(base64.b64decode(raw))


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_a_settled_x402_payment_returns_its_receipt(mock_verify, mock_settle, client):
    """x402 v2 returns the settlement to the payer in PAYMENT-RESPONSE. CORS
    exposed the header since P1b; nothing ever set it."""
    mock_verify.return_value = {"isValid": True}
    mock_settle.return_value = {"success": True, "transaction": "0xp93_tx",
                                "network": "eip155:8453", "payer": "0x" + "cc" * 20}
    resp = _x402_join(client, _x402_room(client, "p93_owner"), "p93_payer")
    assert resp.status_code == 200, resp.text
    assert _receipt(resp) == {"success": True, "transaction": "0xp93_tx",
                              "network": "eip155:8453", "payer": "0x" + "cc" * 20}


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_a_failed_settlement_returns_its_reason(mock_verify, mock_settle, client):
    mock_verify.return_value = {"isValid": True}
    mock_settle.return_value = {"success": False, "errorReason": "insufficient_funds"}
    resp = _x402_join(client, _x402_room(client, "p93_owner_f"), "p93_payer_f")
    assert resp.status_code == 402
    receipt = _receipt(resp)
    assert receipt["success"] is False and receipt["errorReason"] == "insufficient_funds"
    assert receipt["network"] == "eip155:8453" and receipt["transaction"] == ""


def test_no_receipt_where_nothing_settled(client):
    assert "PAYMENT-RESPONSE" not in client.get("/health").headers
