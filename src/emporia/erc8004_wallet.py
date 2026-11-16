"""The registry's `agentWallet` (docs/protocol.md section 3.20): the call, the consent the new wallet signs, the receipt.

Pure: no Hermes, no web framework, no network, no key. Read from the registry implementation's verified source
(`setAgentWallet`, OpenZeppelin EIP-712) and checked against the live registry with an `eth_call` simulation on
11-16 (emporia-notes/erc8004-live-check.md):

* `setAgentWallet(agentId, newWallet, deadline, signature)` is sent by the token's OWNER (or an approved operator).
  The NEW wallet signs the consent: EIP-712 domain `ERC8004IdentityRegistry` / `1` / chain id / the registry, primary
  type `AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)`. The owner is part of the
  message, so a consent holds for one owner only. The deadline must be at most 300 seconds ahead of the block.
* `getAgentWallet(agentId)` answers the zero address when it is unset (a transfer clears it).

`agentWallet` is where the identity says it is paid. It is never proof of control: the link tiers (`erc8004`) are.
"""
from __future__ import annotations

import re
from typing import Any

DOMAIN_NAME, DOMAIN_VERSION = "ERC8004IdentityRegistry", "1"
DEADLINE_SECONDS = 290            # the registry accepts at most 300 ahead of the block; the rest is margin
MAX_DEADLINE_SECONDS = 300

SEL_SET_AGENT_WALLET = bytes.fromhex("2d1ef5ae")      # setAgentWallet(uint256,address,uint256,bytes)
SEL_GET_AGENT_WALLET = bytes.fromhex("00339509")      # getAgentWallet(uint256)
# MetadataSet(uint256 indexed agentId, string indexed indexedMetadataKey, string metadataKey, bytes metadataValue)
TOPIC_METADATA_SET = "0x2c149ed548c6d2993cd73efe187df6eccabe4538091b33adbd25fafdb8a1468b"
SELECTORS = {"setAgentWallet(uint256,address,uint256,bytes)": SEL_SET_AGENT_WALLET,
             "getAgentWallet(uint256)": SEL_GET_AGENT_WALLET}
TYPE_STRING = "AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)"
ZERO = "0x" + "00" * 20
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")


class WalletError(Exception):
    """Said to the person as it is."""


def consent_typed_data(chain_id: int, registry: str, agent_id: int | str, new_wallet: str, owner: str,
                       deadline: int) -> dict:
    """The EIP-712 consent the NEW wallet signs: this wallet may be named as agent `agent_id`'s payment wallet,
    when `owner` holds the token, until `deadline`."""
    for what, value in (("new wallet", new_wallet), ("owner", owner), ("registry", registry)):
        if not _ADDR.fullmatch(value or ""):
            raise WalletError(f"the {what} is not an address")
    return {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
            "AgentWalletSet": [{"name": "agentId", "type": "uint256"}, {"name": "newWallet", "type": "address"},
                               {"name": "owner", "type": "address"}, {"name": "deadline", "type": "uint256"}],
        },
        "primaryType": "AgentWalletSet",
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": int(chain_id),
                   "verifyingContract": registry},
        "message": {"agentId": int(agent_id), "newWallet": new_wallet, "owner": owner, "deadline": int(deadline)},
    }


def set_wallet_calldata(agent_id: int | str, new_wallet: str, deadline: int, signature: bytes) -> bytes:
    from eth_abi import encode
    return SEL_SET_AGENT_WALLET + encode(["uint256", "address", "uint256", "bytes"],
                                         [int(agent_id), new_wallet, int(deadline), bytes(signature)])


def get_wallet_calldata(agent_id: int | str) -> bytes:
    from eth_abi import encode
    return SEL_GET_AGENT_WALLET + encode(["uint256"], [int(agent_id)])


def decode_address(data: bytes) -> str:
    """The address a `getAgentWallet` / `ownerOf` answer holds, lowercase (the zero address when unset)."""
    if len(data) < 32:
        raise WalletError("the registry's answer was not an address")
    return "0x" + bytes(data[12:32]).hex()


def wallet_from_receipt(receipt: dict | None, registry: str, tx_hash: str, agent_id: int | str) -> dict:
    """`{wallet, block_number, block_hash}` from the receipt of a successful `setAgentWallet` on `registry`: the
    registry's own `MetadataSet` log for this agent with the key `agentWallet`. Raises WalletError."""
    from eth_abi import decode
    from eth_utils import keccak
    if not isinstance(receipt, dict):
        raise WalletError("the chain has no receipt for that transaction yet (is the hash right, and is it mined?)")
    if str(receipt.get("transactionHash", "")).lower() != tx_hash.lower():
        raise WalletError("the receipt is for another transaction")
    if str(receipt.get("status", "")).lower() not in ("0x1", "1"):
        raise WalletError("that transaction failed on-chain: the agent wallet was not changed")
    if str(receipt.get("to") or "").lower() != registry.lower():
        raise WalletError("that transaction was not sent to this chain's ERC-8004 registry")
    want_key = "0x" + keccak(text="agentWallet").hex()
    found: list[str] = []
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict) or str(log.get("address", "")).lower() != registry.lower():
            continue
        topics = log.get("topics") or []
        try:
            if (len(topics) == 3 and str(topics[0]).lower() == TOPIC_METADATA_SET
                    and int(str(topics[1]), 16) == int(agent_id) and str(topics[2]).lower() == want_key):
                key, value = decode(["string", "bytes"], bytes.fromhex(str(log.get("data", "0x"))[2:]))
                if key == "agentWallet" and len(value) == 20:
                    found.append("0x" + value.hex())
        except (ValueError, TypeError, IndexError):
            continue
    if len(found) != 1:
        raise WalletError("that transaction did not set this agent's wallet in this registry")
    try:
        block = int(str(receipt.get("blockNumber")), 16)
    except (TypeError, ValueError):
        raise WalletError("the receipt names no block") from None
    return {"wallet": found[0], "block_number": block, "block_hash": str(receipt.get("blockHash", "")).lower()}


def same(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and bool(a) and a.lower() == b.lower()
