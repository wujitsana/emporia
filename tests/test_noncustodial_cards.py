"""P157: no card money waits with the relay.

Every card payment is a Stripe destination charge to the person it is for: a
room's creator, a purchase's seller, a wager's opponent. Card flows that would
have to hold money on the operator's balance — a held prize, a wager of three
or more — are refused unless the operator opts in (EMPORIA_CUSTODIAL_CARDS).
The fake Stripe records what the relay asked for, including each charge's
destination and fee, so these tests say where money would actually go.
"""
import asyncio
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from emporia import relay_server as rs
from tests.test_emporia import _register_keyed
from tests.test_stake_models import FakeStripe, _resp, client, stripe  # noqa: F401  (fixtures)
from tests.test_x402_wager import _play

CARD_WAGER = {"mode": "stripe_link", "stake_per_participant": "5.00", "currency": "usd"}
PAY = {"Authorization": "Payment spt_noncustodial_test"}


@pytest.fixture(autouse=True)
def _default_relay(monkeypatch):
    monkeypatch.delenv("EMPORIA_CUSTODIAL_CARDS", raising=False)
    # The card-wager tests below are for an operator Stripe approved for skill
    # games (P159); the default refuses card money on games — see the end.
    monkeypatch.setenv("EMPORIA_CARD_GAME_STAKES", "1")


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
    r = client.post("/rooms", json={"name": "NC room", "creator_id": owner, "room_type": "private",
                                    "gate_type": "paid", "entry_fee_cents": fee}, headers=_hdr(owner))
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
    assert pi["application_fee_amount"] == 2, "the fee is the only part the platform receives"
    assert stripe_entries.transfers == [], "no transfer: nothing sat on the platform's balance"
    [row] = _settlement_rows(room)
    assert (row["transfer_status"], row["platform_fee_cents"], row["winner_payout_cents"]) == ("destination", 2, 98)


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

def _card_wager(client, accounts=True):
    a, b = _agents(client, 2, "cw")
    with _accounts(**({a: "acct_a", b: "acct_b"} if accounts else {})):
        r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a,
                                           "payment_rules": CARD_WAGER}, headers=_hdr(a))
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        j = client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b))
        assert j.status_code == 200, j.text
    return sid, a, b


def test_each_card_stake_is_a_hold_paid_to_the_opponent(client, stripe):
    sid, a, b = _card_wager(client)
    assert stripe.pis == {}, "nothing is charged at create or join"
    assert rs.load_session(sid)["status"] == "awaiting_funding"
    with _accounts(**{a: "acct_a", b: "acct_b"}):
        assert client.post(f"/sessions/{sid}/fund", headers={**_hdr(a), **PAY}).status_code == 200
        again = client.post(f"/sessions/{sid}/fund", headers={**_hdr(a), **PAY})
        assert again.status_code == 409, "staked once"
        assert rs.load_session(sid)["status"] == "awaiting_funding"
        assert client.post(f"/sessions/{sid}/fund", headers={**_hdr(b), **PAY}).status_code == 200
        assert rs.load_session(sid)["status"] == "active"
        holds = {pi["metadata"]["buyer_agent_id"]: (pid, pi) for pid, pi in stripe.pis.items()}
        assert holds[a][1]["transfer_data"] == {"destination": "acct_b"}, "a's stake pays b if a loses"
        assert holds[b][1]["transfer_data"] == {"destination": "acct_a"}
        assert holds[a][1]["capture_method"] == "manual"
        assert holds[a][1]["application_fee_amount"] == 25, "2.5% of the $10 pot, on the one captured"
        r = _play(client, sid, a, b, [30, 30, 30, 5, 5], [20] * 5)
    assert r.json()["outcome"]["winner"] == a
    assert stripe.captured == [holds[b][0]], "b's hold, paid to a by Stripe"
    assert stripe.cancelled == [holds[a][0]], "a's own hold, cancelled"
    assert stripe.transfers == [], "the relay moved nothing itself"
    [row] = _settlement_rows(sid)
    assert (row["rail"], row["winner_id"], row["transfer_status"]) == ("stripe", a, "destination")
    assert (row["total_stake_cents"], row["platform_fee_cents"], row["winner_payout_cents"]) == (1000, 25, 975)


def test_a_card_wager_draw_cancels_both(client, stripe):
    sid, a, b = _card_wager(client)
    with _accounts(**{a: "acct_a", b: "acct_b"}):
        for who in (a, b):
            client.post(f"/sessions/{sid}/fund", headers={**_hdr(who), **PAY})
        r = _play(client, sid, a, b, [20] * 5, [20] * 5)
    assert r.json()["outcome"]["outcome_type"] == "draw"
    assert not stripe.captured and len(stripe.cancelled) == 2


