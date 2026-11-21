"""Register an Emporia agent in an ERC-8004 identity registry, the own-wallet path (docs/protocol.md section 3.20).

Pure: no Hermes, no web framework, no network of its own. The person's own wallet sends the transaction
`register(agentURI)`; Emporia only builds the registration file and the call, and later reads the receipt. Nothing
here sends a transaction or holds a key.

* `registration_file` is the agent's registration (the same field names the registry's real agents use): an
  Emporia name, its bio, the agent's token picture (`token_picture`, left out by `--no-image`), one `services` entry named `Emporia` that carries the agent's stable id, and
  `active: true`. `data_uri` makes it an on-chain `data:application/json;base64,...` URI, so nothing is hosted (plain-JSON
  `data:` registrations are read too).
* `calldata` is `register(string)`: the overload that takes only the URI.
* `agent_from_receipt` finds the new agent's number in the receipt's `Registered` event (and cross-checks the
  mint's ERC-721 `Transfer`), only from a log the canonical registry itself emitted.
* `names_agent` is the two-way check: the token's registration file names this agent's stable id in its
  `Emporia` service.

Testnet only for now: `chain_problem` refuses every other chain with a plain message.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote

from emporia.networks import ERC8004_REGISTRIES, NETWORKS

SERVICE_NAME = "Emporia"
REG_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
DATA_PREFIX = "data:application/json;base64,"       # written and read
UTF8_PREFIX = "data:application/json;utf8,"          # read too (2.1 test registrations used it)
MAX_URI_BYTES = 8 * 1024            # a registration file this size costs a few hundred thousand gas at most

# register(string): the first four bytes of keccak256 of the signature text (a test recomputes each).
SEL_REGISTER_URI = bytes.fromhex("f2c298be")
SEL_TOKEN_URI = bytes.fromhex("c87b56dd")           # tokenURI(uint256)
TOPIC_REGISTERED = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"   # Registered(uint256,string,address)
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"     # Transfer(address,address,uint256)
SELECTORS = {"register(string)": SEL_REGISTER_URI, "tokenURI(uint256)": SEL_TOKEN_URI}
TOPICS = {"Registered(uint256,string,address)": TOPIC_REGISTERED, "Transfer(address,address,uint256)": TOPIC_TRANSFER}

# The only chains the own-wallet path builds a call for; every other chain needs the release owner's go.
OPEN_CHAINS = ("eip155:84532",)
_TX = re.compile(r"0x[0-9a-fA-F]{64}")
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_B32 = re.compile(r"(0x)?[0-9a-fA-F]{64}")


class RegisterError(Exception):
    """Said to the person as it is."""


def chain_problem(caip2: str) -> str:
    """Why this chain is refused, or '' when the call may be built for it."""
    if caip2 in OPEN_CHAINS:
        return ""
    if caip2 in ERC8004_REGISTRIES:
        n = NETWORKS.get(caip2)
        return (f"registering on {n.label if n else caip2} needs the release owner's go: only Base Sepolia (testnet) "
                "is open for now")
    return f"no ERC-8004 registry is known for {caip2!r}; only Base Sepolia (testnet) is open for registering"


def tx_problem(tx_hash: str) -> str:
    return "" if _TX.fullmatch((tx_hash or "").strip()) else "--tx is a transaction hash: 0x and 64 hex characters"


# -- the registration file ----------------------------------------------------------------------------
def registration_file(name: str, about: str, relay_url: str, stable_id: str, x402_support: bool,
                      image: str | None = None) -> dict:
    """The agent's registration. `services[0].stableId` is the two-way tie to the Emporia agent. `image` is the seam
    for the agent's picture: an on-chain `data:image/...` URI, or None (the field is then left out)."""
    if not _B32.fullmatch(stable_id or ""):
        raise RegisterError("the agent's stable id is not known here (is it registered on its relay?)")
    sid = stable_id.lower().removeprefix("0x")
    base = (relay_url or "").strip().rstrip("/")
    if not base:
        raise RegisterError("this agent has no home relay to point the registration at")
    text = " ".join((about or "").split()) or f"An Emporia agent, {name}."
    if image is not None and not (isinstance(image, str) and image[:11].lower() == "data:image/" and len(image) <= 2048):
        raise RegisterError("the registration's image must be a small on-chain data:image/ URI")
    file = {
        "type": REG_TYPE,
        "name": name,
        "description": text,
        "services": [{"name": SERVICE_NAME, "endpoint": f"{base}/agents/{quote(name, safe='')}", "stableId": sid}],
        "x402Support": bool(x402_support),
        "active": True,
        "registrations": [],
        "supportedTrust": [],
    }
    if image is not None:
        file["image"] = image
    return file


