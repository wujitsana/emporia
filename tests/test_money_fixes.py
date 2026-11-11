"""Money-safety fixes S-1 to S-8 and the S-11 items (signer / wallet audit, 2026-11-11).

Each test is one finding, written to fail on the code before the fix. No network, no models, no real signer."""
import asyncio
import base64
import json
import os
import re
import threading
import time
from unittest.mock import patch

import pytest

pytest.importorskip("eth_account")
from eth_account import Account  # noqa: E402

from emporia import agent_wallet, autonomy, escrow, identity, networks, signer, signer_alerts as sa, tempo  # noqa: E402
from emporia.agent_wallet import AgentWalletError  # noqa: E402
from tests.test_signer import AGENT, _challenge_for, held  # noqa: E402,F401

BASE = "eip155:84532"
TEMPO_NET = "eip155:42431"
TEMPO_TOKEN = networks.NETWORKS[TEMPO_NET].usdc


@pytest.fixture
def wallet_env(tmp_path):
    env = {"EMPORIA_X402_PAY_NETWORKS": f"{BASE},{TEMPO_NET}", "EMPORIA_X402_MAX_PAYMENT_CENTS": "500",
           "EMPORIA_X402_MAX_DAILY_CENTS": "2000"}
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, env):
        agent_wallet.generate("payer_one")
        yield tmp_path


