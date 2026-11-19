"""Sending one transaction from an agent's own EVM wallet, for the ERC-8004 commands (docs/protocol.md section 3.20).
This is the one place Emporia broadcasts a transaction (`eth_sendRawTransaction`): person-run, Base Sepolia, the agent's wallet.

The wallet is the agent's Emporia EVM wallet (`agent_wallet.evm_signer`: the key is read only inside the signer's
own `sign_transaction` and is never printed, logged or put in an error). This is a person's act, run by
`emporia agent register-8004 | set-8004-wallet --from-agent-wallet`: there is no agent tool for it, and the commands
refuse an agent's terminal, a live unattended turn, and every chain but Base Sepolia (`SEND_CHAINS`).

* `quote` is read-only: the gas estimate (a revert means the registry would refuse), the person's `--max-gas` as a hard
  cap, EIP-1559 fees (priority fee plus twice the base fee, never above `MAX_FEE_CEILING`), the wallet's balance and
  its next nonce. It refuses when the balance cannot cover the worst case.
* `send` signs and broadcasts exactly one transaction. It computes the hash itself, so if the answer is lost the
  person still has the hash to read with `--tx`.
"""
from __future__ import annotations

import sys
from typing import Any

from emporia import networks
from emporia.chain_link import LinkError
from emporia.chain_read import ChainRevert, ChainRpcError, ChainUnknown

SEND_CHAINS = ("eip155:84532",)          # the only chain an agent wallet sends on: test ETH only
GAS_MARGIN_NUM, GAS_MARGIN_DEN = 5, 4    # the limit is the estimate plus a quarter, never above --max-gas
HASH_MARK = "emporia-tx-hash: "         # the line the send writes to stderr before it broadcasts (the parent reads it on a timeout)
MAX_FEE_CEILING = 50 * 10**9             # wei per gas: a node that quotes more than this is not believed


def eth(wei: int) -> str:
    return f"{wei / 10**18:.8f}".rstrip("0").rstrip(".") + " ETH" if wei else "0 ETH"


def gwei(wei: int) -> str:
    return f"{wei / 10**9:.4f}".rstrip("0").rstrip(".") + " gwei"


def max_gas_of(value: Any) -> int:
    try:
        n = int(str(value).strip().replace("_", ""))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        raise LinkError("--max-gas is required to send from the agent's wallet: the most gas this transaction may use "
                        "(a whole number; the quote shows the estimate)")
    return n


