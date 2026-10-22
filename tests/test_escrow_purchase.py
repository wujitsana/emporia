"""x402 escrow holds for service purchases (P99), through the relay.

The chain is faked at `escrow_chain`'s boundary (authorize / capture / void /
payment_state); everything above it is real: the offer, the payer's signature,
the relay's checks of what comes back, the hold record, and settlement by rail.
The live Base Sepolia run is the opt-in testnet lane.
"""
import base64
import json
import os
import uuid
from unittest.mock import patch

import pytest

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from emporia import escrow  # noqa: E402
from emporia.relay_server import app  # noqa: E402
from tests.test_emporia import _register_keyed, _signed_action_body  # noqa: E402

OPERATOR = Account.create()
PAYER = Account.create()
SELLER_WALLET = Account.create().address
FEE_RECEIVER = "0x8d96f65aA58421F976D83743B28dbf4929998209"
ENV = {
    "EMPORIA_ESCROW_NETWORK": "eip155:84532",
    "EMPORIA_ESCROW_RPC_URL": "https://rpc.example.test",
    "EMPORIA_ESCROW_CONTRACT": "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c",
    "EMPORIA_ESCROW_COLLECTOR": "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88",
    "EMPORIA_ESCROW_FEE_RECEIVER": FEE_RECEIVER,
    "EMPORIA_X402_ASSET_EIP155_84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "EMPORIA_X402_EXTRA_EIP155_84532": '{"name": "USDC", "version": "2"}',
    "EMPORIA_OPERATOR_PRIVATE_KEY": OPERATOR.key.hex(),
}
STAKE = {"mode": "stripe_link", "stake_per_participant": "5.00", "currency": "usd"}
AMOUNT = 5_000_000   # $5.00 in USDC atomic units


class FakeChain:
    def __init__(self):
        self.authorized, self.captured, self.voided = [], [], []
        self.capturable = AMOUNT

    async def authorize(self, cfg, signer, info, amount, sig):
        assert signer.address == OPERATOR.address
        self.authorized.append((info, amount, sig))
        return "0xauth" + uuid.uuid4().hex[:8]

    async def payment_state(self, cfg, info):
        return {"has_collected_payment": True, "capturable": self.capturable, "refundable": 0}

    async def capture(self, cfg, signer, info, amount):
        self.captured.append((info, amount))
        return "0xcapture"

    async def void(self, cfg, signer, info):
        self.voided.append(info)
        return "0xvoid"


@pytest.fixture
def chain():
    fake = FakeChain()
    from emporia import escrow_chain as ec
    from emporia import relay_server as rs
    with patch.dict(os.environ, ENV), \
         patch.object(ec, "authorize", new=fake.authorize), \
         patch.object(ec, "payment_state", new=fake.payment_state), \
         patch.object(ec, "capture", new=fake.capture), \
         patch.object(ec, "void", new=fake.void), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account", return_value=None):
        yield fake


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216))


def _hdr(a):
    return {"x-emporia-agent-id": a}


def _purchase(client):
    buyer, seller = "eb_" + uuid.uuid4().hex[:8], "es_" + uuid.uuid4().hex[:8]
    _register_keyed(client, buyer)
    _register_keyed(client, seller)
    r = client.post(f"/agents/{seller}/x402-wallet",
                    json={"network_family": "evm", "address": SELLER_WALLET}, headers=_hdr(seller))
    assert r.status_code == 200, r.text
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1",
                                         "creator_agent_id": buyer, "config": {"description": "x"},
                                         "payment_rules": STAKE}, headers=_hdr(buyer)).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    return sid, buyer, seller


def _offer(client, sid, buyer):
    r = client.post(f"/sessions/{sid}/fund", headers=_hdr(buyer))
    assert r.status_code == 402, r.text
    return r.json()["escrow"]


def _fund(client, sid, buyer, header):
    return client.post(f"/sessions/{sid}/fund", headers={**_hdr(buyer), "PAYMENT-ESCROW": header})