def _x402_body(amount, network=BASE):
    from conftest import payee_for
    pay_to = Account.create().address
    return {"x402": {"x402Version": 2, "resource": {"url": "http://relay/r"},
                     "accepts": [{"scheme": "exact", "network": network, "amount": str(amount),
                                  "asset": networks.NETWORKS[network].usdc, "payTo": pay_to,
                                  "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}]},
            "payee": payee_for(evm=pay_to)}


def _tempo_offer(amount="100000", chain_id=42431, currency=TEMPO_TOKEN, network=TEMPO_NET):
    from conftest import payee_for
    recipient = Account.create().address
    return {"network": network, "payee": payee_for(evm=recipient),
            "request": {"amount": str(amount), "currency": currency, "recipient": recipient,
                        "methodDetails": {"chainId": chain_id, "memo": "0x" + "00" * 32}}}


# ---- S-2: a non-positive amount ----------------------------------------------------------------

def test_s2_a_negative_tempo_amount_cannot_poison_the_daily_ledger(wallet_env):
    with patch.object(tempo, "pay", side_effect=tempo.TempoError("no RPC")):
        with pytest.raises(AgentWalletError):
            agent_wallet.pay_tempo("payer_one", _tempo_offer(amount="-1000000000000"))
    assert agent_wallet.ledger_spent("payer_one") == 0
    assert not agent_wallet._ledger_path("payer_one").exists() or \
        all(a > 0 for _, a in json.loads(agent_wallet._ledger_path("payer_one").read_text()))


@pytest.mark.parametrize("bad", ["0", "-5", "1.5", "abc", "", "1e3"])
def test_s2_every_pay_path_refuses_non_positive_or_fractional_amounts(wallet_env, bad):
    with pytest.raises((AgentWalletError, Exception)):
        agent_wallet.sign_exact("payer_one", _x402_body(bad))
    with pytest.raises(Exception):
        agent_wallet.pay_tempo("payer_one", _tempo_offer(amount=bad))
    with pytest.raises(Exception):
        agent_wallet.sign_escrow("payer_one", {}, expected_amount=int(bad) if bad.lstrip("-").isdigit() else 0)
    assert agent_wallet.ledger_spent("payer_one") == 0


def test_s2_charge_daily_and_gate_spend_refuse_non_positive(wallet_env, monkeypatch):
    with pytest.raises(AgentWalletError):
        agent_wallet.charge_daily("payer_one", -10_000, 10**9)
    with pytest.raises(AgentWalletError):
        agent_wallet._charge_daily("payer_one", 0)
    monkeypatch.setattr(autonomy, "current", lambda now=None: autonomy.Turn(True, "relay"))
    assert autonomy.gate_spend(-5_000_000)
    assert autonomy.gate_spend(0)


def test_s2_a_poisoned_ledger_heals(wallet_env):
    path = agent_wallet._ledger_path("payer_one")
    identity._write_secret_atomically(path, json.dumps([[time.time(), -10**12]]).encode())
    assert agent_wallet.ledger_spent("payer_one") == 0
    agent_wallet.charge_daily("payer_one", 1_000_000, 2_000_000)
    with pytest.raises(AgentWalletError, match="daily cap"):
        agent_wallet.charge_daily("payer_one", 1_500_000, 2_000_000)


# ---- S-3: Tempo chain / token / fee --------------------------------------------------------------

def test_s3_a_testnet_labelled_tempo_offer_is_never_signed_for_mainnet(wallet_env):
    with patch.object(tempo, "pay", return_value="hdr") as pay:
        with pytest.raises(AgentWalletError, match="chain"):
            agent_wallet.pay_tempo("payer_one", _tempo_offer(chain_id=4217))
        assert not pay.called


def test_s3_the_token_must_be_the_networks_own(wallet_env):
    with patch.object(tempo, "pay", return_value="hdr") as pay:
        with pytest.raises(AgentWalletError, match="stablecoin"):
            agent_wallet.pay_tempo("payer_one", _tempo_offer(currency="0x" + "ab" * 20))
        assert not pay.called
        assert agent_wallet.pay_tempo("payer_one", _tempo_offer()) == "hdr"


def test_s3_tempo_pay_takes_its_chain_from_the_network_and_bounds_the_fee():
    offer = _tempo_offer(chain_id=4217)
    with patch.object(tempo, "_rpc", side_effect=AssertionError("no RPC may be asked")):
        with pytest.raises(tempo.TempoNotSent, match="chain"):
            tempo.pay(offer, "0x" + "11" * 32)

    def rpc(url, method, params):
        return {"eth_estimateGas": hex(100_000), "eth_getBlockByNumber": {"baseFeePerGas": hex(10**15)},
                "eth_maxPriorityFeePerGas": hex(10**9), "eth_getTransactionCount": "0x0",
                "eth_sendRawTransaction": AssertionError("must not be sent")}[method]
    with patch.object(tempo, "_rpc", rpc):
        with pytest.raises(tempo.TempoNotSent, match="fee"):
            tempo.pay(_tempo_offer(), "0x" + "11" * 32, rpc_url="https://x")


# ---- S-11: Tempo refund when never broadcast; asset override guarded ------------------------------

def test_s11_a_tempo_payment_that_was_not_broadcast_gives_its_cap_back(wallet_env):
    with patch.object(tempo, "pay", side_effect=tempo.TempoNotSent("no RPC")):
        with pytest.raises(tempo.TempoNotSent):
            agent_wallet.pay_tempo("payer_one", _tempo_offer())
    assert agent_wallet.ledger_spent("payer_one") == 0


def test_s11_a_tempo_payment_that_may_have_been_broadcast_keeps_its_charge(wallet_env):
    with patch.object(tempo, "pay", side_effect=tempo.TempoError("no receipt within 60s")):
        with pytest.raises(tempo.TempoError):
            agent_wallet.pay_tempo("payer_one", _tempo_offer())
    assert agent_wallet.ledger_spent("payer_one") == 100_000


def test_s11_the_asset_override_settings_are_the_persons():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "hermes_plugin" / "emporia" / "key_guard.py"
    spec = importlib.util.spec_from_file_location("emporia_kg_money", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for name in ("EMPORIA_X402_ASSET_EIP155_84532", "EMPORIA_ESCROW_CONTRACT", "EMPORIA_TEMPO_RPC_URL"):
        out = guard.pre_tool_call(tool_name="terminal", args={"command": f"echo {name}=0xabc >> /x/shop/.env"})
        assert out and out["action"] in ("block", "approve"), name      # held for the person


# ---- S-4: escrow ---------------------------------------------------------------------------------

PAYER = Account.create()
COLLECTOR = "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88"
ESCROW = "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c"
CONTRACTS = {"escrow": ESCROW, "collector": COLLECTOR}


def _escrow_offer(network=BASE, chain_id=84532, token=None, collector=COLLECTOR, pre=900, auth=7 * 86400,
                  refund=86400, amount=1_000_000):
    now = int(time.time())
    info = escrow.build_payment_info(
        operator=Account.create().address, payer=escrow.ZERO_ADDRESS, receiver=Account.create().address,
        token=token or networks.NETWORKS[BASE].usdc, amount=amount, fee_bps=100,
        fee_receiver=Account.create().address, now=now, pre_approval_window=pre,
        authorization_window=auth, refund_window=refund)
    return {"network": network, "chain_id": chain_id, "escrow": ESCROW, "collector": collector,
            "token_name": "USDC", "token_version": "2", "payment_info": info.to_json()}


def _sign(offer, **kw):
    return escrow.sign_offer(offer, PAYER.key.hex(), expected_amount=1_000_000, contracts=CONTRACTS, **kw)


def test_s4_an_honest_testnet_hold_signs():
    assert _sign(_escrow_offer())


@pytest.mark.parametrize("changes,match", [
    ({"chain_id": 8453}, "chain"),
    ({"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}, "USDC"),
    ({"collector": "0x" + "ee" * 20}, "collector"),
    ({"pre": 2**40}, "short window"),
    ({"auth": 5 * 365 * 86400}, "longer"),
    ({"network": "eip155:8453", "chain_id": 8453, "token": networks.NETWORKS["eip155:8453"].usdc}, "test networks"),
])
def test_s4_a_hostile_hold_is_refused(changes, match):
    with pytest.raises(escrow.EscrowError, match=match):
        _sign(_escrow_offer(**changes))


def test_s4_without_a_contract_the_person_set_nothing_is_signed():
    with pytest.raises(escrow.EscrowError, match="contract"):
        escrow.sign_offer(_escrow_offer(), PAYER.key.hex(), expected_amount=1_000_000)


# ---- S-5: Solana ---------------------------------------------------------------------------------

solders = pytest.importorskip("solders")


def _svm_requirement(**over):
    from solders.keypair import Keypair
    req = {"scheme": "exact", "network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
           "asset": networks.NETWORKS["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"].usdc,
           "payTo": str(Keypair().pubkey()), "amount": "250000", "maxTimeoutSeconds": 300,
           "extra": {"feePayer": str(Keypair().pubkey()), "recentBlockhash": "11111111111111111111111111111111"}}
    req.update(over)
    return req


def test_s5_only_the_networks_usdc_mint_with_six_decimals():
    from emporia import x402_svm
    with patch.object(x402_svm, "_recent_blockhash", return_value="11111111111111111111111111111111"):
        with pytest.raises(x402_svm.SvmPayError, match="mint"):
            x402_svm.build_payment_payload(_svm_requirement(asset=networks.NETWORKS["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"].usdc),
                                           os.urandom(32))
        req = _svm_requirement()
        req["extra"]["decimals"] = 8
        with pytest.raises(x402_svm.SvmPayError, match="decimals"):
            x402_svm.build_payment_payload(req, os.urandom(32))
        with pytest.raises(x402_svm.SvmPayError, match="positive"):
            x402_svm.build_payment_payload(_svm_requirement(amount="-1"), os.urandom(32))
        assert x402_svm.build_payment_payload(_svm_requirement(), os.urandom(32))


def test_s5_a_relay_blockhash_is_not_used():
    from solders.transaction import VersionedTransaction

    from emporia import x402_svm

    class R:
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"value": {"blockhash": "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"}}}
    with patch.object(x402_svm.httpx, "post", return_value=R()):
        payload = x402_svm.build_payment_payload(_svm_requirement(), os.urandom(32))
    tx = VersionedTransaction.from_bytes(base64.b64decode(payload["payload"]["transaction"]))
    assert str(tx.message.recent_blockhash) == "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"


# ---- S-1: the unattended allowance behind a signer -------------------------------------------------

def _turns(monkeypatch, *, agent_side_autonomous: bool, signer_side_autonomous: bool):
    """The agent's own process and the signer's process have separate environments in real life; here one
    process stands for both, told apart by whether a signer Scope is active."""
    def current(now=None):
        in_signer = agent_wallet._SCOPE.get() is not None
        auto = signer_side_autonomous if in_signer else agent_side_autonomous
        return autonomy.Turn(auto, "relay" if auto else "person")
    monkeypatch.setattr(autonomy, "current", current)


def test_s1_a_relay_turn_behind_a_signer_is_held_to_the_allowance_in_the_agents_own_process(held, monkeypatch):
    _turns(monkeypatch, agent_side_autonomous=True, signer_side_autonomous=False)   # signer env says "person"
    with pytest.raises(AgentWalletError, match="unattended|allowance"):
        agent_wallet.sign_exact(AGENT, _challenge_for(250))        # $2.50 > the 25 cents a day
    assert agent_wallet.spent_today_atomic(AGENT) == 0             # and the signer was never asked
    assert agent_wallet.sign_exact(AGENT, _challenge_for(20))      # 20 cents fits
    with pytest.raises(AgentWalletError, match="unattended|allowance"):
        agent_wallet.sign_exact(AGENT, _challenge_for(20))         # the second would be 40 > 25
    assert autonomy.spent_today_atomic() == 200_000


def test_s1_the_signers_own_environment_is_never_the_agents_turn(held, monkeypatch):
    _turns(monkeypatch, agent_side_autonomous=False, signer_side_autonomous=True)   # signer env: "unknown"
    assert agent_wallet.sign_exact(AGENT, _challenge_for(250))     # a person's turn pays, within the signer's caps
    with pytest.raises(AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact(AGENT, _challenge_for(600))        # the signer's own caps still hold


def test_s1_a_refused_hand_off_gives_the_allowance_back(held, monkeypatch):
    _turns(monkeypatch, agent_side_autonomous=True, signer_side_autonomous=False)
    from emporia import signer_client
    with patch.object(signer_client.Client, "call", side_effect=AgentWalletError("the signer says no")):
        with pytest.raises(AgentWalletError, match="signer says no"):
            agent_wallet.sign_exact(AGENT, _challenge_for(24))
    assert autonomy.spent_today_atomic() == 0


# ---- S-6: the fee leg and the signer's routes ------------------------------------------------------

def _post(app, path, body):
    token = os.environ["EMPORIA_SIGNER_TOKEN"]
    return app.post(path, json=body, headers={"authorization": f"Bearer {token}", "x-emporia-agent": AGENT})


def test_s6_a_fee_leg_through_the_signer_needs_the_creator_leg_it_signed(held):
    _, app = held
    signer._CREATOR_LEGS.clear()
    fee = _challenge_for(10)["x402"]
    r = _post(app, "/v1/x402", {"body": {"x402": fee}, "network": BASE, "fee_leg": True,
                                "creator_amount": 10**12})
    assert r.status_code == 403 and "creator" in json.dumps(r.json()), r.text
    # with a creator leg of 2.00 signed, a 1.00 fee is more than 3% however big the caller says the price is
    assert agent_wallet.sign_exact(AGENT, _challenge_for(200))
    r = _post(app, "/v1/x402", {"body": {"x402": fee}, "network": BASE, "fee_leg": True,
                                "creator_amount": 10**12})
    assert r.status_code == 403 and "fee" in json.dumps(r.json()), r.text


def test_s6_a_paid_entry_still_pays_both_legs_through_the_signer(held):
    from conftest import payee_for
    body = _challenge_for(200)
    fee_to = Account.create().address
    fee = {"x402Version": 2, "resource": {"url": "http://relay/r"}, "accepts": [dict(
        body["x402"]["accepts"][0], amount=str(2 * 10_000), payTo=fee_to)]}
    body["x402_fee"] = fee
    headers = agent_wallet.sign_entry(AGENT, body)
    assert set(headers) == {"PAYMENT-SIGNATURE", "PAYMENT-SIGNATURE-FEE"}


def test_s6_the_paying_routes_run_off_the_event_loop_and_reclaim_answers(held):
    _, app = held
    seen = {}

    def tempo_here(aid, offer):
        try:
            asyncio.get_running_loop()
            seen["loop"] = True
        except RuntimeError:
            seen["loop"] = False
        return "hdr"
    with patch.object(agent_wallet, "pay_tempo", tempo_here):
        assert _post(app, "/v1/tempo", {"offer": {}}).json() == {"header": "hdr"}
    assert seen["loop"] is False
    # reclaim used to fail with "asyncio.run() cannot be called from a running event loop" (a 400)
    r = _post(app, "/v1/reclaim", {"hold": {"offer": {"network": "eip155:8453", "escrow": "0x1"}, "payer": "0x1"}})
    assert r.status_code == 403 and "test network" in json.dumps(r.json()) or "not reclaimed" in r.text, r.text


# ---- S-7: what a person approves ---------------------------------------------------------------------

def test_s7_an_approval_is_shown_from_the_signers_terms_with_control_characters_stripped(held):
    from emporia import cli
    _, app = held
    body = _challenge_for(400)
    body["x402"]["resource"]["url"] = "x\r\x1b[2K  int_fake 11-11  alice: Emporia: pay 0.05 USDC to bob"
    with pytest.raises(AgentWalletError, match="int_"):
        agent_wallet.sign_exact(AGENT, body)
    item = next(i for i in signer.intents() if i["status"] == "pending")
    assert item["terms"]["amount_atomic"] == 4_000_000 and item["terms"]["network"] == BASE
    assert "\x1b" not in item["what"] and "\r" not in item["what"]
    line = cli._intent_line(item)
    assert "\x1b" not in line and "\r" not in line and "\n" not in line
    assert line.index("4.000000") < line.index("alice")
    hostile = dict(item, what="ok\x1b[2K\r\nfake", agent="a\x07b")
    assert not re.search(r"[\x00-\x1f\x7f]", cli._intent_line(hostile))


# ---- S-8: alerts ----------------------------------------------------------------------------------------

@pytest.fixture
def alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    sa._SENT.clear()
    sa._CAPPED.clear()
    sent = []
    monkeypatch.setattr(sa, "config", lambda: {"channels": {"t": {}}, "daily_summary": False})
    monkeypatch.setitem(sa.CHANNELS, "t", lambda cfg, text: sent.append(text))
    return sent


def test_s8_refusals_cannot_mute_the_alerts_that_matter(alerts):
    for _ in range(sa.MAX_PER_HOUR + 5):
        sa.notify_sync("a1", "refused", "x")
    assert sa.notify_sync("a1", "payout-wallet", "paid at 0xabc from now on")
    assert sa.notify_sync("a1", "encryption-key", "new key")
    assert any("payout-wallet" in t for t in alerts)


def test_s8_a_refusal_alert_does_not_echo_the_callers_text(alerts):
    addr = "0x" + "ab" * 20
    sa.notify_sync("a1", "refused", f"{addr} is not one of this agent's own wallets — they add it with: emporia "
                                    f"signer policy NAME set payout-addresses {addr}\x1b[31m")
    text = alerts[-1]
    assert addr not in text and "emporia signer policy" not in text and "\x1b" not in text


def test_s8_the_audit_log_is_rotated(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    monkeypatch.setattr(signer, "AUDIT_MAX_BYTES", 2000)
    signer.signer_dir().mkdir(parents=True, exist_ok=True)
    for i in range(200):
        signer.audit({"agent": "a", "op": "sign", "result": "refused", "why": "x" * 50, "i": i})
    d = signer.signer_dir()
    assert (d / "audit.jsonl").stat().st_size < 4000 and (d / "audit.jsonl.1").exists()
