"""Who stakes, and who gets paid (P95).

The session routes applied one rule to every module: the creator joins free and
the joiner pays. That was wrong both ways it could be. A wager's creator risked
nothing. A service session's joiner is the *seller*, so the seller was charged
while the buyer paid nothing — and "dispute → buyer refunded" refunded money
the buyer never put up. Each module now declares its STAKE_MODEL.

The fake Stripe below records what the relay asked Stripe to do, including a
charge's destination, so the tests can say where money would actually go.
"""
import os
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register, _register_keyed, _signed_action_body

PAY = {"Authorization": "Payment spt_stake_models_test"}


class FakeStripe:
    def __init__(self):
        self.pis: dict[str, dict] = {}
        self.captured: list[str] = []
        self.cancelled: list[str] = []
        self.transfers: list[dict] = []

    async def post(self, url, *a, **kw):
        data = kw.get("data") or {}
        if url.rstrip("/").endswith("/payment_intents"):
            pi = f"pi_sm_{uuid.uuid4().hex[:10]}"
            self.pis[pi] = {
                "id": pi, "status": "requires_capture",
                "amount": int(data.get("amount", 0)), "currency": data.get("currency", "usd"),
                "capture_method": data.get("capture_method", "automatic"),
                "metadata": {k[9:-1]: v for k, v in data.items() if k.startswith("metadata[")},
                "transfer_data": ({"destination": data["transfer_data[destination]"]}
                                  if "transfer_data[destination]" in data else None),
                "application_fee_amount": (int(data["application_fee_amount"])
                                           if "application_fee_amount" in data else None),
                "latest_charge": f"ch_{pi[3:]}",
            }
            return _resp(self.pis[pi])
        if url.endswith("/capture"):
            pi = url.split("/payment_intents/")[1].split("/")[0]
            self.captured.append(pi)
            return _resp({**self.pis.get(pi, {"id": pi}), "status": "succeeded"})
        if url.endswith("/cancel"):
            pi = url.split("/payment_intents/")[1].split("/")[0]
            self.cancelled.append(pi)
            return _resp({**self.pis.get(pi, {"id": pi}), "status": "canceled"})
        if url.endswith("/transfers"):
            self.transfers.append(data)
            return _resp({"id": f"tr_{len(self.transfers)}", "status": "succeeded"})
        return _resp({"id": "obj_x"})

    async def get(self, url, *a, **kw):
        if "/shared_payment/granted_tokens/" in url:
            return _resp({"id": "spt_x", "deactivated_at": None, "usage_limits": {}})
        pi = url.rsplit("/payment_intents/", 1)[-1].split("?")[0]
        return _resp(self.pis.get(pi, {"id": pi, "status": "requires_payment_method"}))


def _resp(body):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = body
    r.raise_for_status = MagicMock()
    return r


@pytest.fixture
def stripe():
    fake = FakeStripe()
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_stake_models"}), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=fake.post), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=fake.get):
        yield fake


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


@contextmanager
def _connect_account(agent, account="acct_seller_sm"):
    """The seller has finished Stripe onboarding (a live connected account)."""
    from emporia import relay_server as rs
    real = (rs.AGENT_REGISTRY.get_stripe_account, rs.AGENT_REGISTRY.get_stripe_account_status,
            rs.AGENT_REGISTRY.get_stripe_account_mode)
    with patch.object(rs.AGENT_REGISTRY, "get_stripe_account",
                      side_effect=lambda a: account if a == agent else real[0](a)), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_status",
                      side_effect=lambda a: "active" if a == agent else real[1](a)), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_mode",
                      side_effect=lambda a: "test" if a == agent else real[2](a)):
        yield account


def _settlement(session_id):
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        return dict(conn.execute("SELECT * FROM settlements WHERE session_id = ?",
                                 (session_id,)).fetchone())


def _act(client, sid, who, action, payload=None):
    body = _signed_action_body(client, sid, who, action, payload or {},
                               "Taking this step in the session as agreed")
    r = client.post(f"/sessions/{sid}/action", json=body)
    assert r.status_code == 200, r.text
    return r.json()


