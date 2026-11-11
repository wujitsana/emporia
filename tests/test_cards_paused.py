"""Cards (Stripe) are paused in this release: one switch, EMPORIA_CARDS=on.

With cards paused and a test key set, the relay behaves as if the key were unset: it offers no
card, opens no account, confirms no PaymentIntent, moves no money and creates no intent. The
Stripe code is kept; `EMPORIA_CARDS=on` (what the rest of the suite runs with) restores it.
"""
import argparse
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from emporia import cards, features as F, relay_server as rs
from tests.test_noncustodial_cards import PAY, _accounts, _agents, _hdr, _paid_room, stripe_entries  # noqa: F401


@pytest.fixture
def calls(stripe):
    """Every request that reaches Stripe's API (the fake answers it)."""
    seen: list[tuple[str, str]] = []
    post, get = stripe.post, stripe.get

    async def rec_post(url, *a, **kw):
        seen.append(("POST", url))
        return await post(url, *a, **kw)

    async def rec_get(url, *a, **kw):
        seen.append(("GET", url))
        return await get(url, *a, **kw)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=rec_post), \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=rec_get):
        yield seen


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.delenv("EMPORIA_CARDS", raising=False)
    monkeypatch.setenv("EMPORIA_CUSTODIAL_CARDS", "1")     # even opted in, nothing custodial runs


def test_the_switch(monkeypatch):
    monkeypatch.delenv("EMPORIA_CARDS", raising=False)
    assert cards.cards_paused()
    monkeypatch.setenv("EMPORIA_CARDS", "off")
    assert cards.cards_paused()
    monkeypatch.setenv("EMPORIA_CARDS", "on")
    assert not cards.cards_paused()


def test_a_key_is_treated_as_unset_while_paused(stripe, paused):
    from emporia import payments
    assert cards.stripe_secret_key() == "" and rs._stripe_key_mode() == "" and not rs.custodial_cards_allowed()
    assert not rs._stripe_profile_ready()
    with pytest.raises(RuntimeError):
        payments._stripe_key()          # the one door every Stripe API call in payments.py goes through


def test_health_says_cards_are_paused(client, stripe, paused):
    h = client.get("/health").json()
    assert h["stripe_enabled"] is False and h["stripe_mode"] == ""
    assert h["cards_paused"] is True and h["stripe_key_ignored"] is True
    assert "stripe" not in h["payment_methods"] and "stripe_pi" not in h["payment_rails"]


def test_registration_opens_no_account(client, stripe, calls, paused):
    a, = _agents(client, 1, "cp")
    assert not [c for c in calls if "/accounts" in c[1] or "account_links" in c[1]]
    assert rs.AGENT_REGISTRY.get_stripe_account(a) is None


def test_onboarding_route_answers_paused(client, stripe, calls, paused):
    a, = _agents(client, 1, "cp")
    r = client.post(f"/agents/{a}/stripe-onboarding", json={"email": "a@example.org"}, headers=_hdr(a))
    assert r.status_code == 400 and "paused" in r.text
    assert calls == []


def test_webhook_is_not_found(client, stripe, paused):
    r = client.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 404


def test_a_paid_room_offers_no_card_and_never_self_confirms(client, stripe_entries, calls, paused):  # noqa: F811
    owner, payer = _agents(client, 2, "cp")
    room = _paid_room(client, owner)
    with _accounts(**{owner: "acct_owner"}):         # even a payee with an active account
        challenge = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=_hdr(payer))
        assert challenge.status_code == 402
        assert "www-authenticate" not in {k.lower() for k in challenge.headers}
        assert not (challenge.json().get("card") or {}).get("offered")
        # a payment intent id (the sk_test_ self-confirm path) and an SPT are both refused
        for body, hdr in (({"agent_id": payer, "payment_intent_id": "pi_x"}, _hdr(payer)),
                          ({"agent_id": payer}, PAY)):
            r = client.post(f"/rooms/{room}/join", json=body, headers=hdr)
            assert r.status_code in (400, 402, 403), r.text
    assert calls == [], f"nothing reached Stripe: {calls}"
    assert stripe_entries.pis == {} and stripe_entries.transfers == [] and stripe_entries.refunds == []


