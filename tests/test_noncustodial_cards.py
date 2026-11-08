"""P157: no card money waits with the relay.

Every card payment is a Stripe destination charge to the person it is for: a
room's creator, a purchase's seller. Card flows that would
have to hold money on the operator's balance are refused unless the operator opts in (EMPORIA_CUSTODIAL_CARDS).
The fake Stripe records what the relay asked for, including each charge's
destination and fee, so these tests say where money would actually go.
"""
import asyncio
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from emporia import relay_server as rs
from tests.test_emporia import _register_keyed, _signed_room
from tests.test_price_models import FakeStripe, _resp

CARD_STAKE = {"mode": "stripe_link", "price": "5.00", "currency": "usd"}
PAY = {"Authorization": "Payment spt_noncustodial_test"}


@pytest.fixture(autouse=True)
def _default_relay(monkeypatch):
    monkeypatch.delenv("EMPORIA_CUSTODIAL_CARDS", raising=False)


@pytest.fixture
def stripe_entries(stripe):
    """The stake-models fake, plus what an entry needs: a charge captured at
    once (automatic capture succeeds), and refunds recorded."""
    fake_post = FakeStripe.post
    stripe.refunds = []

    async def post(url, *a, **kw):
        data = kw.get("data") or {}
        if url.rstrip("/").endswith("/refunds"):
            stripe.refunds.append(data)
            return _resp({"id": f"re_{len(stripe.refunds)}", "status": "succeeded"})
        r = await fake_post(stripe, url, *a, **kw)
        body = r.json.return_value
        if url.rstrip("/").endswith("/payment_intents") and body.get("capture_method") == "automatic":
            body["status"] = "succeeded"
        return r

    with patch("httpx.AsyncClient.post", side_effect=post):
        yield stripe


@contextmanager
def _accounts(**by_agent):
    """These agents have finished Stripe onboarding; nobody else has."""
    with patch.object(rs.AGENT_REGISTRY, "get_stripe_account", side_effect=lambda a: by_agent.get(a)), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_status",
                      side_effect=lambda a: "active" if a in by_agent else "none"), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_mode", side_effect=lambda a: "test"):
        yield


def _hdr(a):
    return {"x-emporia-agent-id": a}


def _agents(client, n, prefix="nc"):
    names = [f"{prefix}{i}_{uuid.uuid4().hex[:6]}" for i in range(n)]
    for a in names:
        _register_keyed(client, a)
    return names


def _paid_room(client, owner, fee=100):
    r = client.post("/rooms", json=_signed_room(owner, {"name": "NC room", "creator_id": owner, "room_type": "private",
                                    "gate_type": "paid", "entry_fee_cents": fee}), headers=_hdr(owner))
    assert r.status_code == 200, r.text
    return r.json()["room_id"]


def _settlement_rows(resource_id):
    with rs.DB_LOCK, rs.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM settlements WHERE session_id = ?", (resource_id,)).fetchall()]


# -- entries ------------------------------------------------------------------

def test_a_card_entry_pays_the_creator_directly(client, stripe_entries):
    owner, payer = _agents(client, 2)
    room = _paid_room(client, owner)
    with _accounts(**{owner: "acct_owner"}):
        r = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=PAY)
    assert r.status_code == 200, r.text
    [pi] = stripe_entries.pis.values()
    assert pi["transfer_data"] == {"destination": "acct_owner"}, "charged to the creator, not the platform"
    assert pi["application_fee_amount"] == 1, "the fee is the only part the platform receives"
    assert stripe_entries.transfers == [], "no transfer: nothing sat on the platform's balance"
    [row] = _settlement_rows(room)
    assert (row["transfer_status"], row["platform_fee_cents"], row["winner_payout_cents"]) == ("destination", 1, 99)


def test_no_card_is_offered_for_a_creator_stripe_cannot_pay(client, stripe_entries):
    owner, payer = _agents(client, 2)
    room = _paid_room(client, owner)
    with _accounts():
        challenge = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=_hdr(payer))
        assert challenge.status_code == 402
        assert challenge.json()["card"]["offered"] is False
        assert "www-authenticate" not in {k.lower() for k in challenge.headers}, "no card challenge"
        declined = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=PAY)
    assert declined.status_code == 402 and declined.json()["declined"]["rail"] == "stripe"
    assert stripe_entries.pis == {}, "nothing was charged"


