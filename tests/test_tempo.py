"""P156: Tempo stablecoin payments, straight to the payee, checked from the receipt."""
import base64
import json
import uuid

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from emporia import tempo
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed

TOKEN = "0x20c0000000000000000000000000000000000000"
CFG = {"network": "eip155:42431", "chain_id": 42431, "token": TOKEN, "rpc": "https://rpc.test"}
SECRET = b"s" * 32


def _receipt(offer, *, amount=None, to=None, memo=None, status="0x1", token=TOKEN):
    req = offer["request"]
    return {"status": status, "logs": [{
        "address": token,
        "topics": [tempo.TRANSFER_WITH_MEMO_TOPIC, "0x" + "11" * 32,
                   "0x" + (to or req["recipient"]).lower().removeprefix("0x").rjust(64, "0"),
                   memo or req["methodDetails"]["memo"]],
        "data": hex(int(amount or req["amount"])),
    }]}


def _cred(offer, h="0xabc"):
    return base64.b64encode(json.dumps({"offer": offer, "payload": {"type": "hash", "hash": h}}).encode()).decode()


@pytest.fixture
def chain(monkeypatch):
    state = {"receipt": None}
    monkeypatch.setattr(tempo, "_rpc", lambda url, method, params: state["receipt"])
    return state


def _offer(payer="alice", amount_cents=50, recipient=None):
    return tempo.build_offer(amount_cents=amount_cents, resource="emporia:room:r1",
                             recipient=recipient or Account.create().address, payer_id=payer,
                             secret=SECRET, cfg=CFG)


def _verify(cred, offer, **kw):
    return tempo.verify(cred, payer_id=kw.get("payer", "alice"), resource="emporia:room:r1",
                        amount_cents=kw.get("amount_cents", 50), recipient=offer["binding"]["recipient"],
                        secret=SECRET, cfg=CFG)


def test_an_honest_payment_verifies(chain):
    offer = _offer()
    chain["receipt"] = _receipt(offer)
    assert _verify(_cred(offer), offer) == "0xabc"
    assert offer["request"]["amount"] == "500000", "6 decimals: $0.50"


@pytest.mark.parametrize("bad", [
    {"amount": 400_000}, {"to": "0x" + "22" * 20}, {"memo": "0x" + "33" * 32},
    {"status": "0x0"}, {"token": "0x20c0000000000000000000000000000000000001"},
])
def test_a_receipt_that_is_not_this_payment_is_refused(chain, bad):
    offer = _offer()
    chain["receipt"] = _receipt(offer, **bad)
    with pytest.raises(tempo.TempoError):
        _verify(_cred(offer), offer)


def test_an_offer_altered_by_the_payer_is_refused(chain):
    offer = _offer()
    offer["binding"]["amount"] = "1"
    chain["receipt"] = _receipt(offer)
    with pytest.raises(tempo.TempoError, match="not issued by this relay"):
        _verify(_cred(offer), offer)


def test_an_offer_for_someone_else_or_expired_is_refused(chain):
    offer = _offer(payer="bob")
    chain["receipt"] = _receipt(offer)
    with pytest.raises(tempo.TempoError, match="different payer"):
        _verify(_cred(offer), offer)
    old = tempo.build_offer(amount_cents=50, resource="emporia:room:r1", recipient=Account.create().address,
                            payer_id="alice", secret=SECRET, cfg=CFG, now=1)
    chain["receipt"] = _receipt(old)
    with pytest.raises(tempo.TempoError, match="expired"):
        _verify(_cred(old), old)


def test_a_room_entry_paid_on_tempo_is_direct_and_cannot_be_replayed(chain, monkeypatch):
    monkeypatch.setenv("EMPORIA_TEMPO_NETWORK", "eip155:42431")
    monkeypatch.setenv("EMPORIA_TEMPO_TOKEN", TOKEN)
    client = TestClient(app, client=("127.0.0.1", 51230))
    owner, payer, other = (f"tp{i}_{uuid.uuid4().hex[:6]}" for i in range(3))
    for a in (owner, payer, other):
        _register_keyed(client, a)
    wallet = Account.create().address
    client.post(f"/agents/{owner}/x402-wallet", json={"network_family": "evm", "address": wallet},
                headers={"x-emporia-agent-id": owner})
    room = client.post("/rooms", json={"name": "Tempo room", "creator_id": owner, "room_type": "private",
                                       "gate_type": "paid", "entry_fee_cents": 50},
                       headers={"x-emporia-agent-id": owner}).json()["room_id"]
    first = client.post(f"/rooms/{room}/join", json={"agent_id": payer}, headers={"x-emporia-agent-id": payer})
    assert first.status_code == 402
    offer = first.json()["tempo"]
    assert offer["request"]["recipient"] == wallet.lower(), "paid straight to the creator"
    chain["receipt"] = _receipt(offer)
    h = "0x" + uuid.uuid4().hex * 2
    paid = client.post(f"/rooms/{room}/join", json={"agent_id": payer},
                       headers={"x-emporia-agent-id": payer, "PAYMENT-TEMPO": _cred(offer, h)})
    assert paid.status_code == 200, paid.text
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT rail, transfer_status, platform_fee_cents FROM settlements "
                           "WHERE session_id = ? AND payer_id = ?", (room, payer)).fetchone()
    assert tuple(row) == ("tempo", "direct", 0), "nothing held, nothing transferred, no cut"
    # The same transaction cannot buy someone else's entry.
    offer2 = client.post(f"/rooms/{room}/join", json={"agent_id": other},
                         headers={"x-emporia-agent-id": other}).json()["tempo"]
    replay = client.post(f"/rooms/{room}/join", json={"agent_id": other},
                         headers={"x-emporia-agent-id": other, "PAYMENT-TEMPO": _cred(offer, h)})
    assert replay.status_code == 402, "an offer bound to another payer, and a spent hash"
    assert offer2["binding"]["payer"] == other