def test_no_intent_is_created_on_anyones_behalf(client, stripe, calls, paused):
    a, = _agents(client, 1, "cp")
    r = client.post("/payments/create-intent", json={"amount_cents": 500, "seller_id": "relay"}, headers=_hdr(a))
    assert r.status_code >= 400
    assert calls == [] and stripe.pis == {}


def test_every_stripe_call_in_payments_refuses_while_paused(stripe, calls, paused):
    """Accounts, intents, confirm, capture, cancel, transfers, refunds, reversals."""
    from emporia import payments as p
    jobs = [
        lambda: p.create_connected_account("a"),
        lambda: p.create_live_connected_account("a", email="a@example.org"),
        lambda: p.create_account_onboarding_link("acct_x", "https://x", "https://x"),
        lambda: p.verify_payment_intent("pi_x"), lambda: p.confirm_stripe_intent("pi_x"),
        lambda: p.capture_payment_intent("pi_x"), lambda: p.cancel_payment_hold("pi_x"),
        lambda: p.refund_payment_intent("pi_x"), lambda: p.reverse_transfer("tr_x"),
    ]
    for job in jobs:
        with pytest.raises(Exception):
            asyncio.run(job())
    assert calls == []


def test_the_reconciler_does_nothing(client, stripe, calls, paused):
    counts = asyncio.run(rs._reconcile_owed_payouts())
    assert not any(counts.values())
    assert calls == []


def test_setup_and_features_refuse(capsys, paused):
    from emporia import cli
    assert cli.cmd_setup_stripe(argparse.Namespace(agent="", stripe_key_env="", stripe_webhook_env="",
                                                   stripe_profile_id="")) == 1
    assert cards.PAUSED_LINE in capsys.readouterr().out
    assert cli.cmd_setup_agent(argparse.Namespace(stripe=True, name="x")) == 1
    assert cards.PAUSED_LINE in capsys.readouterr().out
    assert "stripe" in F.paused() and "stripe" not in F.enabled_from({"EMPORIA_FEATURES": "stripe,money"})
    assert "stripe" not in F.enabled_from({"STRIPE_SECRET_KEY": "sk_test_x"})
    assert "stripe" not in F.derive(stripe=True)
    from emporia import setup_choices as sc
    assert "stripe" not in dict(sc.CHOICES["optional"].labels())


def test_features_on_stripe_refuses(capsys, paused, tmp_path):
    from emporia import agent_init, cli
    agent_init.write_env(tmp_path, {"EMPORIA_AGENT_ID": "cp_agent"})
    rc = cli.cmd_features(argparse.Namespace(action="on", names=["stripe"], dir=str(tmp_path), agent=""))
    assert rc == 1 and cards.PAUSED_LINE in capsys.readouterr().out


def test_the_installer_resolves_no_key_while_paused(tmp_path, capsys, paused):
    from installer import install
    assert install.resolve_provider_secrets(tmp_path, stripe_secret_key="sk_test_x", interactive=False) == (None, None)
    install._print_provider_status("sk_test_x", None)
    assert "paused in this release" in capsys.readouterr().out


def test_the_tools_are_not_installed_while_paused(paused):
    off = F.hidden_by_features(F.enabled_from({}))
    assert {"start_stripe_onboarding", "create_payment_intent"} <= off


def test_cards_on_restores_today(client, stripe_entries, monkeypatch):  # noqa: F811
    monkeypatch.setenv("EMPORIA_CARDS", "on")
    assert not cards.cards_paused() and cards.stripe_secret_key() == "sk_test_price_models"
    assert rs._stripe_key_mode() == "test"
    owner, payer = _agents(client, 2, "on")
    assert rs.AGENT_REGISTRY.get_stripe_account(owner), "registration opens its test account again"
    room = _paid_room(client, owner)
    with _accounts(**{owner: "acct_owner"}):
        r = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers=PAY)
    assert r.status_code == 200, r.text
    assert len(stripe_entries.pis) == 1
