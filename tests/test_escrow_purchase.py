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
        self.capturable, self.refundable = AMOUNT, 0
        self.fail_capture = self.fail_void = None   # an exception to raise, P111

    async def authorize(self, cfg, signer, info, amount, sig):
        assert signer.address == OPERATOR.address
        self.authorized.append((info, amount, sig))
        return "0xauth" + uuid.uuid4().hex[:8]

    async def payment_state(self, cfg, info):
        return {"has_collected_payment": True, "capturable": self.capturable,
                "refundable": self.refundable}

    async def capture(self, cfg, signer, info, amount):
        if self.fail_capture:
            raise self.fail_capture
        self.captured.append((info, amount))
        self.capturable, self.refundable = 0, amount
        return "0xcapture"

    async def void(self, cfg, signer, info):
        if self.fail_void:
            raise self.fail_void
        self.voided.append(info)
        self.capturable = 0
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
    # P129: the ledger records the split the contract made — $0.125 of $5.00,
    # which the cents columns can only round.
    assert (row["platform_fee_atomic"], row["winner_payout_atomic"]) == ("125000", "4875000")
    # P153: pages can say how it was paid, and the relay says where it holds USDC.
    got = client.get(f"/sessions/{sid}", headers=_hdr(buyer)).json()
    assert got["paid_by"] == ["x402"]
    assert client.get("/health").json()["x402_escrow_network"] == "eip155:84532"


def test_a_dispute_voids_the_hold_back_to_the_buyer(client, chain):
    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    _act(client, sid, seller, "accept")
    _act(client, sid, seller, "deliver", {"deliverable": "not it"})
    _act(client, sid, buyer, "dispute", {"reason": "wrong"})
    assert len(chain.voided) == 1 and not chain.captured
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == "voided"
    assert _sql("SELECT status FROM payments WHERE session_id = ?", sid)[0]["status"] == \
        "released", "P130: the ledger says the stake went back"


def test_abandoning_voids_the_hold(client, chain):
    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    assert client.post(f"/sessions/{sid}/abandon", json={}, headers=_hdr(buyer)).status_code == 200
    assert len(chain.voided) == 1
    assert _sql("SELECT status FROM payments WHERE session_id = ?", sid)[0]["status"] == "released"


def test_a_failed_void_leaves_the_payment_confirmed_until_it_lands(client, chain):
    from emporia.escrow_chain import EscrowChainError

    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    chain.fail_void = EscrowChainError("eth_estimateGas failed: upstream timeout")
    assert client.post(f"/sessions/{sid}/abandon", json={}, headers=_hdr(buyer)).status_code == 200
    assert _sql("SELECT status FROM payments WHERE session_id = ?", sid)[0]["status"] == "confirmed"
    chain.fail_void = None
    assert _reconcile()["escrow_voided"] >= 1
    assert _sql("SELECT status FROM payments WHERE session_id = ?", sid)[0]["status"] == \
        "released", "the reconciler's void records it too"


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


# ── P111: the chain decides, and what settlement could not finish is retried ──

def _funded_and_delivered(client, chain):
    sid, buyer, seller = _purchase(client)
    _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                expected_amount=AMOUNT))
    _act(client, sid, seller, "accept")
    _act(client, sid, seller, "deliver", {"deliverable": "done"})
    return sid, buyer, seller


def _reconcile():
    import asyncio

    from emporia.routes.sessions import reconcile_escrow_holds
    return asyncio.run(reconcile_escrow_holds())


def test_a_failed_capture_is_retried_until_the_seller_is_paid(client, chain):
    from emporia.escrow_chain import EscrowChainError

    sid, buyer, seller = _funded_and_delivered(client, chain)
    chain.fail_capture = EscrowChainError("eth_estimateGas failed: upstream timeout")
    _act(client, sid, buyer, "confirm")
    [row] = _sql("SELECT * FROM settlements WHERE session_id = ?", sid)
    assert row["transfer_status"] == "capture_failed"
    assert row["platform_fee_atomic"] == "125000", "the split a retried capture will pay"
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == \
        "capture_pending"

    chain.fail_capture = None
    assert _reconcile()["escrow_captured"] == 1
    assert len(chain.captured) == 1
    [row] = _sql("SELECT * FROM settlements WHERE session_id = ?", sid)
    assert (row["transfer_status"], row["transfer_id"]) == ("escrow_captured", "0xcapture")
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == "captured"


def test_a_capture_whose_receipt_timed_out_but_landed_counts_as_paid(client, chain):
    from emporia.escrow_chain import EscrowChainError

    sid, buyer, seller = _funded_and_delivered(client, chain)
    err = EscrowChainError("no receipt for 0xslow within 120s")
    err.tx_hash = "0xslow"
    chain.fail_capture = err
    chain.capturable, chain.refundable = 0, AMOUNT     # it landed after all
    _act(client, sid, buyer, "confirm")
    [row] = _sql("SELECT * FROM settlements WHERE session_id = ?", sid)
    assert (row["transfer_status"], row["transfer_id"]) == ("escrow_captured", "0xslow")