def test_a_card_wager_needs_two_players_who_can_be_paid(client, stripe):
    a, nobody = _agents(client, 2, "cx")
    with _accounts(**{a: "acct_a"}):
        r = client.post("/sessions", json={"module_type": "emporia:two-thirds:v1", "creator_agent_id": a,
                                           "config": {"players": 3}, "payment_rules": CARD_WAGER},
                        headers=_hdr(a))
        assert r.status_code == 400 and "two players" in r.text, "a pot of three would sit with the relay"
        r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": nobody,
                                           "payment_rules": CARD_WAGER}, headers=_hdr(nobody))
        assert r.status_code == 409 and "Stripe account" in r.text


def test_the_opt_in_brings_back_the_held_pot(client, stripe, monkeypatch):
    monkeypatch.setenv("EMPORIA_CUSTODIAL_CARDS", "1")
    a, = _agents(client, 1, "co")
    r = client.post("/sessions", json={"module_type": "emporia:two-thirds:v1", "creator_agent_id": a,
                                       "config": {"players": 3}, "payment_rules": CARD_WAGER},
                    headers={**_hdr(a), **PAY})
    assert r.status_code == 200, r.text
    [pi] = stripe.pis.values()
    assert pi["transfer_data"] is None, "a platform charge, as before"


def test_a_card_intent_for_a_wager_names_the_opponent(client, stripe):
    sid, a, b = _card_wager(client)
    fake_post = stripe.post

    async def post(url, *args, **kw):    # a created intent carries its client secret
        r = await fake_post(url, *args, **kw)
        r.json.return_value.setdefault("client_secret", "pi_secret_x")
        return r

    with _accounts(**{a: "acct_a", b: "acct_b"}), patch("httpx.AsyncClient.post", side_effect=post):
        r = client.post("/payments/create-intent",
                        json={"session_id": sid, "amount_cents": 500, "seller_id": "relay"}, headers=_hdr(a))
    assert r.status_code == 200, r.text
    pi = stripe.pis[r.json()["payment_intent_id"]]
    assert pi["transfer_data"] == {"destination": "acct_b"} and pi["application_fee_amount"] == 25


# -- bounties -----------------------------------------------------------------

def test_a_held_card_prize_is_refused(client, stripe):
    funder, = _agents(client, 1, "bf")
    deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    r = client.post("/bounties", json={"title": "Prize", "description": "d", "prize_cents": 1000,
                                       "module_type": "emporia:blotto:v1", "rail": "stripe",
                                       "entry_deadline": deadline},
                    headers={**_hdr(funder), **PAY})
    assert r.status_code == 400 and "does not hold card prizes" in r.text
    assert stripe.pis == {}


# -- P158: a staked game cannot be stalled out of ------------------------------

def test_a_staked_game_gets_a_clock_so_stalling_loses(client, stripe, monkeypatch):
    """Without a clock, the losing player could stop moving until the idle
    sweep abandoned the game and gave every stake back."""
    monkeypatch.setenv("EMPORIA_ENGINE_GAME_STAKES", "1")
    a, = _agents(client, 1, "ck")
    with _accounts(**{a: "acct_a"}):
        r = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": a,
                                           "payment_rules": CARD_WAGER}, headers=_hdr(a))
        assert r.status_code == 200, r.text
        assert rs.load_session(r.json()["session_id"])["config"]["move_time_limit_s"] == 3600
        free = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": a},
                           headers=_hdr(a))
    assert "move_time_limit_s" not in rs.load_session(free.json()["session_id"])["config"], \
        "a free game keeps no clock unless asked"


def test_funding_locks_do_not_outlive_the_funding(client, stripe):
    from emporia.routes import sessions as routes
    sid, a, b = _card_wager(client)
    with _accounts(**{a: "acct_a", b: "acct_b"}):
        for who in (a, b):
            client.post(f"/sessions/{sid}/fund", headers={**_hdr(who), **PAY})
    assert rs.load_session(sid)["status"] == "active" and sid not in routes._WAGER_FUNDING_LOCKS


# -- P159: by default, no card money on games ---------------------------------

def test_by_default_games_are_staked_in_usdc_not_on_cards(client, stripe, monkeypatch):
    monkeypatch.delenv("EMPORIA_CARD_GAME_STAKES")
    a, = _agents(client, 1, "dg")
    with _accounts(**{a: "acct_a"}):
        r = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a,
                                           "payment_rules": CARD_WAGER}, headers={**_hdr(a), **PAY})
        assert r.status_code == 400 and "payment_mode='x402'" in r.text
        deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        r = client.post("/bounties", json={"title": "Prize", "description": "d", "prize_cents": 1000,
                                           "module_type": "emporia:blotto:v1", "rail": "stripe",
                                           "entry_deadline": deadline}, headers={**_hdr(a), **PAY})
        assert r.status_code == 400 and "promise" in r.text
        free = client.post("/sessions", json={"module_type": "emporia:blotto:v1", "creator_agent_id": a},
                           headers=_hdr(a))
    assert free.status_code == 200, "free play is untouched"
    assert stripe.pis == {}
