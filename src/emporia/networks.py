"""The payment networks Emporia knows, by CAIP-2 id (P160).

One table for every surface that names a network to a person or an agent —
the CLI, the MCP tools' 402 summaries, `money_info`, and the relay's
presets — so "eip155:84532" is "Base Sepolia (testnet)" everywhere, and a
setup step can say where test funds come from.

Public facts only: chain ids, token addresses, public RPCs, faucets. A relay
still advertises only what its operator configured; this table never turns a
network on.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Network:
    caip2: str
    label: str
    family: str            # "evm" | "svm" — which wallet key pays here
    testnet: bool
    usdc: str = ""         # the USDC token (EVM contract / Solana mint), "" if none
    rpc: str = ""
    explorer: str = ""     # transaction URL prefix
    faucet: str = ""       # where test funds come from (testnets)
    payments: bool = True  # False: a chain Emporia reads, never one an agent pays on
    address_explorer: str = ""  # address URL prefix


NETWORKS: dict[str, Network] = {n.caip2: n for n in (
    Network("eip155:84532", "Base Sepolia", "evm", True,
            usdc="0x036CbD53842c5426634e7929541eC2318f3dCF7e", rpc="https://sepolia.base.org",
            explorer="https://sepolia.basescan.org/tx/",
            faucet="https://faucet.circle.com (USDC, Base Sepolia)"),
    Network("eip155:8453", "Base", "evm", False,
            usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", rpc="https://mainnet.base.org",
            explorer="https://basescan.org/tx/"),
    Network("solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", "Solana devnet", "svm", True,
            usdc="4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU", rpc="https://api.devnet.solana.com",
            explorer="https://explorer.solana.com/tx/",
            faucet="https://faucet.circle.com (USDC, Solana devnet)"),
    Network("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "Solana", "svm", False,
            usdc="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", rpc="https://api.mainnet-beta.solana.com",
            explorer="https://explorer.solana.com/tx/"),
    Network("eip155:42431", "Tempo Moderato", "evm", True,
            usdc="0x20c0000000000000000000000000000000000000",  # pathUSD, Tempo's test stablecoin
            rpc="https://rpc.moderato.tempo.xyz",
            faucet="the tempo_fundAddress RPC method on the Moderato RPC"),
    Network("eip155:4217", "Tempo", "evm", False, rpc="https://rpc.tempo.xyz"),
    # Read-only chains. Their RPCs answer the right chain id (checked against the
    # operator's published connection docs). No USDC is pinned: neither Circle's USDC address pages nor
    # Robinhood's contracts page list one for this chain (its listed stablecoin is USDG), so payments stay
    # off until an official USDC address is published and verified.
    Network("eip155:46630", "Robinhood Chain Testnet", "evm", True, rpc="https://rpc.testnet.chain.robinhood.com",
            explorer="https://explorer.testnet.chain.robinhood.com/tx/",
            address_explorer="https://explorer.testnet.chain.robinhood.com/address/", payments=False),
    Network("eip155:4663", "Robinhood Chain", "evm", False, rpc="https://rpc.mainnet.chain.robinhood.com",
            explorer="https://robinhoodchain.blockscout.com/tx/",
            address_explorer="https://robinhoodchain.blockscout.com/address/", payments=False),
)}


def pay_networks() -> dict[str, Network]:
    """The networks an agent may pay on: every entry except the read-only ones."""
    return {k: v for k, v in NETWORKS.items() if v.payments}


# ERC-8004 identity registries, per chain (a table, never derived from the chain id).
ERC8004_REGISTRIES: dict[str, str] = {
    "eip155:8453": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
    "eip155:4663": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
    "eip155:84532": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
    "eip155:46630": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
}


def rpc_for(caip2: str) -> list[str]:
    """RPC URLs for a chain: EMPORIA_RPC_<CHAINID> (one URL, or two comma-separated
    so reads must agree), else the table's public RPC."""
    chain_id = caip2.split(":", 1)[-1]
    raw = os.getenv(f"EMPORIA_RPC_{re.sub(r'[^A-Za-z0-9]', '_', chain_id).upper()}", "")
    urls = [u.strip() for u in raw.split(",") if u.strip()][:2]
    if urls:
        return urls
    n = NETWORKS.get(caip2)
    return [n.rpc] if n and n.rpc else []


def rpc_configured(caip2: str) -> list[str]:
    """Only what an operator set (EMPORIA_RPC_<CHAINID>), never the table's public RPC: the relay reads only
    RPC URLs its operator chose."""
    raw = os.getenv(f"EMPORIA_RPC_{re.sub(r'[^A-Za-z0-9]', '_', caip2.split(':', 1)[-1]).upper()}", "")
    return [u.strip() for u in raw.split(",") if u.strip()][:2]


def label(caip2: str) -> str:
    """"Base Sepolia (testnet)", or the raw id for a network not in the table."""
    n = NETWORKS.get(caip2)
    if n is None:
        return caip2
    return f"{n.label} (testnet)" if n.testnet else n.label


# P177: agents pay on testnets only until a person unlocks mainnet (real money)
# for that agent, in their own terminal (`emporia wallet mainnet NAME`). A
# network not in the table counts as mainnet: only a known testnet is "test".
MAINNET_VAR = "EMPORIA_X402_MAINNET"


def is_testnet(caip2: str) -> bool:
    n = NETWORKS.get(caip2)
    return bool(n and n.testnet)


def mainnet_unlocked(env: dict[str, str] | None = None) -> bool:
    raw = os.getenv("EMPORIA_X402_MAINNET", "") if env is None else env.get(MAINNET_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def family(caip2: str) -> str | None:
    n = NETWORKS.get(caip2)
    if n:
        return n.family
    if caip2.startswith("eip155:"):
        return "evm"
    if caip2.startswith("solana:"):
        return "svm"
    return None


# Short names for the CLI: `--networks base-sepolia,solana-devnet`.
ALIASES: dict[str, str] = {
    "base-sepolia": "eip155:84532", "base": "eip155:8453",
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "tempo-moderato": "eip155:42431", "tempo": "eip155:4217",
    "robinhood-testnet": "eip155:46630", "robinhood": "eip155:4663",
}


def resolve(name: str) -> str:
    """A short name or a CAIP-2 id, as a CAIP-2 id."""
    name = name.strip()
    return ALIASES.get(name.lower(), name)


def resolve_list(csv: str) -> list[str]:
    return [resolve(n) for n in csv.split(",") if n.strip()]


def stablecoin_balance(caip2: str, address: str, timeout: float = 8.0) -> float | None:
    """The address's USDC (or pathUSD on Tempo) in whole units, read from the
    network's public RPC; None when it cannot be read. Read-only."""
    import httpx

    n = NETWORKS.get(caip2)
    if n is None or not n.usdc or not n.rpc or not address:
        return None
    try:
        if n.family == "evm":
            data = "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")  # balanceOf
            body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": n.usdc, "data": data}, "latest"]}
            result = httpx.post(n.rpc, json=body, timeout=timeout).json().get("result")
            return int(result, 16) / 1e6 if result else None
        body = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [address, {"mint": n.usdc}, {"encoding": "jsonParsed"}]}
        accounts = (httpx.post(n.rpc, json=body, timeout=timeout).json().get("result") or {}).get("value")
        if accounts is None:
            return None
        return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                   for a in accounts)
    except Exception:
        return None