def _tampered(offer, **changes):
    """Sign an offer whose fields were changed after the relay built it."""
    info = escrow.PaymentInfo.from_json({**offer["payment_info"], "payer": PAYER.address, **changes})
    msg = escrow.receive_with_authorization_message(
        info, chain_id=offer["chain_id"], escrow=offer["escrow"], collector=offer["collector"],
        token_name=offer["token_name"], token_version=offer["token_version"])
    sig = PAYER.sign_typed_data(full_message=msg).signature
    return base64.b64encode(json.dumps({"payment_info": info.to_json(),
                                        "signature": "0x" + bytes(sig).hex()}).encode()).decode()


def _act(client, sid, who, action, payload=None):
    body = _signed_action_body(client, sid, who, action, payload or {},
                               "Taking this step in the purchase as agreed")
    r = client.post(f"/sessions/{sid}/action", json=body)
    assert r.status_code == 200, r.text


def _sql(q, *a):
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        return [dict(r) for r in conn.execute(q, a).fetchall()]


def test_the_offer_names_the_seller_the_amount_and_the_fee(client, chain):
    sid, buyer, seller = _purchase(client)
    offer = _offer(client, sid, buyer)
    info = offer["payment_info"]
    assert offer["scheme"] == "emporia-escrow-v1"
    assert info["receiver"] == SELLER_WALLET and info["operator"] == OPERATOR.address
    assert int(info["max_amount"]) == AMOUNT
    assert (int(info["min_fee_bps"]), int(info["max_fee_bps"])) == (250, 250)
    assert info["fee_receiver"] == FEE_RECEIVER
    assert info["payer"] == escrow.ZERO_ADDRESS, "the payer fills in their own address"


def test_a_signed_hold_is_authorized_and_starts_the_session(client, chain):
    sid, buyer, seller = _purchase(client)
    header = escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(), expected_amount=AMOUNT)
    r = _fund(client, sid, buyer, header)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    [(info, amount, _)] = chain.authorized
    assert (info.payer, info.receiver, amount) == (PAYER.address, SELLER_WALLET, AMOUNT)
    receipt = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert receipt["success"] and receipt["payer"] == PAYER.address
    [hold] = _sql("SELECT * FROM escrow_holds WHERE session_id = ?", sid)
    assert hold["state"] == "authorized" and hold["receiver_agent_id"] == seller
    [pay] = _sql("SELECT * FROM payments WHERE session_id = ?", sid)
    assert pay["rail"] == "x402" and pay["payment_intent_id"] == hold["hold_hash"]


def test_confirmed_delivery_captures_to_the_seller(client, chain):
    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    _act(client, sid, seller, "accept")
    _act(client, sid, seller, "deliver", {"deliverable": "done"})
    _act(client, sid, buyer, "confirm")
    [(info, amount)] = chain.captured
    assert (info.receiver, amount) == (SELLER_WALLET, AMOUNT)
    assert not chain.voided
    [row] = _sql("SELECT * FROM settlements WHERE session_id = ?", sid)
    assert (row["rail"], row["transfer_status"], row["winner_id"]) == ("x402", "escrow_captured", seller)
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == "captured"


def test_a_dispute_voids_the_hold_back_to_the_buyer(client, chain):
    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    _act(client, sid, seller, "accept")
    _act(client, sid, seller, "deliver", {"deliverable": "not it"})
    _act(client, sid, buyer, "dispute", {"reason": "wrong"})
    assert len(chain.voided) == 1 and not chain.captured
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == "voided"


def test_abandoning_voids_the_hold(client, chain):
    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    assert client.post(f"/sessions/{sid}/abandon", json={}, headers=_hdr(buyer)).status_code == 200
    assert len(chain.voided) == 1


@pytest.mark.parametrize("change", [
    {"receiver": "0x" + "66" * 20}, {"fee_receiver": "0x" + "77" * 20},
    {"max_amount": str(AMOUNT - 1)}, {"min_fee_bps": "0", "max_fee_bps": "0"},
    {"operator": "0x" + "88" * 20},
])
def test_a_hold_that_is_not_what_was_offered_is_refused_before_any_gas(client, chain, change):
    sid, buyer, seller = _purchase(client)
    r = _fund(client, sid, buyer, _tampered(_offer(client, sid, buyer), **change))
    assert r.status_code == 402, (change, r.text)
    assert not chain.authorized


