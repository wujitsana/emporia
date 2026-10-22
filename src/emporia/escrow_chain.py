"""Commerce Payments escrow — the chain half: authorize, capture, void (P98).

Talks JSON-RPC to one network and signs as the operator (`operator_signer`).
`escrow.py` builds and hashes PaymentInfo; this module submits it.

Configuration, with no built-in addresses — a wrong contract or token address is
unrecoverable money, the rule `payments._x402_asset_for` already follows:

    EMPORIA_ESCROW_NETWORK       CAIP-2 id, e.g. eip155:84532 (Base Sepolia)
    EMPORIA_ESCROW_RPC_URL       JSON-RPC endpoint for that network
    EMPORIA_ESCROW_CONTRACT      AuthCaptureEscrow address
    EMPORIA_ESCROW_COLLECTOR     ERC3009PaymentCollector address
    EMPORIA_ESCROW_FEE_RECEIVER  where the operator fee goes (a Safe, ideally)

The token and its EIP-712 domain come from the existing x402 settings for the
same network (EMPORIA_X402_ASSET_<NET>, EMPORIA_X402_EXTRA_<NET>), so a network
has one answer to "which USDC, signed how".

Every operator transaction goes through one lock: nonces are sequential, and two
concurrent captures from one key would otherwise race for the same one.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from emporia import escrow

_INFO_TUPLE = ("(address,address,address,address,uint120,uint48,uint48,uint48,"
               "uint16,uint16,address,uint256)")
_SELECTORS = {
    "authorize": keccak(text=f"authorize({_INFO_TUPLE},uint256,address,bytes)")[:4],
    "capture": keccak(text=f"capture({_INFO_TUPLE},uint256,uint256,address)")[:4],
    "void": keccak(text=f"void({_INFO_TUPLE})")[:4],
    "paymentState": keccak(text="paymentState(bytes32)")[:4],
}
_TX_LOCK = asyncio.Lock()
_RECEIPT_TIMEOUT_S = 120.0


class EscrowChainError(RuntimeError):
    """A chain call failed or reverted. The message says which and why."""

    #: P111: the transaction, when one was sent. A missing receipt is not a
    #: failed transaction — it may still land — so a caller that sees this set
    #: reads the payment's state before deciding anything.
    tx_hash: str | None = None


@dataclass(frozen=True)
class EscrowConfig:
    network: str
    chain_id: int
    rpc_url: str
    contract: str
    collector: str
    token: str
    token_name: str
    token_version: str
    fee_receiver: str


def load_config() -> EscrowConfig | None:
    """The escrow configuration, or None when it is not fully set — a partly
    configured escrow behaves as an absent one, never as a guessed one."""
    from emporia.payments import _x402_asset_for, _x402_extra_for

    network = os.getenv("EMPORIA_ESCROW_NETWORK", "").strip()
    values = {
        "rpc_url": os.getenv("EMPORIA_ESCROW_RPC_URL", "").strip(),
        "contract": os.getenv("EMPORIA_ESCROW_CONTRACT", "").strip(),
        "collector": os.getenv("EMPORIA_ESCROW_COLLECTOR", "").strip(),
        "fee_receiver": os.getenv("EMPORIA_ESCROW_FEE_RECEIVER", "").strip(),
    }
    if not network.startswith("eip155:") or not all(values.values()):
        return None
    token = _x402_asset_for(network)
    extra = _x402_extra_for(network) or {}
    if not token or not extra.get("name") or not extra.get("version"):
        return None
    return EscrowConfig(
        network=network, chain_id=int(network.split(":", 1)[1]), rpc_url=values["rpc_url"],
        contract=to_checksum_address(values["contract"]),
        collector=to_checksum_address(values["collector"]),
        token=to_checksum_address(token), token_name=str(extra["name"]),
        token_version=str(extra["version"]),
        fee_receiver=to_checksum_address(values["fee_receiver"]),
    )


async def _rpc(cfg: EscrowConfig, method: str, params: list) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(cfg.rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                                 "method": method, "params": params})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise EscrowChainError(f"{method} failed: {body['error']}")
    return body["result"]


async def payment_state(cfg: EscrowConfig, info: escrow.PaymentInfo) -> dict[str, int | bool]:
    """`paymentState(hash)`: whether funds were collected, what is still
    capturable, what was captured and is refundable."""
    h = escrow.payment_info_hash(info, chain_id=cfg.chain_id, escrow=cfg.contract)
    raw = await _rpc(cfg, "eth_call", [{"to": cfg.contract,
                                         "data": "0x" + (_SELECTORS["paymentState"] + h).hex()},
                                        "latest"])
    collected, capturable, refundable = decode(["bool", "uint120", "uint120"],
                                               bytes.fromhex(raw[2:]))
    return {"has_collected_payment": collected, "capturable": capturable,
            "refundable": refundable}


async def _send(cfg: EscrowConfig, signer: Any, data: bytes) -> str:
    """Sign and submit one operator call to the escrow, wait for it, and return
    the transaction hash. A revert raises; so does a receipt that never comes."""
    async with _TX_LOCK:
        call = {"from": signer.address, "to": cfg.contract, "data": "0x" + data.hex()}
        gas = int(int(await _rpc(cfg, "eth_estimateGas", [call]), 16) * 1.25)
        block = await _rpc(cfg, "eth_getBlockByNumber", ["latest", False])
        base_fee = int(block.get("baseFeePerGas") or "0x0", 16)
        tip = int(await _rpc(cfg, "eth_maxPriorityFeePerGas", []), 16)
        tx = {
            "type": 2, "chainId": cfg.chain_id, "to": cfg.contract, "value": 0, "gas": gas,
            "data": "0x" + data.hex(),
            "nonce": int(await _rpc(cfg, "eth_getTransactionCount",
                                    [signer.address, "pending"]), 16),
            "maxFeePerGas": base_fee * 2 + tip, "maxPriorityFeePerGas": tip,
        }
        tx_hash = await _rpc(cfg, "eth_sendRawTransaction",
                             ["0x" + signer.sign_transaction(tx).hex()])
    deadline = asyncio.get_running_loop().time() + _RECEIPT_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        receipt = await _rpc(cfg, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if int(receipt["status"], 16) != 1:
                err = EscrowChainError(f"transaction {tx_hash} reverted")
                err.tx_hash = tx_hash
                raise err
            return tx_hash
        await asyncio.sleep(2)
    err = EscrowChainError(f"no receipt for {tx_hash} within {_RECEIPT_TIMEOUT_S:.0f}s; "
                           f"check it before retrying")
    err.tx_hash = tx_hash
    raise err


async def authorize(cfg: EscrowConfig, signer: Any, info: escrow.PaymentInfo,
                    amount: int, payer_signature: bytes) -> str:
    """Pull the payer's funds into escrow, using their ERC-3009 signature."""
    data = _SELECTORS["authorize"] + encode(
        [_INFO_TUPLE, "uint256", "address", "bytes"],
        [info.as_tuple(), amount, cfg.collector, payer_signature])
    return await _send(cfg, signer, data)


async def capture(cfg: EscrowConfig, signer: Any, info: escrow.PaymentInfo, amount: int) -> str:
    """Release the hold to its receiver, less the pinned fee to the fee receiver."""
    data = _SELECTORS["capture"] + encode(
        [_INFO_TUPLE, "uint256", "uint256", "address"],
        [info.as_tuple(), amount, escrow.fee_amount(info, amount), info.fee_receiver])
    return await _send(cfg, signer, data)


async def void(cfg: EscrowConfig, signer: Any, info: escrow.PaymentInfo) -> str:
    """Return the whole hold to the payer."""
    data = _SELECTORS["void"] + encode([_INFO_TUPLE], [info.as_tuple()])
    return await _send(cfg, signer, data)
