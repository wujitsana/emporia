"""P145: a payer takes back an escrow hold the relay never settled — using only
what it saved when it paid, its own wallet and a public RPC."""
import json
import time

import pytest
from eth_account import Account

from emporia import agent_wallet, escrow, escrow_chain, identity
from emporia import mcp_server as m

OPERATOR = Account.create()


@pytest.fixture
def saved_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(agent_wallet, "load_key", lambda aid: "0x" + "11" * 32)
    payer = Account.from_key("0x" + "11" * 32).address

    def write(authorization_expiry):
        info = escrow.build_payment_info(
            operator=OPERATOR.address, payer=payer, receiver="0x" + "22" * 20,
            token="0x036CbD53842c5426634e7929541eC2318f3dCF7e", amount=1_000_000, fee_bps=250,
            fee_receiver="0x" + "33" * 20, now=int(time.time()), pre_approval_window=900,
            authorization_window=3600, refund_window=7200)
        d = info.to_json()
        d["authorization_expiry"] = authorization_expiry
        offer = {"network": "eip155:84532", "chain_id": 84532,
                 "escrow": "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c",
                 "collector": "0x" + "44" * 20, "token_name": "USDC", "token_version": "2",
                 "payment_info": d}
        m._agent_dir("payer1", "holds", "sess_r", ".json").write_text(
            json.dumps({"offer": offer, "payer": payer, "relay_url": "http://relay.test"}))
    return write


def _chain(monkeypatch, capturable):
    calls = []

    async def state(cfg, info):
        return {"has_collected_payment": True, "capturable": capturable, "refundable": 0}

    async def reclaim(cfg, signer, info):
        calls.append((cfg.contract, cfg.rpc_url, signer.address, info.payer))
        return "0xreclaimed"
    monkeypatch.setattr(escrow_chain, "payment_state", state)
    monkeypatch.setattr(escrow_chain, "reclaim", reclaim)
    return calls


def test_after_the_expiry_the_payer_reclaims_from_the_contract(saved_hold, monkeypatch):
    saved_hold(int(time.time()) - 10)
    calls = _chain(monkeypatch, 1_000_000)
    r = m.reclaim_escrow("sess_r", agent_id="payer1")
    assert r["success"] and r["transaction"] == "0xreclaimed" and r["reclaimed_atomic"] == 1_000_000
    [(contract, rpc, signer, payer)] = calls
    assert contract == "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c"
    assert rpc == "https://sepolia.base.org", "a public RPC — no relay involved"
    assert signer == payer, "signed by the payer's own wallet"


def test_too_early_says_when(saved_hold, monkeypatch):
    saved_hold(int(time.time()) + 3600)
    calls = _chain(monkeypatch, 1_000_000)
    r = m.reclaim_escrow("sess_r", agent_id="payer1")
    assert not r["success"] and "too early" in r["error"] and not calls


def test_nothing_left_is_said_plainly(saved_hold, monkeypatch):
    saved_hold(int(time.time()) - 10)
    calls = _chain(monkeypatch, 0)
    r = m.reclaim_escrow("sess_r", agent_id="payer1")
    assert not r["success"] and "nothing left" in r["error"] and not calls


def test_no_saved_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    assert "no hold saved" in m.reclaim_escrow("sess_none", agent_id="payer1")["error"]
