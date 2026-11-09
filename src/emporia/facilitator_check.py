"""Does the x402 facilitator's fee payer have gas? (`emporia doctor`, relay side.)

A facilitator that settles an x402 payment pays the network fee from its own account (its *fee payer*, or
*signer*). When that account is empty every settlement fails, and nothing on the relay says why until a payer is
refused. The check reads the facilitator's public `/supported` list (v1: `kinds[].extra.feePayer`; v2:
`signers`, by network family), asks the network's RPC for each account's balance and says which has none.

Every call has a short timeout and a failure is a note, never an error: a facilitator that cannot be read, or an
RPC that does not answer, says nothing about the relay's own health. Pure of Hermes; `get` and `post` are
parameters so tests use fakes and never the network.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

TIMEOUT_S = 5.0
FIX = "switch EMPORIA_X402_FACILITATOR_URL to a facilitator whose fee payer has funds (or fund that account)"


def _get(url: str) -> Any:
    import httpx
    r = httpx.get(url, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _post(url: str, body: dict) -> Any:
    import httpx
    r = httpx.post(url, json=body, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def fee_payers(supported: dict[str, Any], networks: list[str]) -> dict[str, list[str]]:
    """{network: [fee payer addresses]} the facilitator names for these networks."""
    out: dict[str, list[str]] = {n: [] for n in networks}
    signers = supported.get("signers") if isinstance(supported.get("signers"), dict) else {}
    for net in networks:
        family = net.split(":", 1)[0]
        for key, accounts in signers.items():
            if key == net or key == f"{family}:*":
                out[net] += [str(a) for a in (accounts or []) if isinstance(a, str)]
    for kind in supported.get("kinds") or []:
        net = kind.get("network") if isinstance(kind, dict) else None
        payer = ((kind.get("extra") or {}).get("feePayer") if isinstance(kind, dict) else None)
        if net in out and isinstance(payer, str):
            out[net].append(payer)
    return {n: list(dict.fromkeys(a)) for n, a in out.items() if a}


def balance(network: str, address: str, rpc_url: str, post: Callable[[str, dict], Any] = _post) -> int:
    """The account's native balance (wei, or lamports) through the network's JSON-RPC. Raises on any failure."""
    if network.startswith("solana:"):
        body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}
        return int(post(rpc_url, body)["result"]["value"])
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]}
    return int(str(post(rpc_url, body)["result"]), 16)


def rpc_for(network: str, env: dict[str, str]) -> str:
    """The relay's own RPC for a network (`EMPORIA_X402_RPC_<NETWORK>`), else the network table's public one."""
    from emporia.networks import NETWORKS
    name = "EMPORIA_X402_RPC_" + re.sub(r"[:-]", "_", network.upper())
    return (env.get(name) or "").strip() or (NETWORKS[network].rpc if network in NETWORKS else "")


def check(facilitator: str, networks: list[str], env: dict[str, str], *, get: Callable[[str], Any] = _get,
          post: Callable[[str, dict], Any] = _post) -> list[tuple[str, str, str]]:
    """[(level, message, fix)] with level "warn" or "note". Empty when every fee payer has gas."""
    if not facilitator or not networks:
        return []
    try:
        supported = get(f"{facilitator.rstrip('/')}/supported")
    except Exception as e:  # noqa: BLE001 - a note, not an error
        return [("note", f"could not read the facilitator's /supported to check its fee payer's gas ({type(e).__name__})", "")]
    payers = fee_payers(supported if isinstance(supported, dict) else {}, networks)
    out: list[tuple[str, str, str]] = []
    for net in networks:
        if net not in payers:
            out.append(("note", f"the facilitator names no fee payer for {net}, so its gas was not checked", ""))
            continue
        rpc = rpc_for(net, env)
        if not rpc:
            out.append(("note", f"no RPC for {net} (EMPORIA_X402_RPC_*), so the fee payer's gas was not checked", ""))
            continue
        # A facilitator may name many fee payers and settle from whichever is funded, so one empty account
        # says nothing: one line per network, a warning only when none of the readable ones has gas.
        funded = empty = unread = 0
        for addr in payers[net]:
            try:
                held = balance(net, addr, rpc, post)
            except Exception:  # noqa: BLE001 - counted as unread
                unread += 1
                continue
            funded, empty = (funded + 1, empty) if held > 0 else (funded, empty + 1)
        read = funded + empty
        tail = f" ({unread} could not be read)" if unread else ""
        if not read:
            out.append(("note", f"could not read the balance of any fee payer on {net}, so its gas was not checked", ""))
        elif not funded:
            out.append(("warn", f"none of the x402 facilitator's {read} fee payer(s) on {net} has gas{tail}: its "
                                "settlements will fail", FIX))
        else:
            out.append(("note", f"{funded} of {read} fee payer(s) on {net} have gas{tail}", ""))
    return out
