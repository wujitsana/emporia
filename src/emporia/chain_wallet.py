"""An agent's side of setting an ERC-8004 identity's `agentWallet` to its own Emporia wallet (docs/protocol.md section
3.20). Run as the agent (its own settings) by `emporia agent set-8004-wallet`; a person's act, so there is no agent tool.

The registry's `setAgentWallet(agentId, newWallet, deadline, signature)`: the NEW wallet (here, the agent's own EVM
wallet) signs an EIP-712 consent (`erc8004_wallet.consent_typed_data`), and the token's OWNER sends the call. So:

* `build` signs the consent with the agent's wallet (the key never leaves the signer), proves it with an `eth_call`
  simulation from the owner (the registry itself checks owner, deadline and signature), and returns the call for the
  owner to send: printed, through `--browser`, or, when the agent's own wallet is the owner, by `send`.
* `send` is that last case, the same guards as `chain_register.send_register` (`--max-gas`, balance, one transaction).
* `confirm` reads the receipt at the `safe` block, then `getAgentWallet` there, and says whether the identity now
  names this agent's own wallet.

`agentWallet` says where the identity is paid. It is never proof of control: the link tiers say who controls it.
"""
from __future__ import annotations

import time
from typing import Any

from emporia import chain_link, chain_register, erc8004, networks
from emporia import erc8004_register as reg
from emporia import erc8004_wallet as W
from emporia.chain_link import LinkError
from emporia.chain_read import ChainRevert, ChainUnknown

EXPLAIN = ("This sets where the ERC-8004 identity says it is paid: the agent's own Emporia wallet. The registry needs "
           "two things: the new wallet's signed consent (made here, with the agent's own wallet; it can be used only "
           "for this identity, this owner and the next few minutes), and the identity's owner to send the call and pay "
           "its gas. The registry checks both. It changes no ownership and moves no funds. `agentWallet` is not proof "
           "of control; it only says where the identity is paid.")


def _target(chain: str, agent_id: Any) -> tuple[str, str, str]:
    caip2 = chain_register._chain(chain)
    return caip2, chain_link._agent_id(agent_id), networks.ERC8004_REGISTRIES[caip2]


def _read_owner(reader: Any, registry: str, agent_id: int) -> tuple[str, str]:
    """(owner, current agentWallet) at the reader's block, lowercase."""
    try:
        owner = W.decode_address(reader.call(registry, erc8004.SEL_OWNER_OF + agent_id.to_bytes(32, "big")))
        current = W.decode_address(reader.call(registry, W.get_wallet_calldata(agent_id)))
    except ChainRevert:
        raise LinkError(f"ERC-8004 agent #{agent_id} does not exist in that registry (yet: a new mint is visible here "
                        "after the chain's safe block)") from None
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be read ({e}); nothing changed") from None
    except Exception as e:  # noqa: BLE001 — a malformed answer is 'could not read', not a crash
        raise LinkError(f"the chain's answer was not understood ({type(e).__name__}); nothing changed") from None
    if owner == W.ZERO:
        raise LinkError(f"ERC-8004 agent #{agent_id} has no owner")
    return owner, current


def build(chain: str, agent_id: Any, *, read: Any = None, signer: Any = None, now: float | None = None) -> dict[str, Any]:
    """The consent, the call, and the registry's own answer to a simulation of it from the owner."""
    if not erc8004.available():
        raise LinkError("these checks need eth-account (the x402 extra): pip install 'emporia[x402]'")
    caip2, aid, registry = _target(chain, agent_id)
    chain_link.person_only("signing the consent with the agent's own wallet", chain_link.agent_profile())
    me, _ = chain_link._me(None)
    wallet = signer or chain_register.own_wallet()
    reader = chain_register._reader(caip2, read, "safe")
    owner, current = _read_owner(reader, registry, int(aid))
    new_wallet = wallet.address
    chain_id = int(caip2.split(":", 1)[1])
    try:
        sim = reader
        deadline = int(now if now is not None else sim.chain_time()) + W.DEADLINE_SECONDS
        td = W.consent_typed_data(chain_id, registry, aid, new_wallet, owner, deadline)
        try:
            sig_hex = wallet.sign_typed_data(td)
        except Exception as e:  # noqa: BLE001 — never echo a signer's words
            raise LinkError(f"the agent's wallet could not sign the consent ({type(e).__name__})") from None
        data = W.set_wallet_calldata(aid, new_wallet, deadline, bytes.fromhex(str(sig_hex).removeprefix("0x")))
        try:
            sim.simulate(registry, data, owner)
            gas = sim.estimate_gas(registry, data, owner)
        except ChainRevert as e:
            raise LinkError(f"the registry would refuse this call ({e}); nothing to send") from None
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be asked ({e}); nothing changed") from None
    except W.WalletError as e:
        raise LinkError(str(e)) from None
    return {"chain": caip2, "chain_label": networks.label(caip2), "chain_id": chain_id, "registry": registry, "agent": me,
            "agent_id": aid, "owner": owner, "new_wallet": new_wallet, "current_wallet": current,
            "already": W.same(current, new_wallet), "owner_is_agent_wallet": W.same(owner, new_wallet),
            "deadline": deadline, "valid_seconds": W.DEADLINE_SECONDS, "calldata": "0x" + data.hex(),
            "selector": "0x" + W.SEL_SET_AGENT_WALLET.hex(), "gas_estimate": gas, "explain": EXPLAIN,
            "simulated": True}


