"""The relay's fee: 1% on a paid entry, published on its signed card, shown in the terms before any payment.

A paid entry is two x402 payments in one action: the creator's share to the creator, the fee to the relay's
published fee address. Entry is granted only once both settle. The relay never holds either (it is a notary and
a post office, never a broker). No fee address: no fee, and the card says so. All with the x402 fakes: nothing
here touches a chain.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from emporia import fees, payments
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import (
    _X402_ENV,
    _X402_EXTRA,
    _register,
    _register_keyed,
    _signed_room,
    _x402_payment_signature_header,
)

FEE_ADDRESS = "0x" + "fe" * 20
CREATOR_WALLET = "0x" + "cd" * 20
PRICE = 1000           # cents: $10.00 -> 990 to the creator, 10 to the relay


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51480))


@pytest.fixture
def fee_env(monkeypatch):
    for k, v in _X402_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EMPORIA_ESCROW_FEE_RECEIVER", raising=False)
    monkeypatch.delenv("EMPORIA_FEE_BPS", raising=False)
    monkeypatch.setenv("EMPORIA_FEE_ADDRESS", FEE_ADDRESS)


def _atomic(cents: int) -> str:
    return str(cents * 10_000)


def _accepted(pay_to: str, cents: int) -> dict:
    return {"scheme": "exact", "network": "eip155:8453", "amount": _atomic(cents), "asset": "0xUSDCFAKE",
            "payTo": pay_to, "maxTimeoutSeconds": 300, "extra": _X402_EXTRA}


def _header(pay_to: str, cents: int, nonce_byte: str) -> str:
    return _x402_payment_signature_header(_accepted(pay_to, cents), {
        "signature": "0xsig",
        "authorization": {"from": "0x" + "11" * 20, "nonce": "0x" + nonce_byte * 32, "validBefore": "9999999999"}})


def _paid_room(client, owner: str, cents: int = PRICE) -> str:
    _register_keyed(client, owner)
    __import__("conftest").give_wallet(owner, "evm", CREATOR_WALLET)
    r = client.post("/rooms", json=_signed_room(owner, {
        "name": f"{owner} room", "creator_id": owner, "room_type": "private", "gate_type": "paid",
        "entry_fee_cents": cents}), headers={"x-emporia-agent-id": owner})
    assert r.status_code == 200, r.text
    return r.json()["room_id"]


def _join(client, room_id: str, payer: str, **headers):
    return client.post(f"/rooms/{room_id}/join", json={"agent_id": payer},
                       headers={"x-emporia-agent-id": payer, **headers})


# ── the fee on the card ──────────────────────────────────────────────────────────────────────────

def test_the_signed_relay_card_publishes_the_fee_and_where_it_is_paid(client, fee_env):
    from emporia.identity import verify_relay_card
    card = client.get("/relay/card").json()
    assert card["fee_bps"] == 100 and card["fee_address"] == FEE_ADDRESS
    assert verify_relay_card(card)["ok"], "the fee is inside the relay's signature"
    health = client.get("/health").json()
    assert health["fee_bps"] == 100 and health["fee_address"] == FEE_ADDRESS


def test_no_fee_address_means_no_fee_and_the_card_says_fee_zero(client, fee_env, monkeypatch):
    monkeypatch.delenv("EMPORIA_FEE_ADDRESS")
    card = client.get("/relay/card").json()
    assert card["fee_bps"] == 0 and card["fee_address"] is None
    assert fees.split(PRICE) == (PRICE, 0)


def test_the_escrow_fee_receiver_is_the_fee_address_when_none_is_set_apart(fee_env, monkeypatch):
    monkeypatch.delenv("EMPORIA_FEE_ADDRESS")
    monkeypatch.setenv("EMPORIA_ESCROW_FEE_RECEIVER", "0x" + "ab" * 20)
    assert fees.card()["fee_address"] == "0x" + "ab" * 20 and fees.fee_bps() == 100


def test_the_fee_is_one_percent_the_same_for_everyone(fee_env):
    assert fees.split(PRICE) == (990, 10)
    assert fees.split(250) == (248, 2)           # rounded to the cent
    assert fees.split(40) == (40, 0)             # below a cent: no fee part
    assert payments.platform_fee(PRICE) == 10    # deals and cards use the same value: 1%, not 2.5%


# ── the terms, before any payment ────────────────────────────────────────────────────────────────

def test_the_402_shows_the_fee_in_its_terms_and_asks_for_two_payments(client, fee_env):
    room = _paid_room(client, "fee_t_owner")
    _register(client, "fee_t_payer")
    r = _join(client, room, "fee_t_payer")
    assert r.status_code == 402
    body = r.json()
    assert body["terms"]["fee_bps"] == 100 and body["terms"]["fee_cents"] == 10
    assert body["terms"]["creator_cents"] == 990 and body["terms"]["total_cents"] == PRICE
    assert body["terms"]["fee_address"] == FEE_ADDRESS
    [creator_leg] = body["x402"]["accepts"]
    [fee_leg] = body["x402_fee"]["accepts"]
    assert creator_leg["payTo"] == CREATOR_WALLET and creator_leg["amount"] == _atomic(990)
    assert fee_leg["payTo"] == FEE_ADDRESS and fee_leg["amount"] == _atomic(10)
    assert "PAYMENT-REQUIRED-FEE" in r.headers


def test_without_a_fee_address_the_402_is_one_payment_for_the_whole_price(client, fee_env, monkeypatch):
    monkeypatch.delenv("EMPORIA_FEE_ADDRESS")
    room = _paid_room(client, "fee_z_owner")
    _register(client, "fee_z_payer")
    body = _join(client, room, "fee_z_payer").json()
    assert "x402_fee" not in body and body["terms"]["fee_cents"] == 0
    assert body["x402"]["accepts"][0]["amount"] == _atomic(PRICE)


# ── two payments, both required ──────────────────────────────────────────────────────────────────

@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_entry_needs_both_payments_and_each_goes_to_its_own_address(mock_verify, mock_settle, client, fee_env):
    room = _paid_room(client, "fee_b_owner")
    _register(client, "fee_b_payer")
    mock_verify.return_value = {"isValid": True, "payer": "0x" + "11" * 20}
    mock_settle.side_effect = [
        {"success": True, "transaction": "0xcreatorshare", "network": "eip155:8453"},
        {"success": True, "transaction": "0xrelayfee", "network": "eip155:8453"},
    ]
    only_creator = _join(client, room, "fee_b_payer", **{"PAYMENT-SIGNATURE": _header(CREATOR_WALLET, 990, "01")})
    assert only_creator.status_code == 402 and only_creator.json()["declined"]["reason"] == "fee_payment"
    mock_settle.assert_not_called()

    r = _join(client, room, "fee_b_payer", **{"PAYMENT-SIGNATURE": _header(CREATOR_WALLET, 990, "02"),
                                              "PAYMENT-SIGNATURE-FEE": _header(FEE_ADDRESS, 10, "03")})
    assert r.status_code == 200, r.text
    settled_to = [c.args[1]["payTo"] for c in mock_settle.call_args_list]
    assert settled_to == [CREATOR_WALLET, FEE_ADDRESS], "the creator's share to the creator, the fee to the relay's address"
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM settlements WHERE session_id = ?", (room,)).fetchone()
    assert (row["total_stake_cents"], row["platform_fee_cents"], row["winner_payout_cents"]) == (PRICE, 10, 990)
    assert row["platform_fee_bps"] == 100 and "0xrelayfee" in row["payment_intent_ids"]


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_a_wrong_amount_or_address_on_either_leg_is_refused_before_anything_settles(
        mock_verify, mock_settle, client, fee_env):
    room = _paid_room(client, "fee_w_owner")
    _register(client, "fee_w_payer")
    mock_verify.return_value = {"isValid": True, "payer": "0x" + "11" * 20}
    good_creator = _header(CREATOR_WALLET, 990, "11")
    good_fee = _header(FEE_ADDRESS, 10, "12")
    cases = {
        "fee leg too small": (good_creator, _header(FEE_ADDRESS, 1, "13")),
        "fee leg to someone else": (good_creator, _header("0x" + "99" * 20, 10, "14")),
        "creator leg for the whole price": (_header(CREATOR_WALLET, PRICE, "15"), good_fee),
        "creator share to the relay": (_header(FEE_ADDRESS, 990, "16"), good_fee),
    }
    for why, (creator, fee) in cases.items():
        r = _join(client, room, "fee_w_payer", **{"PAYMENT-SIGNATURE": creator, "PAYMENT-SIGNATURE-FEE": fee})
        assert r.status_code == 402, why
    mock_settle.assert_not_called()
    assert client.get(f"/rooms/{room}", headers={"x-emporia-agent-id": "fee_w_payer"}).status_code in (200, 403, 404)


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_the_fee_leg_must_be_on_the_same_network_as_the_creators(mock_verify, mock_settle, client, fee_env):
    room = _paid_room(client, "fee_n_owner")
    _register(client, "fee_n_payer")
    mock_verify.return_value = {"isValid": True}
    other = {**_accepted(FEE_ADDRESS, 10), "network": "eip155:84532"}
    fee = _x402_payment_signature_header(other, {"signature": "0x", "authorization": {"nonce": "0x" + "21" * 32}})
    r = _join(client, room, "fee_n_payer", **{"PAYMENT-SIGNATURE": _header(CREATOR_WALLET, 990, "22"),
                                              "PAYMENT-SIGNATURE-FEE": fee})
    assert r.status_code == 402 and "same network" in r.json()["message"]
    mock_settle.assert_not_called()


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_with_no_fee_address_one_payment_of_the_whole_price_enters(mock_verify, mock_settle, client, fee_env, monkeypatch):
    monkeypatch.delenv("EMPORIA_FEE_ADDRESS")
    room = _paid_room(client, "fee_o_owner")
    _register(client, "fee_o_payer")
    mock_verify.return_value = {"isValid": True}
    mock_settle.return_value = {"success": True, "transaction": "0xwhole", "network": "eip155:8453"}
    r = _join(client, room, "fee_o_payer", **{"PAYMENT-SIGNATURE": _header(CREATOR_WALLET, PRICE, "31")})
    assert r.status_code == 200, r.text
    assert mock_settle.call_count == 1


def test_a_free_room_stays_free(client, fee_env):
    _register_keyed(client, "fee_f_owner")
    _register(client, "fee_f_payer")
    room = client.post("/rooms", json=_signed_room("fee_f_owner", {"name": "free room"}),
                       headers={"x-emporia-agent-id": "fee_f_owner"}).json()["room_id"]
    assert _join(client, room, "fee_f_payer").status_code == 200


# ── the paying client ────────────────────────────────────────────────────────────────────────────

NET = "eip155:84532"


def _entry_body(creator_atomic: int, fee_atomic: int) -> dict:
    from eth_account import Account

    from conftest import payee_for
    pay_to = Account.create().address

    def leg(amount: int, to: str) -> dict:
        return {"scheme": "exact", "network": NET, "amount": str(amount),
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "payTo": to, "maxTimeoutSeconds": 300,
                "extra": {"name": "USDC", "version": "2"}}
    resource = {"url": "http://relay/rooms/r1/join"}
    return {"x402": {"x402Version": 2, "resource": resource, "accepts": [leg(creator_atomic, pay_to)]},
            "x402_fee": {"x402Version": 2, "resource": resource, "accepts": [leg(fee_atomic, FEE_ADDRESS)]},
            "payee": payee_for(evm=pay_to)}


@pytest.fixture
def wallet_env(tmp_path):
    from unittest.mock import patch as _patch

    from emporia import identity
    env = {"EMPORIA_X402_PAY_NETWORKS": NET, "EMPORIA_X402_MAX_PAYMENT_CENTS": "500"}
    with _patch.object(identity, "KEY_DIR", tmp_path), _patch.dict("os.environ", env):
        yield tmp_path


def test_the_wallet_signs_both_legs_of_an_entry_on_the_same_network(wallet_env):
    pytest.importorskip("eth_account")
    import base64
    import json

    from emporia import agent_wallet
    agent_wallet.generate("fee_payer_one")
    headers = agent_wallet.sign_entry("fee_payer_one", _entry_body(990_000, 10_000))
    assert set(headers) == {"PAYMENT-SIGNATURE", "PAYMENT-SIGNATURE-FEE"}

    def decode(h: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4)))
    assert decode(headers["PAYMENT-SIGNATURE"])["accepted"]["amount"] == "990000"
    fee = decode(headers["PAYMENT-SIGNATURE-FEE"])
    assert fee["accepted"]["payTo"] == FEE_ADDRESS and fee["accepted"]["amount"] == "10000"
    assert fee["accepted"]["network"] == decode(headers["PAYMENT-SIGNATURE"])["accepted"]["network"]


def test_the_wallet_refuses_a_fee_far_above_the_stated_share_and_gives_the_charge_back(wallet_env):
    pytest.importorskip("eth_account")
    from emporia import agent_wallet
    agent_wallet.generate("fee_payer_two")
    before = agent_wallet.ledger_spent("fee_payer_two")
    with pytest.raises(agent_wallet.AgentWalletError, match="more than"):
        agent_wallet.sign_entry("fee_payer_two", _entry_body(600_000, 400_000))    # a 40% "fee"
    assert agent_wallet.ledger_spent("fee_payer_two") == before, "nothing was signed, nothing is charged"


def test_a_paid_entry_without_a_fee_leg_signs_one_payment(wallet_env):
    pytest.importorskip("eth_account")
    from emporia import agent_wallet
    agent_wallet.generate("fee_payer_three")
    body = _entry_body(1_000_000, 0)
    body.pop("x402_fee")
    assert set(agent_wallet.sign_entry("fee_payer_three", body)) == {"PAYMENT-SIGNATURE"}


def test_with_no_fee_address_nothing_that_pays_the_relay_pays_it(fee_env, monkeypatch):
    monkeypatch.delenv("EMPORIA_FEE_ADDRESS")
    assert fees.fee_bps() == 0 and payments.platform_fee(PRICE) == 0
    assert fees.card()["fee_bps"] == 0


def test_one_published_number_is_the_fee_everywhere(client, fee_env, monkeypatch):
    monkeypatch.setenv("EMPORIA_FEE_BPS", "200")
    card, health = client.get("/relay/card").json(), client.get("/health").json()
    assert card["fee_bps"] == health["fee_bps"] == fees.fee_bps() == 200
    assert payments.platform_fee(PRICE) == 20 and "operator_fee_bps" not in card and "operator_fee_bps" not in health