def quote(reader: Any, sender: str, to: str, data: bytes, max_gas: Any, *, chain_id: int) -> dict[str, Any]:
    """What sending `data` to `to` from `sender` would cost at most. Raises LinkError with the reason, never sends."""
    if f"eip155:{chain_id}" not in SEND_CHAINS or not networks.is_testnet(f"eip155:{chain_id}"):
        raise LinkError("an agent's own wallet sends on Base Sepolia (testnet) only for now")
    cap = max_gas_of(max_gas)
    try:
        reader.pin()                    # the chain id is checked on this very reader, whatever built the call
    except AttributeError:
        pass
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be asked ({e}); nothing changed") from None
    try:
        estimate = reader.estimate_gas(to, data, sender)
        priority, base = reader.fee_data()
        balance = reader.balance(sender)
        nonce = reader.nonce(sender)
        mined = reader.nonce(sender, "latest")
    except ChainRevert as e:
        raise LinkError(f"the registry would refuse this call ({e}); nothing to send") from None
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be asked ({e}); nothing changed") from None
    if nonce > mined:
        raise LinkError(f"the wallet {sender} has a transaction waiting ({nonce - mined} not yet mined): an earlier send may "
                        "still be in flight. Let it finish and read it with --tx (the explorer lists it under that "
                        "address) before sending another")
    if estimate > cap:
        raise LinkError(f"the call needs about {estimate:,} gas, over your --max-gas {cap:,}: raise --max-gas if you "
                        "accept that, or stop here")
    gas_limit = min(cap, max(estimate, estimate * GAS_MARGIN_NUM // GAS_MARGIN_DEN))
    max_fee = priority + 2 * base
    if max_fee > MAX_FEE_CEILING:
        raise LinkError(f"the node quotes a fee of {gwei(max_fee)} per gas, above the {gwei(MAX_FEE_CEILING)} this "
                        "command will pay: try again later")
    cost = gas_limit * max_fee
    if balance < cost:
        raise LinkError(f"the wallet {sender} holds {eth(balance)}, and the worst case here is {eth(cost)} "
                        f"({gas_limit:,} gas at {gwei(max_fee)}): get Base Sepolia test ETH for {sender}")
    return {"sender": sender, "chain_id": int(chain_id), "to": to, "gas_estimate": estimate, "max_gas": cap,
            "gas_limit": gas_limit, "max_fee_per_gas": max_fee, "priority_fee_per_gas": priority, "base_fee": base,
            "balance_wei": balance, "max_cost_wei": cost, "nonce": nonce}


def pin_to_confirmed(fresh: dict[str, Any], confirmed: Any) -> dict[str, Any]:
    """The quote to sign: the gas limit and fees the person confirmed, checked against a fresh quote of the same call.
    A rise in the fee is not a reason to refuse by itself: the confirmed max fee already has room (twice the base fee).
    It refuses only when it could not be included or would not fit: the fresh base fee is above the confirmed max fee,
    the call now needs more gas than the confirmed limit, or the wallet cannot cover the confirmed worst case."""
    try:
        limit, fee = int(confirmed["gas_limit"]), int(confirmed["max_fee_per_gas"])
        priority, cost = int(confirmed["priority_fee_per_gas"]), int(confirmed["max_cost_wei"])
    except (TypeError, ValueError, KeyError):
        raise LinkError("the confirmed cost is missing: nothing sent; run the command again") from None
    if limit <= 0 or fee <= 0 or priority < 0 or limit * fee != cost or fee > MAX_FEE_CEILING:
        raise LinkError("the confirmed cost does not add up: nothing sent; run the command again")
    if fresh["base_fee"] > fee:
        raise LinkError(f"the base fee is now {gwei(fresh['base_fee'])}, above the max fee {gwei(fee)} you confirmed, so "
                        "the transaction would not be included: nothing sent; run the command again")
    if fresh["gas_estimate"] > limit:
        raise LinkError(f"the call now needs about {fresh['gas_estimate']:,} gas, over the limit {limit:,} you confirmed: "
                        "nothing sent; run the command again")
    if fresh["balance_wei"] < cost:
        raise LinkError(f"the wallet holds {eth(fresh['balance_wei'])}, less than the {eth(cost)} worst case you "
                        "confirmed: nothing sent")
    return {**fresh, "gas_limit": limit, "max_fee_per_gas": fee, "priority_fee_per_gas": min(priority, fee),
            "max_cost_wei": cost}


def describe(q: dict[str, Any]) -> list[str]:
    """The lines the person reads before the y/N."""
    return [f"from:        {q['sender']}  (the agent's own wallet; holds {eth(q['balance_wei'])})",
            f"gas:         about {q['gas_estimate']:,} estimated; limit {q['gas_limit']:,} (your --max-gas {q['max_gas']:,})",
            f"max fee:     {gwei(q['max_fee_per_gas'])} per gas, so at most {eth(q['max_cost_wei'])} of test ETH",
            f"nonce:       {q['nonce']}"]


def send(reader: Any, signer: Any, q: dict[str, Any], data: bytes) -> str:
    """Sign and broadcast the one transaction `q` quoted; the hash. A failure never carries key material: only the
    kind of error is said, and the hash computed here, in case the node did get it."""
    from eth_utils import keccak

    from emporia.chain_link import agent_profile, person_only
    person_only("sending from the agent's own wallet", agent_profile())
    if f"eip155:{q['chain_id']}" not in SEND_CHAINS:
        raise LinkError("an agent's own wallet sends on Base Sepolia (testnet) only for now")
    tx = {"type": 2, "chainId": q["chain_id"], "nonce": q["nonce"], "to": q["to"], "value": 0, "data": "0x" + data.hex(),
          "gas": q["gas_limit"], "maxFeePerGas": q["max_fee_per_gas"], "maxPriorityFeePerGas": q["priority_fee_per_gas"]}
    try:
        raw = bytes(signer.sign_transaction(tx))
    except Exception as e:  # noqa: BLE001 — never put the signer's own words in front of anyone: they may echo a key
        raise LinkError(f"the wallet could not sign ({type(e).__name__}); nothing sent") from None
    local = "0x" + keccak(raw).hex()
    print(f"{HASH_MARK}{local}", file=sys.stderr, flush=True)   # before the broadcast: a hang after it still leaves the hash
    try:
        got = reader.send_raw(raw)
    except (ChainRevert, ChainRpcError) as e:
        raise LinkError(f"the node refused the transaction ({e}); nothing was sent") from None
    except ChainUnknown as e:
        raise LinkError(f"the node did not confirm the send ({e}). It may still have arrived: check {local} "
                        f"on the explorer, then run the command again with --tx {local}") from None
    if got != local:
        raise LinkError(f"the node answered a different hash ({got}) than the one signed ({local}); check both "
                        "on the explorer before running anything again")
    return got