def test_a_failed_void_is_retried_back_to_the_buyer(client, chain):
    from emporia.escrow_chain import EscrowChainError

    sid, buyer, seller = _funded_and_delivered(client, chain)
    chain.fail_void = EscrowChainError("rpc down")
    _act(client, sid, buyer, "dispute", {"reason": "wrong"})
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == \
        "void_pending"
    chain.fail_void = None
    assert _reconcile()["escrow_voided"] == 1
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == "voided"


def test_a_hold_past_its_authorization_window_is_left_to_the_payer(client, chain):
    from emporia.escrow_chain import EscrowChainError

    sid, buyer, seller = _funded_and_delivered(client, chain)
    chain.fail_capture = EscrowChainError("rpc down")
    _act(client, sid, buyer, "confirm")
    chain.fail_capture = None
    with patch("time.time", return_value=4_000_000_000):
        assert _reconcile()["escrow_expired"] == 1
    assert not chain.captured
    assert _sql("SELECT state FROM escrow_holds WHERE session_id = ?", sid)[0]["state"] == "expired"


def test_a_purchase_being_funded_cannot_be_funded_twice(client, chain):
    from emporia import relay_server as rs
    from emporia.routes.sessions import AWAITING_FUNDING, FUNDING

    sid, buyer, seller = _purchase(client)
    header = escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(), expected_amount=AMOUNT)
    assert rs.claim_session_status(sid, AWAITING_FUNDING, FUNDING)    # another request in flight
    r = _fund(client, sid, buyer, header)
    assert r.status_code == 409 and "already being funded" in r.text
    assert not chain.authorized


def test_a_failed_authorize_releases_the_claim(client, chain):
    from emporia.escrow_chain import EscrowChainError

    sid, buyer, seller = _purchase(client)
    header = escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(), expected_amount=AMOUNT)

    async def refused(*a, **k):
        raise EscrowChainError("transaction reverted")
    chain.capturable = 0
    with patch("emporia.escrow_chain.authorize", new=refused):
        assert _fund(client, sid, buyer, header).status_code == 402
    assert _sql("SELECT status FROM sessions WHERE session_id = ?", sid)[0]["status"] == \
        "awaiting_funding"


def test_a_hold_for_the_wrong_amount_is_voided_at_once(client, chain):
    sid, buyer, seller = _purchase(client)
    chain.capturable = AMOUNT - 1
    r = _fund(client, sid, buyer, escrow.sign_offer(_offer(client, sid, buyer), PAYER.key.hex(),
                                                    expected_amount=AMOUNT))
    assert r.status_code == 502 and "voided back" in r.text
    assert len(chain.voided) == 1


# ── P112: service deadlines end on their own, inside the hold ──

def test_a_silent_buyer_is_auto_confirmed_and_the_seller_paid(client, chain):
    """Delivered, then the buyer never acts: the sweep ends it, the hold is captured."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from emporia.routes.sessions import sweep_service_deadlines

    sid, buyer, seller = _funded_and_delivered(client, chain)
    asyncio.run(sweep_service_deadlines())
    assert _sql("SELECT status FROM sessions WHERE session_id = ?", sid)[0]["status"] == "active"
    captured_before = len(chain.captured)
    later = datetime.now(UTC) + timedelta(days=3)

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later
    with patch("emporia.modules.service.datetime", _Later):
        asyncio.run(sweep_service_deadlines())     # other tests' sessions may end too
        assert _sql("SELECT status FROM sessions WHERE session_id = ?", sid)[0]["status"] == \
            "completed"
        [row] = _sql("SELECT * FROM settlements WHERE session_id = ?", sid)
        assert (row["winner_id"], row["transfer_status"]) == (seller, "escrow_captured")
        captured = len(chain.captured)
        assert asyncio.run(sweep_service_deadlines())["service_timeouts"] == 0, "settled once"
    assert captured > captured_before and len(chain.captured) == captured
    assert len(_sql("SELECT * FROM settlements WHERE session_id = ?", sid)) == 1


def test_a_service_deadline_must_end_inside_the_hold(client, chain):
    buyer = "ed_" + uuid.uuid4().hex[:8]
    _register_keyed(client, buyer)
    r = client.post("/sessions", json={"module_type": "emporia:service:v1",
                                       "creator_agent_id": buyer,
                                       "config": {"description": "x", "deadline_hours": 500},
                                       "payment_rules": STAKE}, headers=_hdr(buyer))
    assert r.status_code == 400 and "at most 120" in r.text


def test_the_agent_profile_shows_where_the_agent_is_paid(client, chain):
    """P120: an agent could not tell whether its wallet was registered — the
    profile carried no payout address at all."""
    sid, buyer, seller = _purchase(client)
    assert client.get(f"/agents/{seller}", headers=_hdr(seller)).json()["x402_pay_to"] == \
        {"evm": SELLER_WALLET}
    assert client.get(f"/agents/{buyer}", headers=_hdr(buyer)).json()["x402_pay_to"] == {}