def test_a_signature_from_someone_else_is_refused(client, chain):
    sid, buyer, seller = _purchase(client)
    offer = _offer(client, sid, buyer)
    info = escrow.PaymentInfo.from_json({**offer["payment_info"], "payer": PAYER.address})
    msg = escrow.receive_with_authorization_message(
        info, chain_id=offer["chain_id"], escrow=offer["escrow"], collector=offer["collector"],
        token_name=offer["token_name"], token_version=offer["token_version"])
    sig = Account.create().sign_typed_data(full_message=msg).signature
    header = base64.b64encode(json.dumps({"payment_info": info.to_json(),
                                          "signature": "0x" + bytes(sig).hex()}).encode()).decode()
    assert _fund(client, sid, buyer, header).status_code == 402
    assert not chain.authorized


def test_a_hold_that_did_not_hold_the_amount_does_not_start_anything(client, chain):
    chain.capturable = AMOUNT - 1
    sid, buyer, seller = _purchase(client)
    r = _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                    expected_amount=AMOUNT))
    assert r.status_code == 502
    assert client.get(f"/sessions/{sid}", headers=_hdr(buyer)).json()["status"] == "awaiting_funding"


def test_no_offer_where_the_seller_has_no_wallet_or_escrow_is_off(client, chain):
    buyer, seller = "nb_" + uuid.uuid4().hex[:8], "ns_" + uuid.uuid4().hex[:8]
    _register_keyed(client, buyer)
    _register_keyed(client, seller)
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1",
                                         "creator_agent_id": buyer, "payment_rules": STAKE},
                      headers=_hdr(buyer)).json()["session_id"]
    client.post(f"/sessions/{sid}/join", json={"agent_id": seller}, headers=_hdr(seller))
    r = client.post(f"/sessions/{sid}/fund", headers=_hdr(buyer))
    assert r.status_code == 409 and "wallet" in r.json()["detail"]


# -- the payer's own checks before it signs anything --------------------------

def _plain_offer(**info_changes):
    info = escrow.build_payment_info(
        operator=OPERATOR.address, payer=escrow.ZERO_ADDRESS, receiver=SELLER_WALLET,
        token=ENV["EMPORIA_X402_ASSET_EIP155_84532"], amount=AMOUNT, fee_bps=250,
        fee_receiver=FEE_RECEIVER, now=1_800_000_000, pre_approval_window=900,
        authorization_window=7 * 86400, refund_window=86400)
    wire = {**info.to_json(), **info_changes}
    return {"network": "eip155:84532", "chain_id": 84532, "escrow": ENV["EMPORIA_ESCROW_CONTRACT"],
            "collector": ENV["EMPORIA_ESCROW_COLLECTOR"], "token_name": "USDC",
            "token_version": "2", "payment_info": wire}


def test_the_payer_will_not_sign_an_unexpected_amount_fee_or_network():
    with pytest.raises(escrow.EscrowError, match="expected"):
        escrow.sign_offer(_plain_offer(), PAYER.key.hex(), expected_amount=AMOUNT - 1)
    with pytest.raises(escrow.EscrowError, match="cap"):
        escrow.sign_offer(_plain_offer(min_fee_bps="2000", max_fee_bps="2000"),
                          PAYER.key.hex(), expected_amount=AMOUNT)
    with pytest.raises(escrow.EscrowError, match="network"):
        escrow.sign_offer(_plain_offer(), PAYER.key.hex(), expected_amount=AMOUNT,
                          allowed_networks=["eip155:8453"])


def test_escrow_holds_are_visible_to_their_parties_only(client, chain):
    """P105: the dashboard's read of escrow holds — each party sees the hold,
    an outsider sees none, an anonymous caller is refused, and the signed
    PaymentInfo is never served."""
    sid, buyer, seller = _purchase(client)
    header = escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(), expected_amount=AMOUNT)
    assert _fund(client, sid, buyer, header).status_code == 200
    outsider = "eo_" + uuid.uuid4().hex[:8]
    _register_keyed(client, outsider)
    for who in (buyer, seller):
        holds = client.get("/payments/escrow-holds", headers=_hdr(who)).json()["holds"]
        [hold] = [h for h in holds if h["session_id"] == sid]
        assert hold["state"] == "authorized" and hold["amount_atomic"] == str(AMOUNT)
        assert "payment_info_json" not in hold
    assert client.get("/payments/escrow-holds", headers=_hdr(outsider)).json()["holds"] == []
    assert TestClient(app).get("/payments/escrow-holds").status_code == 401