STAKE = {"mode": "stripe_link", "stake_per_participant": "10.00", "currency": "usd"}


# -- wagers: every participant stakes -----------------------------------------

def test_a_wagers_creator_stakes_or_no_session_exists(client, stripe):
    creator = "w_" + uuid.uuid4().hex[:8]
    _register(client, creator)
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1",
                                       "creator_agent_id": creator, "payment_rules": STAKE},
                    headers=_hdr(creator))
    assert r.status_code == 402, "the creator of a wager used to risk nothing"
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        joined = conn.execute("SELECT COUNT(*) FROM participants WHERE agent_id = ?",
                              (creator,)).fetchone()[0]
    assert joined == 0, "a refused wager must leave no session behind"


def test_both_sides_of_a_wager_are_held_and_the_winner_takes_both(client, stripe):
    white, black = "ww_" + uuid.uuid4().hex[:8], "wb_" + uuid.uuid4().hex[:8]
    _register_keyed(client, white)
    _register_keyed(client, black)
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1",
                                       "creator_agent_id": white, "payment_rules": STAKE},
                    headers={**_hdr(white), **PAY})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": black},
                       headers={**_hdr(black), **PAY}).status_code == 200
    assert len(stripe.pis) == 2

    from emporia import relay_server as rs
    with patch.object(rs.AGENT_REGISTRY, "get_stripe_account", return_value="acct_black"), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_status", return_value="active"), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_mode", return_value="test"):
        for who, uci in (("w", "f2f3"), ("b", "e7e5"), ("w", "g2g4"), ("b", "d8h4")):
            _act(client, sid, white if who == "w" else black, "move", {"uci": uci})

    assert sorted(stripe.captured) == sorted(stripe.pis)
    [transfer] = stripe.transfers
    assert transfer["destination"] == "acct_black"
    assert int(transfer["amount"]) == 2000 - 50, "both stakes, less the 2.5% fee"


def test_a_module_with_no_payee_takes_no_stake(client, stripe):
    creator = "r_" + uuid.uuid4().hex[:8]
    _register(client, creator)
    r = client.post("/sessions", json={"module_type": "emporia:research:v1",
                                       "creator_agent_id": creator, "payment_rules": STAKE},
                    headers={**_hdr(creator), **PAY})
    assert r.status_code == 400 and "emporia:service:v1" in r.json()["detail"]
    assert not stripe.pis


# -- purchases: the buyer pays the seller, directly ---------------------------

def _purchase(client):
    buyer, seller = "pb_" + uuid.uuid4().hex[:8], "ps_" + uuid.uuid4().hex[:8]
    _register_keyed(client, buyer)
    _register_keyed(client, seller)
    sid = client.post("/sessions", json={
        "module_type": "emporia:service:v1", "creator_agent_id": buyer,
        "config": {"description": "A haiku"}, "payment_rules": STAKE,
    }, headers=_hdr(buyer)).json()["session_id"]
    return sid, buyer, seller


def test_the_seller_is_not_charged_to_take_the_job(client, stripe):
    """The bug at the root: the join gate charged whoever joined, and the
    joiner of a service session is the seller."""
    sid, buyer, seller = _purchase(client)
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_funding"
    assert not stripe.pis, "the seller paid to work"


def test_nothing_is_played_until_the_buyer_funds_it(client, stripe):
    sid, buyer, seller = _purchase(client)
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    body = _signed_action_body(client, sid, seller, "accept", {}, "Accepting the commission")
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 400


def test_the_buyers_hold_pays_the_seller_directly(client, stripe):
    sid, buyer, seller = _purchase(client)
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    with _connect_account(seller) as account:
        r = client.post(f"/sessions/{sid}/fund", headers={**_hdr(buyer), **PAY})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"
        [pi] = stripe.pis.values()
        assert pi["transfer_data"] == {"destination": account}
        assert pi["application_fee_amount"] == 25
        assert pi["metadata"]["buyer_agent_id"] == buyer

        _act(client, sid, seller, "accept")
        _act(client, sid, seller, "deliver", {"deliverable": "Packets hum / along the wire"})
        _act(client, sid, buyer, "confirm")

    assert stripe.captured == [pi["id"]]
    assert not stripe.transfers, "capture *is* the payout for a destination charge"
    row = _settlement(sid)
    assert row["winner_id"] == seller and row["transfer_status"] == "destination"
    assert row["winner_payout_cents"] == 975


