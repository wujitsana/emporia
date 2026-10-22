"""The escrow chain client (P98) against a fake JSON-RPC node.

What these pin: each call is ABI-encoded for the deployed contract's exact
signature, capture carries the pinned fee to the payer-signed fee receiver,
a revert raises instead of reading as success, operator transactions never
overlap (one nonce each), and a partly configured escrow is treated as absent.
"""
import asyncio
import os
from unittest.mock import patch

import pytest

pytest.importorskip("eth_account")

from eth_abi import decode  # noqa: E402
from eth_account import Account  # noqa: E402

from emporia import escrow, escrow_chain as ec  # noqa: E402
from emporia.operator_signer import LocalKeySigner  # noqa: E402

ENV = {
    "EMPORIA_ESCROW_NETWORK": "eip155:84532",
    "EMPORIA_ESCROW_RPC_URL": "https://rpc.example.test",
    "EMPORIA_ESCROW_CONTRACT": "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c",
    "EMPORIA_ESCROW_COLLECTOR": "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88",
    "EMPORIA_ESCROW_FEE_RECEIVER": "0x8d96f65aA58421F976D83743B28dbf4929998209",
    "EMPORIA_X402_ASSET_EIP155_84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "EMPORIA_X402_EXTRA_EIP155_84532": '{"name": "USDC", "version": "2"}',
}


class FakeNode:
    def __init__(self, *, revert=False):
        self.sent: list[bytes] = []
        self.nonces_seen: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.revert = revert
        self.nonce = 7

    async def __call__(self, cfg, method, params):
        if method == "eth_estimateGas":
            return hex(100_000)
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(1_000)}
        if method == "eth_maxPriorityFeePerGas":
            return hex(100)
        if method == "eth_getTransactionCount":
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.01)          # give a concurrent call a chance to overlap
            n = self.nonce
            self.nonces_seen.append(n)
            return hex(n)
        if method == "eth_sendRawTransaction":
            self.sent.append(bytes.fromhex(params[0][2:]))
            self.nonce += 1
            self.in_flight -= 1
            return "0x" + f"{len(self.sent):064x}"
        if method == "eth_getTransactionReceipt":
            return {"status": "0x0" if self.revert else "0x1"}
        raise AssertionError(method)


def _decoded_call(raw_tx: bytes) -> tuple[bytes, bytes]:
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes
    t = TypedTransaction.from_bytes(HexBytes(raw_tx))
    data = bytes(t.as_dict()["data"])
    return data[:4], data[4:]


@pytest.fixture
def cfg():
    with patch.dict(os.environ, ENV):
        yield ec.load_config()


def _info(cfg):
    return escrow.build_payment_info(
        operator="0x8B5bECa1Debad054342A128D285a26dbFd54d1d4",
        payer="0x72BE1b0EC70d02FAFCA849638B9218dd6464B382",
        receiver="0x1111111111111111111111111111111111111111",
        token=cfg.token, amount=1_000_000, fee_bps=250, fee_receiver=cfg.fee_receiver,
        now=1_800_000_000, pre_approval_window=3600, authorization_window=86400,
        refund_window=86400, salt=99)


def test_a_partial_configuration_is_no_configuration():
    for missing in ("EMPORIA_ESCROW_COLLECTOR", "EMPORIA_X402_EXTRA_EIP155_84532"):
        env = {k: v for k, v in ENV.items() if k != missing}
        with patch.dict(os.environ, env, clear=True):
            assert ec.load_config() is None, missing


def test_capture_sends_the_pinned_fee_to_the_signed_fee_receiver(cfg):
    node, signer = FakeNode(), LocalKeySigner(Account.create())
    info = _info(cfg)
    with patch.object(ec, "_rpc", new=node):
        asyncio.run(ec.capture(cfg, signer, info, 1_000_000))
    selector, args = _decoded_call(node.sent[0])
    assert selector == ec._SELECTORS["capture"]
    decoded_info, amount, fee, fee_receiver = decode(
        [ec._INFO_TUPLE, "uint256", "uint256", "address"], args)
    assert tuple(decoded_info)[2].lower() == info.receiver.lower()
    assert (amount, fee) == (1_000_000, 25_000)
    assert fee_receiver.lower() == cfg.fee_receiver.lower()


def test_authorize_names_the_collector_and_carries_the_payer_signature(cfg):
    node, signer = FakeNode(), LocalKeySigner(Account.create())
    with patch.object(ec, "_rpc", new=node):
        asyncio.run(ec.authorize(cfg, signer, _info(cfg), 1_000_000, b"\x01" * 65))
    selector, args = _decoded_call(node.sent[0])
    assert selector == ec._SELECTORS["authorize"]
    _, amount, collector, sig = decode([ec._INFO_TUPLE, "uint256", "address", "bytes"], args)
    assert (amount, collector.lower(), sig) == (1_000_000, cfg.collector.lower(), b"\x01" * 65)


def test_a_revert_is_an_error_not_a_success(cfg):
    node, signer = FakeNode(revert=True), LocalKeySigner(Account.create())
    with patch.object(ec, "_rpc", new=node):
        with pytest.raises(ec.EscrowChainError, match="reverted"):
            asyncio.run(ec.void(cfg, signer, _info(cfg)))


def test_operator_transactions_never_share_a_nonce(cfg):
    node, signer = FakeNode(), LocalKeySigner(Account.create())

    async def both():
        await asyncio.gather(ec.void(cfg, signer, _info(cfg)),
                             ec.capture(cfg, signer, _info(cfg), 1_000_000))
    with patch.object(ec, "_rpc", new=node):
        asyncio.run(both())
    assert node.max_in_flight == 1
    assert node.nonces_seen == [7, 8]
