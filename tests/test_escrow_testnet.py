"""Live: x402 escrow holds for service purchases on Base Sepolia (P101).

The opt-in `escrow` lane of scripts/testnet.py. A real relay subprocess, the
deployed Commerce Payments contracts, and test USDC:

  A. fund -> deliver -> confirm: the hold is captured; the seller's wallet gets
     the stake less the fee, the fee receiver gets the fee, to the unit.
  B. fund -> deliver -> dispute: the hold is voided; the payer gets it all back.

First run passed 2026-10-22 (tx hashes recorded in docs/security-log.md, P96-P99 pass).

Keys come from files, never from hex in the environment:

    EMPORIA_TESTNET_OPERATOR_KEY_FILE   escrow operator; needs a little Base Sepolia ETH
    EMPORIA_TESTNET_PAYER_KEY_FILE      payer; needs >= 2 test USDC (no ETH)
    EMPORIA_TESTNET_FEE_RECEIVER        optional; a fresh address when unset
"""
import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.testnet

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402

RPC = os.getenv("EMPORIA_X402_RPC_EIP155_84532", "https://sepolia.base.org")
CHAIN = 84532
USDC = os.getenv("EMPORIA_TESTNET_ASSET", "0x036CbD53842c5426634e7929541eC2318f3dCF7e")
ESCROW = os.getenv("EMPORIA_TESTNET_ESCROW_CONTRACT", "")
COLLECTOR = os.getenv("EMPORIA_TESTNET_ESCROW_COLLECTOR", "")


def _key(env: str) -> Account:
    path = os.getenv(env, "").strip()
    if not path:
        pytest.skip(f"{env} is not set")
    return Account.from_key(Path(path).expanduser().read_bytes())


def _rpc(method, params):
    import httpx
    r = httpx.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                   timeout=30).json()
    if "error" in r:
        raise RuntimeError(f"{method}: {r['error']}")
    return r["result"]


def _usdc(addr: str) -> int:
    data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
    return int(_rpc("eth_call", [{"to": USDC, "data": data}, "latest"]), 16)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    import httpx

    if not (ESCROW and COLLECTOR):
        pytest.skip("escrow contract addresses not configured (run via scripts/testnet.py)")
    operator = _key("EMPORIA_TESTNET_OPERATOR_KEY_FILE")
    payer = _key("EMPORIA_TESTNET_PAYER_KEY_FILE")
    fee_receiver = os.getenv("EMPORIA_TESTNET_FEE_RECEIVER") or Account.create().address
    if _usdc(payer.address) < 2_000_000:
        pytest.skip(f"payer {payer.address} needs >= 2 test USDC")

    tmp = tmp_path_factory.mktemp("escrow_live")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ,
           "EMPORIA_DB_PATH": str(tmp / "relay.sqlite3"), "EMPORIA_LOG_DIR": str(tmp / "logs"),
           "EMPORIA_AUDIT_DIR": str(tmp / "audit"), "EMPORIA_KEYS_DIR": str(tmp / "relay_keys"),
           "EMPORIA_RELAY_PORT": str(port), "EMPORIA_RELAY_URL": url,
           "EMPORIA_TRUST_LOCAL_HEADER_AUTH": "0", "STRIPE_SECRET_KEY": "",
           "EMPORIA_X402_ENABLED": "0",
           "EMPORIA_ESCROW_NETWORK": f"eip155:{CHAIN}", "EMPORIA_ESCROW_RPC_URL": RPC,
           "EMPORIA_ESCROW_CONTRACT": ESCROW, "EMPORIA_ESCROW_COLLECTOR": COLLECTOR,
           "EMPORIA_ESCROW_FEE_RECEIVER": fee_receiver,
           "EMPORIA_X402_ASSET_EIP155_84532": USDC,
           "EMPORIA_X402_EXTRA_EIP155_84532": '{"name": "USDC", "version": "2"}',
           "EMPORIA_OPERATOR_SIGNER": "env", "EMPORIA_OPERATOR_PRIVATE_KEY": operator.key.hex(),
           "EMPORIA_OPERATOR_ADDRESS": operator.address}
    relay = subprocess.Popen([sys.executable, "-m", "emporia.relay_server"], env=env,
                             stdout=open(tmp / "relay.out", "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            pytest.fail(f"relay did not start; see {tmp / 'relay.out'}")
        yield {"url": url, "payer": payer, "fee_receiver": fee_receiver,
               "db": env["EMPORIA_DB_PATH"]}
    finally:
        relay.terminate()
        relay.wait(timeout=10)


async def _purchase(live, seller_wallet, *, dispute):
    import uuid

    from emporia.agent_sdk import EmporiaAgent
    from emporia.identity import get_public_key_hex

    url, payer = live["url"], live["payer"]
    buyer_id, seller_id = f"lb_{uuid.uuid4().hex[:8]}", f"ls_{uuid.uuid4().hex[:8]}"
    async with EmporiaAgent(url, buyer_id, get_public_key_hex(buyer_id),
                            x402_private_key=payer.key.hex(),
                            x402_networks=[f"eip155:{CHAIN}"]) as buyer, \
               EmporiaAgent(url, seller_id, get_public_key_hex(seller_id)) as seller:
        await buyer.register()
        await seller.register()
        r = await seller._bearer_request("post", f"{url}/agents/{seller_id}/x402-wallet",
                                         json={"network_family": "evm", "address": seller_wallet})
        r.raise_for_status()
        sid = (await buyer.create_session(
            "emporia:service:v1", config={"description": "live escrow lane"},
            payment_rules={"mode": "stripe_link", "stake_per_participant": "1.00",
                           "currency": "usd"}))["session_id"]
        await seller.join_session(sid)
        assert (await buyer.fund_session(sid, pay_with_x402=True))["status"] == "active"
        await seller.submit_action(sid, "accept", {}, "Accepting this live escrow lane job")
        await seller.submit_action(sid, "deliver", {"deliverable": "ok"},
                                   "Delivering the agreed lane output")
        if dispute:
            await buyer.submit_action(sid, "dispute", {"reason": "void path"},
                                      "Disputing on purpose to exercise the void path")
        else:
            await buyer.submit_action(sid, "confirm", {}, "Confirming the delivery is acceptable")
        return sid


def _hold(live, sid):
    conn = sqlite3.connect(live["db"])
    conn.row_factory = sqlite3.Row
    [row] = conn.execute("SELECT * FROM escrow_holds WHERE session_id=?", (sid,)).fetchall()
    return dict(row)


def test_testnet_escrow_confirm_captures_to_the_seller_less_the_fee(live):
    seller_wallet = Account.create().address
    fee_before, payer_before = _usdc(live["fee_receiver"]), _usdc(live["payer"].address)
    sid = asyncio.run(_purchase(live, seller_wallet, dispute=False))
    hold = _hold(live, sid)
    print(f"authorize {hold['authorize_tx']}  capture {hold['capture_tx']}")
    assert hold["state"] == "captured"
    assert _usdc(seller_wallet) == 975_000
    assert _usdc(live["fee_receiver"]) - fee_before == 25_000
    assert payer_before - _usdc(live["payer"].address) == 1_000_000


def test_testnet_escrow_dispute_voids_back_to_the_payer(live):
    payer_before = _usdc(live["payer"].address)
    sid = asyncio.run(_purchase(live, Account.create().address, dispute=True))
    hold = _hold(live, sid)
    print(f"authorize {hold['authorize_tx']}  void {hold['void_tx']}")
    assert hold["state"] == "voided"
    assert _usdc(live["payer"].address) == payer_before