def quote(chain: str, agent_id: Any, max_gas: Any, *, read: Any = None, signer: Any = None, now: float | None = None) -> dict[str, Any]:
    """`build` for the case the agent's own wallet is the token's owner (and so sends), plus the cost quote."""
    from emporia import chain_send
    wallet = signer or chain_register.own_wallet()
    out = build(chain, agent_id, read=read, signer=wallet, now=now)
    if not out["owner_is_agent_wallet"]:
        raise LinkError(f"ERC-8004 agent #{out['agent_id']} is owned by {out['owner']}, not by this agent's wallet "
                        f"({wallet.address}): only the owner sends this. Print the call or use --browser, with the "
                        "owner's wallet")
    out["wallet"] = wallet.address
    out["quote"] = chain_send.quote(chain_register._reader(out["chain"], read, "safe"),
                                    wallet.address, out["registry"], bytes.fromhex(out["calldata"][2:]), max_gas,
                                    chain_id=out["chain_id"])
    return out


def send(chain: str, agent_id: Any, max_gas: Any, expect_owner: str, confirmed: dict[str, Any], *, read: Any = None, signer: Any = None,
         now: float | None = None) -> dict[str, Any]:
    """Quote again (fresh consent, fresh deadline), refuse if the owner is not the one the person confirmed, send one
    transaction from the agent's own wallet, signed with the gas limit and max fee the person confirmed (`confirmed`
    is the quote they were shown). The answer is the hash; `confirm` takes it from there."""
    from emporia import chain_send
    chain_link.person_only("sending from the agent's own wallet", chain_link.agent_profile())
    wallet = signer or chain_register.own_wallet()
    q = quote(chain, agent_id, max_gas, read=read, signer=wallet, now=now)
    pinned = chain_send.pin_to_confirmed(q["quote"], confirmed)
    if not W.same(q["owner"], expect_owner):
        raise LinkError("the identity's owner is not the wallet you confirmed: nothing sent; run the command again")
    tx_hash = chain_send.send(chain_register._reader(q["chain"], read, "safe"),
                              wallet, pinned, bytes.fromhex(q["calldata"][2:]))
    return {"tx_hash": tx_hash, "wallet": wallet.address, "chain": q["chain"], "chain_label": q["chain_label"],
            "max_cost_wei": pinned["max_cost_wei"]}


def confirm(chain: str, agent_id: Any, tx_hash: str, *, read: Any = None, signer: Any = None) -> dict[str, Any]:
    """The receipt of the owner's transaction at the `safe` block, then `getAgentWallet` there: what the identity now
    names, and whether that is this agent's own wallet."""
    caip2, aid, registry = _target(chain, agent_id)
    if why := reg.tx_problem(tx_hash):
        raise LinkError(why)
    tx_hash = tx_hash.strip().lower()
    me, _ = chain_link._me(None)
    reader = chain_register._reader(caip2, read, "safe")
    try:
        number = reader.block_number
        receipt = reader.receipt(tx_hash)
        if receipt is None:
            raise LinkError("that transaction is not mined yet (or the hash is wrong); try again in a minute")
        try:
            got = W.wallet_from_receipt(receipt, registry, tx_hash, int(aid))
        except W.WalletError as e:
            raise LinkError(str(e)) from None
        if got["block_number"] > number:
            raise LinkError(f"mined in block {got['block_number']}, but the chain's safe block is {number}: "
                            "wait a minute or two and run it again")
        if reader.block_hash_at(got["block_number"]).lower() != got["block_hash"]:
            raise LinkError("the receipt's block is not the chain's block at that height: not trusting it")
        _, current = _read_owner(reader, registry, int(aid))
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be read ({e}); nothing changed") from None
    except LinkError:
        raise
    except Exception as e:  # noqa: BLE001 — a malformed answer is 'could not read', not a crash
        raise LinkError(f"the chain's answer was not understood ({type(e).__name__}); nothing changed") from None
    try:
        mine = (signer or chain_register.own_wallet()).address
    except LinkError:
        mine = ""
    return {"chain": caip2, "chain_label": networks.label(caip2), "agent": me, "agent_id": aid, "tx": tx_hash,
            "block_number": got["block_number"], "block_hash": got["block_hash"], "agent_wallet": current,
            "set_by_tx": got["wallet"], "own_wallet": mine, "is_own_wallet": W.same(current, mine),
            "read_at_block": number, "read_at": int(time.time())}
