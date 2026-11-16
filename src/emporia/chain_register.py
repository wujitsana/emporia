"""An agent's side of ERC-8004 registering, the own-wallet path (docs/protocol.md section 3.20): build the call a
person sends from their own wallet, then read the receipt and find the new agent's number. Run as the agent (its own
settings) by `emporia agent register-8004`; it is a person's act, so there is no agent tool for it.

Emporia sends nothing here and holds no key: the transaction is the person's, from their wallet. Reads go through
`chain_read.PinnedReader`; the call is checked first with an `eth_call` simulation and a gas estimate.
"""
from __future__ import annotations

from typing import Any

from emporia import chain_link, erc8004, networks
from emporia import erc8004_register as reg
from emporia.chain_link import LinkError
from emporia.chain_read import ChainRevert, ChainUnknown, PinnedReader

# A stand-in sender for the simulation and the estimate when the person gives none: the registry mints to whoever
# calls, so any address answers the same. It is never used for anything else.
PLACEHOLDER_SENDER = "0x000000000000000000000000000000000000dEaD"
EXPLAIN = ("Registering gives this Emporia agent an ERC-8004 identity: one token in the chain's public identity "
           "registry, minted to the wallet that sends the transaction (that wallet owns it). The token's registration "
           "file (below) is stored on-chain and names this agent. Emporia sends nothing and spends nothing: you send "
           "the transaction from your own wallet and pay its gas in test ETH. Afterwards the registration is public.")


def _chain(name: str) -> str:
    caip2 = chain_link.chain_of(name)
    if why := reg.chain_problem(caip2):
        raise LinkError(why)
    return caip2


def _reader(caip2: str, read: Any = None, tag: str = "finalized") -> Any:
    if read is not None:
        return read
    try:
        return PinnedReader(caip2, tag=tag)
    except ChainUnknown as e:
        raise LinkError(str(e)) from None