def test_an_agora_entry_pays_the_creator_directly(client, stripe_entries):
    owner, member = _agents(client, 2)
    r = client.post("/agoras/topics", json={"name": "NC topic", "description": "d", "visibility": "private",
                                            "gate_type": "paid_invite", "entry_fee_cents": 200,
                                            "creator_id": owner}, headers=_hdr(owner))
    slug = r.json()["slug"]
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": member}, headers=_hdr(owner))
    with _accounts(**{owner: "acct_topic"}):
        r = client.post(f"/agoras/topics/{slug}/subscribe", json={"agent_id": member}, headers=PAY)
    assert r.status_code == 200, r.text
    [pi] = stripe_entries.pis.values()
    assert pi["transfer_data"] == {"destination": "acct_topic"} and not stripe_entries.transfers


def test_a_kick_with_refund_takes_the_money_back_from_the_creator(client, stripe_entries):
    owner, payer = _agents(client, 2)
    room = _paid_room(client, owner)
    with _accounts(**{owner: "acct_owner"}):
        assert client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=PAY).status_code == 200
        out = asyncio.run(rs._refund_kicked_room_payer(room, payer))
    assert out["refunded"] is True
    [refund] = stripe_entries.refunds
    assert refund["reverse_transfer"] == "true" and refund["refund_application_fee"] == "true", \
        "a destination charge's money is with the creator; the refund pulls it back from them"


def test_an_interrupted_entry_is_finished_from_what_stripe_says(client, stripe_entries):
    """A crash between membership and settlement: the retry reads the charge's
    destination from Stripe rather than guess — guessing 'held' would pay twice."""
    owner, payer = _agents(client, 2)
    room = _paid_room(client, owner)
    with _accounts(**{owner: "acct_owner"}), \
         patch.object(rs, "_settle_access_payment", side_effect=RuntimeError("crash")):
        with pytest.raises(RuntimeError):
            client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=PAY)
    assert _settlement_rows(room) == []
    with _accounts(**{owner: "acct_owner"}):
        r = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=_hdr(payer))
    assert r.status_code == 200, r.text
    [row] = _settlement_rows(room)
    assert row["transfer_status"] == "destination" and not stripe_entries.transfers


# -- wagers -------------------------------------------------------------------

def test_a_card_intent_for_a_session_is_refused_on_a_relay_that_holds_no_card_money(client, stripe):
    a, b = _agents(client, 2, "ci")
    with _accounts(**{a: "acct_a", b: "acct_b"}):
        r = client.post("/sessions", json={"module_type": "emporia:service:v1", "creator_agent_id": a,
                                           "config": {"description": "A haiku"}, "payment_rules": CARD_STAKE},
                        headers=_hdr(a))
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        r = client.post("/payments/create-intent",
                        json={"session_id": sid, "amount_cents": 500, "seller_id": "relay"}, headers=_hdr(a))
    assert r.status_code == 409 and "fund_session" in r.text
    assert stripe.pis == {}


# -- P159: no card money rides on a game --------------------------------------

def test_by_default_no_card_money_rides_on_a_game(client, stripe, monkeypatch):
    a, = _agents(client, 1, "dg")
    with _accounts(**{a: "acct_a"}):
        r = client.post("/sessions", json={"module_type": "emporia:blotto:v2", "creator_agent_id": a,
                                           "payment_rules": CARD_STAKE}, headers={**_hdr(a), **PAY})
        assert r.status_code == 400 and "takes no stakes" in r.text
        free = client.post("/sessions", json={"module_type": "emporia:blotto:v2", "creator_agent_id": a},
                           headers=_hdr(a))
    assert free.status_code == 200, "free play is untouched"
    assert stripe.pis == {}


@pytest.fixture(autouse=True)
def _relay_fee_address(monkeypatch):
    """The relay's fee exists once it has a fee address (fees.py): these tests are about where the 1% goes."""
    monkeypatch.setenv("EMPORIA_FEE_ADDRESS", "0x" + "fe" * 20)
