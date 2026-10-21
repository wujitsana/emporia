"""A stake that was not captured is not paid out (P82).

`settle()` used to record `capture_failed` and transfer the winner's payout
anyway, from the platform's own balance. And `capture_payment_intent()` read
Stripe's `payment_intent_unexpected_state` as "already captured", though Stripe
returns it for a canceled PI too — which is what an expired hold becomes. Either
half alone paid a winner with money that was never collected.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from emporia import payments


def _resp(status_code, body):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    if status_code >= 400:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status_code}")
    else:
        r.raise_for_status = MagicMock()
    return r


def _unexpected_state(pi_status):
    return _resp(400, {"error": {"code": "payment_intent_unexpected_state",
                                 "payment_intent": {"id": "pi_x", "status": pi_status}}})


@pytest.fixture(autouse=True)
def _key():
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_capture"}):
        yield


def test_an_expired_hold_is_not_read_as_captured():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_unexpected_state("canceled")):
        with pytest.raises(RuntimeError, match="hold is gone"):
            anyio.run(payments.capture_payment_intent, "pi_x")


def test_an_already_captured_pi_is_still_a_success():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_unexpected_state("succeeded")):
        out = anyio.run(payments.capture_payment_intent, "pi_x")
    assert out["status"] == "already_captured"


def test_settle_does_not_pay_out_what_it_could_not_capture():
    transfers = []

    async def post(url, *a, **kw):
        if url.endswith("/capture"):
            return _unexpected_state("canceled")
        if url.endswith("/transfers"):
            transfers.append(kw)
            return _resp(200, {"id": "tr_x"})
        return _resp(200, {})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=post):
        out = anyio.run(lambda: payments.settle(
            "ses_x", "winner", 5000, ["pi_x"], winner_stripe_account="acct_w",
            winner_stripe_account_status="active", winner_stripe_account_mode="test",
        ))
    assert not transfers, "the winner was paid from the platform balance"
    assert out["transfer_status"] == "capture_failed"
    assert out["transfer_id"] is None


def test_the_stake_that_was_captured_goes_back_when_another_was_not():
    """P87: one player's lapsed hold used to leave the other player's captured
    stake on the platform balance, waiting for an operator to notice."""
    refunds, transfers = [], []

    async def post(url, *a, **kw):
        if url.endswith("/pi_lapsed/capture"):
            return _unexpected_state("canceled")
        if url.endswith("/capture"):
            return _resp(200, {"id": "pi_good", "status": "succeeded"})
        if url.endswith("/refunds"):
            refunds.append(kw)
            return _resp(200, {"id": "re_x", "status": "succeeded"})
        if url.endswith("/transfers"):
            transfers.append(kw)
        return _resp(200, {"id": "tr_x"})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=post):
        out = anyio.run(lambda: payments.settle(
            "ses_pair", "winner", 10000, ["pi_good", "pi_lapsed"],
            winner_stripe_account="acct_w", winner_stripe_account_status="active",
            winner_stripe_account_mode="test",
        ))
    assert not transfers
    assert [r["data"]["payment_intent"] for r in refunds] == ["pi_good"]
    assert refunds[0]["headers"]["Idempotency-Key"] == (
        "emporia:capture-failed-refund:ses_pair:pi_good"
    )
    assert out["refund_results"][0]["refund_id"] == "re_x"


def test_a_clean_settle_refunds_nothing():
    refunds = []

    async def post(url, *a, **kw):
        if url.endswith("/capture"):
            return _resp(200, {"id": "pi_ok", "status": "succeeded"})
        if url.endswith("/refunds"):
            refunds.append(kw)
        return _resp(200, {"id": "tr_x"})

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=post):
        out = anyio.run(lambda: payments.settle(
            "ses_ok", "winner", 5000, ["pi_ok"], winner_stripe_account="acct_w",
            winner_stripe_account_status="active", winner_stripe_account_mode="test",
        ))
    assert not refunds and out["refund_results"] == []
    assert out["transfer_status"] == "transferred"


# P93 -- a payout names the charge that funded it ------------------------------

def _stripe(transfers, *, latest_charge="ch_funding", get_fails=False):
    async def post(url, *a, **kw):
        if url.endswith("/capture"):
            return _resp(200, {"status": "succeeded"})
        if url.endswith("/transfers"):
            transfers.append(kw["data"])
        return _resp(200, {"id": "tr_x"})

    async def get(url, *a, **kw):
        if get_fails:
            raise RuntimeError("stripe unreachable")
        return _resp(200, {"id": url.rsplit("/", 1)[-1], "latest_charge": latest_charge})
    return post, get


def _settle(pis):
    return anyio.run(lambda: payments.settle(
        "ses_src", "winner", 5000, pis, winner_stripe_account="acct_w",
        winner_stripe_account_status="active", winner_stripe_account_mode="test"))


def test_a_single_stake_payout_draws_on_its_own_charge():
    """Without source_transaction a live-mode payout draws on the *available*
    balance and can fail while the stake it pays out is still pending."""
    transfers = []
    post, get = _stripe(transfers)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=post), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=get):
        out = _settle(["pi_one"])
    assert out["transfer_status"] == "transferred"
    assert transfers[0]["source_transaction"] == "ch_funding"


def test_several_stakes_cannot_name_one_source():
    transfers = []
    post, get = _stripe(transfers)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=post), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=get):
        _settle(["pi_one", "pi_two"])
    assert "source_transaction" not in transfers[0]


def test_an_unreadable_charge_never_blocks_the_payout():
    transfers = []
    post, get = _stripe(transfers, get_fails=True)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=post), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=get):
        out = _settle(["pi_one"])
    assert out["transfer_status"] == "transferred"
    assert "source_transaction" not in transfers[0]