def _sender(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return PLACEHOLDER_SENDER
    chain_link._signer(value)       # same address check as --signer
    return value


def _x402_support() -> bool:
    """Whether the agent has an EVM wallet set up (no key is read or made)."""
    try:
        from emporia import agent_wallet, mcp_server as m
        return "evm" in agent_wallet._families(m.AGENT_ID)
    except Exception:  # noqa: BLE001 — any failure answers 'no wallet'; the registration just says x402Support false
        return False


def _about() -> str:
    try:
        from emporia import identity_card
        return identity_card.about() or ""
    except Exception:  # noqa: BLE001 — no bio is fine; the registration then says what the agent is
        return ""


def build(chain: str, sender: str = "", *, read: Any = None, stable: str | None = None, about: str | None = None,
          relay: str | None = None, x402: bool | None = None, image: str | None = None) -> dict[str, Any]:
    """The registration file, the call and what the chain says about it (a simulation and a gas estimate)."""
    if not erc8004.available():
        raise LinkError("these checks need eth-account (the x402 extra): pip install 'emporia[x402]'")
    caip2 = _chain(chain)
    me, stable_id = chain_link._me(stable)
    if relay is None:
        from emporia import mcp_server as m
        relay = m.RELAY_URL
    try:
        file = reg.registration_file(me, _about() if about is None else about, relay, stable_id,
                                     _x402_support() if x402 is None else x402, image)
        uri = reg.data_uri(file)
    except reg.RegisterError as e:
        raise LinkError(str(e)) from None
    registry = networks.ERC8004_REGISTRIES[caip2]
    data = reg.calldata(uri)
    who = _sender(sender)
    reader = _reader(caip2, read)
    out: dict[str, Any] = {"chain": caip2, "chain_label": networks.label(caip2), "chain_id": int(caip2.split(":", 1)[1]),
                           "registry": registry, "agent": me, "stable_id": stable_id, "file": file, "agent_uri": uri,
                           "calldata": "0x" + data.hex(), "selector": "0x" + reg.SEL_REGISTER_URI.hex(),
                           "sender_for_estimate": who, "explain": EXPLAIN, "relay": relay,
                           "local_relay": _is_local(relay)}
    try:
        out["would_register_id"] = str(reg.decode_registered_id(reader.simulate(registry, data, who)))
        out["gas_estimate"] = reader.estimate_gas(registry, data, who)
    except ChainRevert as e:
        raise LinkError(f"the registry would refuse this call ({e}); nothing to send") from None
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be asked ({e}); nothing changed") from None
    except Exception as e:  # noqa: BLE001 — a malformed answer is 'could not ask', not a crash
        raise LinkError(f"the chain's answer was not understood ({type(e).__name__}); nothing changed") from None
    return out


def own_wallet() -> Any:
    """The agent's own EVM signer (`.address`, `.sign_transaction`, `.sign_typed_data`), or a LinkError saying what is
    missing. Behind the operator signer the keys are not here, and its limits and approvals are the way to spend."""
    from emporia import agent_wallet, mcp_server as m
    if agent_wallet.remote() is not None:
        raise LinkError("this agent's wallet keys are held by the operator signer, which sends nothing for the "
                        "ERC-8004 commands: use your own wallet (print the call, or --browser)")
    try:
        return agent_wallet.evm_signer(m.AGENT_ID)
    except agent_wallet.AgentWalletError as e:
        raise LinkError(f"{e}") from None
    except Exception as e:  # noqa: BLE001 — never echo a signer's own words: they may carry key material
        raise LinkError(f"this agent's EVM wallet could not be opened ({type(e).__name__})") from None


def _digest(data: bytes) -> str:
    from eth_utils import keccak
    return "0x" + keccak(data).hex()


def quote_register(chain: str, max_gas: Any, *, read: Any = None, signer: Any = None, stable: str | None = None,
                   about: str | None = None, relay: str | None = None, x402: bool | None = None,
                   image: str | None = None) -> dict[str, Any]:
    """`build` with the agent's own wallet as the sender, plus what sending it would cost at most (read-only):
    the answer of `chain_send.quote` under `quote`, and the calldata's digest the send must match."""
    from emporia import chain_send
    if not erc8004.available():
        raise LinkError("these checks need eth-account (the x402 extra): pip install 'emporia[x402]'")
    _chain(chain)
    wallet = signer or own_wallet()
    out = build(chain, wallet.address, read=read, stable=stable, about=about, relay=relay, x402=x402, image=image)
    data = bytes.fromhex(out["calldata"][2:])
    out["wallet"] = wallet.address
    out["calldata_digest"] = _digest(data)
    out["quote"] = chain_send.quote(_reader(out["chain"], read), wallet.address, out["registry"], data, max_gas,
                                    chain_id=out["chain_id"])
    return out


def send_register(chain: str, max_gas: Any, expect_digest: str, *, read: Any = None, signer: Any = None,
                  stable: str | None = None, about: str | None = None, relay: str | None = None,
                  x402: bool | None = None, image: str | None = None) -> dict[str, Any]:
    """Quote again, refuse if the call is no longer the one the person confirmed, then send exactly one transaction
    from the agent's own wallet. The answer is the hash; `confirm` takes it from there."""
    from emporia import chain_send
    wallet = signer or own_wallet()
    q = quote_register(chain, max_gas, read=read, signer=wallet, stable=stable, about=about, relay=relay, x402=x402,
                       image=image)
    if q["calldata_digest"] != expect_digest:
        raise LinkError("the registration changed since you confirmed it (the bio or the relay?): nothing sent; "
                        "run the command again")
    data = bytes.fromhex(q["calldata"][2:])
    tx_hash = chain_send.send(_reader(q["chain"], read), wallet, q["quote"], data)
    return {"tx_hash": tx_hash, "wallet": wallet.address, "chain": q["chain"], "chain_label": q["chain_label"],
            "max_cost_wei": q["quote"]["max_cost_wei"]}


def _is_local(url: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local")


def confirm(chain: str, tx_hash: str, *, read: Any = None, finalized: Any = None,
            stable: str | None = None) -> dict[str, Any]:
    """Read the receipt of the person's transaction at the `safe` block, find the agent number in the registry's own
    `Registered` event, and check the token's registration names this agent's stable id (the two-way link).
    `finalized` is a second reader (the `finalized` tag) only to say how long link-8004 has to wait."""
    caip2 = _chain(chain)
    if why := reg.tx_problem(tx_hash):
        raise LinkError(why)
    tx_hash = tx_hash.strip().lower()
    me, stable_id = chain_link._me(stable)
    registry = networks.ERC8004_REGISTRIES[caip2]
    reader = _reader(caip2, read, "safe")
    try:
        number = reader.block_number              # the safe block: pinned once, every read below is at it
        receipt = reader.receipt(tx_hash)
        if receipt is None:
            raise LinkError("that transaction is not mined yet (or the hash is wrong); try again in a minute")
        try:
            found = reg.agent_from_receipt(receipt, registry, tx_hash)
        except reg.RegisterError as e:
            raise LinkError(str(e)) from None
        if found["block_number"] > number:
            raise LinkError(f"mined in block {found['block_number']}, but the chain's safe block is {number}: "
                            "wait a minute or two and run it again")
        if reader.block_hash_at(found["block_number"]).lower() != found["block_hash"]:
            raise LinkError("the receipt's block is not the chain's block at that height: not trusting it")
        agent_id = int(found["agent_id"])
        owner_now = erc8004._addr(reader.call(registry, erc8004.SEL_OWNER_OF + agent_id.to_bytes(32, "big")))
        uri_now = reg.decode_token_uri(reader.call(registry, reg.token_uri_calldata(agent_id)))
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be read ({e}); nothing changed") from None
    except LinkError:
        raise
    except Exception as e:  # noqa: BLE001 — a malformed answer is 'could not read', not a crash
        raise LinkError(f"the chain's answer was not understood ({type(e).__name__}); nothing changed") from None
    if not owner_now or owner_now.lower() != found["owner"]:
        raise LinkError("the registry's owner of that token is not the one in the receipt (it moved, or the receipt "
                        "is not trusted); not continuing")
    out: dict[str, Any] = {"chain": caip2, "chain_label": networks.label(caip2), "agent": me, "stable_id": stable_id,
                           "agent_id": found["agent_id"], "owner": found["owner"], "tx": tx_hash,
                           "block_number": found["block_number"], "block_hash": found["block_hash"],
                           "agent_uri": uri_now, "two_way": reg.names_agent(uri_now, stable_id),
                           "finalized_block": None}
    out["owner_is_agent_wallet"] = False
    try:
        out["owner_is_agent_wallet"] = bool(own_wallet().address.lower() == found["owner"])
    except Exception:  # noqa: BLE001 — no usable wallet here: the identity is simply not the agent wallet's
        pass
    if finalized is not None or read is None:
        try:
            out["finalized_block"] = _reader(caip2, finalized, "finalized").block_number
        except ChainUnknown:
            pass
    if not out["two_way"]:
        raise LinkError(f"agent #{found['agent_id']} was registered, but its registration does not name this agent "
                        f"({me}, stable id {stable_id[:12]}...): not linking it. To give this agent its own identity, "
                        "send the call that register-8004 prints (without --tx).")
    return out