def test_a_dispute_releases_the_buyers_own_money(client, stripe):
    sid, buyer, seller = _purchase(client)
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    with _connect_account(seller):
        client.post(f"/sessions/{sid}/fund", headers={**_hdr(buyer), **PAY})
        _act(client, sid, seller, "accept")
        _act(client, sid, seller, "deliver", {"deliverable": "Not a haiku"})
        _act(client, sid, buyer, "dispute", {"reason": "not 5-7-5"})
    [pi] = stripe.pis
    assert stripe.cancelled == [pi] and not stripe.captured
    assert _settlement(sid)["status"] == "refunded"


def test_only_the_buyer_funds_and_only_a_payable_seller(client, stripe):
    sid, buyer, seller = _purchase(client)
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    with _connect_account(seller):
        assert client.post(f"/sessions/{sid}/fund",
                           headers={**_hdr(seller), **PAY}).status_code == 403
    from emporia import relay_server as rs
    with patch.object(rs.AGENT_REGISTRY, "get_stripe_account", return_value=None):
        r = client.post(f"/sessions/{sid}/fund", headers={**_hdr(buyer), **PAY})
    assert r.status_code == 409 and "cannot receive payments" in r.json()["detail"]
    assert not stripe.pis


def test_funding_without_a_credential_is_a_402(client, stripe):
    sid, buyer, seller = _purchase(client)
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    with _connect_account(seller):
        r = client.post(f"/sessions/{sid}/fund", headers=_hdr(buyer))
    assert r.status_code == 402
    assert "x402" not in r.json(), "a purchase hold cannot be carried by x402"


# -- paying and proving who you are, on a relay that trusts no header ---------

@pytest.fixture
def prod():
    from emporia import relay_server
    with patch.object(relay_server, "_HEADER_AUTH_TRUSTED_HOSTS", frozenset()):
        yield TestClient(app, client=("127.0.0.1", 51216))


def _jwt(client, agent):
    from tests.test_emporia import bearer_for
    return bearer_for(client, agent)["Authorization"]


def test_a_payment_no_longer_hides_who_paid(prod, stripe):
    """`Authorization: Payment <spt>` displaced the identity bearer, so a remote
    agent paying with an SPT could not say who it was. Every route that must
    know the payer 401'd for any agent not co-located with the relay."""
    creator = "pp_" + uuid.uuid4().hex[:8]
    _register_keyed(prod, creator)
    body = {"module_type": "emporia:chess:v1", "creator_agent_id": creator,
            "payment_rules": STAKE}

    alone = prod.post("/sessions", json=body, headers=PAY)
    assert alone.status_code == 401, "a payment alone must still not name a caller"

    both = prod.post("/sessions", json=body,
                     headers={**PAY, "X-Emporia-Authorization": _jwt(prod, creator)})
    assert both.status_code == 200, both.text
    [pi] = stripe.pis.values()
    assert pi["metadata"]["buyer_agent_id"] == creator


def test_a_paying_joiner_is_held_to_the_identity_it_proves(prod, stripe):
    """With the second header the join is authenticated: naming someone else as
    the joiner is refused, where before the name was simply taken on trust."""
    creator, joiner, victim = ("pj_" + uuid.uuid4().hex[:8] for _ in range(3))
    for a in (creator, joiner, victim):
        _register_keyed(prod, a)
    sid = prod.post("/sessions", json={"module_type": "emporia:chess:v1",
                                       "creator_agent_id": creator, "payment_rules": STAKE},
                    headers={**PAY, "X-Emporia-Authorization": _jwt(prod, creator)}
                    ).json()["session_id"]
    r = prod.post(f"/sessions/{sid}/join", json={"agent_id": victim},
                  headers={**PAY, "X-Emporia-Authorization": _jwt(prod, joiner)})
    assert r.status_code == 403