def token_picture(stable_id: str) -> str:
    """The agent's token picture (`emporia.avatars.token_avatar`, a flat SVG from its stable id) as an on-chain
    `data:image/svg+xml;base64,...` URI: the default `image` of a registration. At most ~750 characters, inside the
    seam's limit; no remote address, nothing to fetch."""
    from emporia.avatars import token_avatar
    if not _B32.fullmatch(stable_id or ""):
        raise RegisterError("the agent's stable id is not known here (is it registered on its relay?)")
    svg = token_avatar(stable_id.lower().removeprefix("0x"))
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def data_uri(file: dict) -> str:
    """The registration as a base64 `data:application/json` URI, the on-chain form ERC-8004 indexers read. (A plain-JSON
    form was tried for 2.1: wallets shorten a decoded string to a few characters anyway, so it bought no readability
    and risked indexers that read base64 only; `decode_data_uri` still reads both.)"""
    raw = json.dumps(file, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()
    uri = DATA_PREFIX + base64.b64encode(raw).decode()
    if len(uri.encode()) > MAX_URI_BYTES:
        raise RegisterError(f"the registration is {len(uri)} bytes; at most {MAX_URI_BYTES} (shorten the bio)")
    return uri


def decode_data_uri(uri: str) -> dict | None:
    """The JSON of an on-chain `data:application/json` URI (base64 or plain), else None."""
    if not isinstance(uri, str) or len(uri) > 4 * MAX_URI_BYTES:
        return None
    from urllib.parse import unquote
    for prefix in (DATA_PREFIX, UTF8_PREFIX, "data:application/json,"):
        if uri.startswith(prefix):
            body = uri[len(prefix):]
            try:
                raw = base64.b64decode(body, validate=True) if prefix == DATA_PREFIX else unquote(body).encode()
                out = json.loads(raw)
            except (ValueError, TypeError):
                return None
            return out if isinstance(out, dict) else None
    return None


def names_agent(uri: str, stable_id: str) -> bool:
    """Whether the registration at `uri` names this stable id in its `Emporia` service: the two-way link."""
    doc = decode_data_uri(uri)
    if doc is None or not _B32.fullmatch(stable_id or ""):
        return False
    want = stable_id.lower().removeprefix("0x")
    services = doc.get("services")
    if not isinstance(services, list):
        return False
    for s in services:
        if isinstance(s, dict) and s.get("name") == SERVICE_NAME:
            got = s.get("stableId")
            if isinstance(got, str) and _B32.fullmatch(got) and got.lower().removeprefix("0x") == want:
                return True
    return False


# -- the call -----------------------------------------------------------------------------------------
def calldata(agent_uri: str) -> bytes:
    from eth_abi import encode
    return SEL_REGISTER_URI + encode(["string"], [agent_uri])


def token_uri_calldata(agent_id: int) -> bytes:
    from eth_abi import encode
    return SEL_TOKEN_URI + encode(["uint256"], [int(agent_id)])


def decode_token_uri(data: bytes) -> str:
    from eth_abi import decode
    (uri,) = decode(["string"], data)
    return uri


def decode_registered_id(data: bytes) -> int:
    """The agent id `register(...)` returns (a simulated call's answer)."""
    from eth_abi import decode
    (agent_id,) = decode(["uint256"], data)
    return agent_id


# -- the receipt --------------------------------------------------------------------------------------
def _topic_int(t: Any) -> int:
    return int(str(t), 16)


def _topic_addr(t: Any) -> str:
    return "0x" + str(t)[-40:].lower()


def agent_from_receipt(receipt: dict | None, registry: str, tx_hash: str) -> dict:
    """`{agent_id (decimal string), owner, agent_uri, block_number, block_hash}` from the receipt of a successful
    `register` on `registry`. Only the registry's own `Registered` log counts (a log from any other contract is
    ignored, however it is shaped); a mint `Transfer` from the registry must agree with it. The transaction may be
    addressed to the registry or reach it through a wallet's smart account (EIP-7702, a delegation a wallet shows as
    "Redeem Delegations"): the registry's own log is the evidence, not the address the transaction was sent to.
    Raises RegisterError."""
    if not isinstance(receipt, dict):
        raise RegisterError("the chain has no receipt for that transaction yet (is the hash right, and is it mined?)")
    got = str(receipt.get("transactionHash", "")).lower()
    if got != tx_hash.lower():
        raise RegisterError("the receipt is for another transaction")
    if str(receipt.get("status", "")).lower() not in ("0x1", "1"):
        raise RegisterError("that transaction failed on-chain: no agent was registered")
    from eth_abi import decode
    registered: list[tuple[int, str, str]] = []
    minted: set[tuple[int, str]] = set()
    for log in receipt.get("logs") or []:
        if not isinstance(log, dict) or str(log.get("address", "")).lower() != registry.lower():
            continue
        topics = log.get("topics") or []
        if not topics:
            continue
        try:
            if str(topics[0]).lower() == TOPIC_REGISTERED and len(topics) == 3:
                raw = bytes.fromhex(str(log.get("data", "0x"))[2:])
                (uri,) = decode(["string"], raw)
                registered.append((_topic_int(topics[1]), _topic_addr(topics[2]), uri))
            elif str(topics[0]).lower() == TOPIC_TRANSFER and len(topics) == 4 and _topic_int(topics[1]) == 0:
                minted.add((_topic_int(topics[3]), _topic_addr(topics[2])))
        except (ValueError, TypeError, IndexError):
            continue
    if len(registered) != 1:
        raise RegisterError("that transaction registered " + ("no agent" if not registered else "more than one agent")
                            + " in this registry")
    agent_id, owner, uri = registered[0]
    if minted and (agent_id, owner) not in minted:
        raise RegisterError("the receipt's mint and registration disagree: not trusting it")
    try:
        block = int(str(receipt.get("blockNumber")), 16)
    except (TypeError, ValueError):
        raise RegisterError("the receipt names no block") from None
    return {"agent_id": str(agent_id), "owner": owner, "agent_uri": uri, "block_number": block,
            "block_hash": str(receipt.get("blockHash", "")).lower()}
