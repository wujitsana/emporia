"""Verify that a wallet controls an ERC-8004 agent: the EIP-712 link statement and the tiers of proof.

Pure: no Hermes, no web framework, no network of its own. Every chain read goes through an injected pinned
reader (anything with `caip2`, `block_number`, `block_hash`, `call(to, data)` and `code(address)`), so all of
a verification is read at one block. Signature recovery needs `eth-account` (the optional x402 extra); without
it the result is `not-checked`.

Result statuses: `proven` (a tier of owner / bound-owner / controller), `not-proven` (the wallet signed but
the chain does not back it), `invalid` (the link or its signature is wrong), `unknown` (a read failed: say
nothing, never call it invalid), `not-checked` (no signature library here).
"""
from __future__ import annotations

import re
import time
from typing import Any, Protocol

from emporia.chain_read import ChainRevert, ChainUnknown
from emporia.networks import ERC8004_REGISTRIES

STATEMENT = "Link this ERC-8004 agent to the Emporia agent below. Moves no funds, grants no permission."
DOMAIN_NAME, DOMAIN_VERSION = "Emporia agent link", "1"
MAGIC_1271 = bytes.fromhex("1626ba7e")
MAX_SIG_BYTES = 4096
BINDING_KEY = "agent-binding"

# Function selectors: the first four bytes of keccak256 of the signature text (a test recomputes each).
SEL_OWNER_OF = bytes.fromhex("6352211e")          # ownerOf(uint256)
SEL_GET_METADATA = bytes.fromhex("cb4799f2")      # getMetadata(uint256,string)
SEL_BINDING_OF = bytes.fromhex("4d69ebc2")        # bindingOf(uint256)
SEL_IS_CONTROLLER = bytes.fromhex("158e711d")     # isController(uint256,address)
SEL_IS_VALID_SIG = MAGIC_1271                     # isValidSignature(bytes32,bytes)
SEL_OWNER = bytes.fromhex("8da5cb5b")             # owner()
SELECTORS = {
    "ownerOf(uint256)": SEL_OWNER_OF, "getMetadata(uint256,string)": SEL_GET_METADATA,
    "bindingOf(uint256)": SEL_BINDING_OF, "isController(uint256,address)": SEL_IS_CONTROLLER,
    "isValidSignature(bytes32,bytes)": SEL_IS_VALID_SIG, "owner()": SEL_OWNER,
}

# The binding interface's `standard` enum, as read from a deployed binding contract's verified source.
STD_ERC721, STD_ACCOUNT, STD_OWNABLE = 0, 5, 6
STANDARD_NAMES = {0: "erc721", 1: "erc1155", 2: "erc6909", 3: "erc1155f", 4: "erc6909f", 5: "account",
                  6: "contract-ownable", 7: "contract-admin"}

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_B32 = re.compile(r"(0x)?[0-9a-fA-F]{64}")
_DEC = re.compile(r"[0-9]{1,78}")


class Reader(Protocol):
    caip2: str
    block_number: int
    block_hash: str

    def call(self, to: str, data: bytes) -> bytes: ...
    def code(self, address: str) -> bytes: ...


def available() -> bool:
    try:
        import eth_abi  # noqa: F401
        import eth_account  # noqa: F401
        import eth_utils  # noqa: F401
    except ImportError:
        return False
    return True


def _keccak(data: bytes) -> bytes:
    from eth_utils import keccak
    return keccak(data)


# -- the signed statement ---------------------------------------------------------------------------
def registry_string(chain_id: int, registry: str) -> str:
    return f"eip155:{int(chain_id)}:{registry.lower()}"


def agent_link_typed_data(chain_id: int, registry: str, agent_id: str | int, signer: str,
                          stable_id: str, issued_at: int) -> dict:
    """The EIP-712 message a wallet signs. `agent_id` is a decimal string outside the ABI; `stable_id` is
    32 bytes as hex. No `verifyingContract`: no contract of ours verifies it."""
    sid = stable_id[2:] if stable_id.lower().startswith("0x") else stable_id
    return {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"}],
            "AgentLink": [{"name": "statement", "type": "string"}, {"name": "agentRegistry", "type": "string"},
                          {"name": "agentId", "type": "uint256"}, {"name": "signer", "type": "address"},
                          {"name": "emporiaStableId", "type": "bytes32"}, {"name": "issuedAt", "type": "uint64"}],
        },
        "primaryType": "AgentLink",
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": int(chain_id)},
        "message": {"statement": STATEMENT, "agentRegistry": registry_string(chain_id, registry),
                    "agentId": int(agent_id), "signer": signer, "emporiaStableId": bytes.fromhex(sid),
                    "issuedAt": int(issued_at)},
    }


def agent_link_digest(typed_data: dict) -> bytes:
    from eth_account.messages import encode_typed_data
    m = encode_typed_data(full_message=typed_data)
    return _keccak(b"\x19" + m.version + m.header + m.body)


def _recover(typed_data: dict, sig: bytes) -> str | None:
    """The address that signed, for a 65-byte low-s signature; else None."""
    if len(sig) != 65:
        return None
    r, s, v = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"), sig[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28) or not (0 < s <= SECP256K1_N // 2) or not (0 < r < SECP256K1_N):
        return None
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    try:
        return Account.recover_message(encode_typed_data(full_message=typed_data), signature=sig[:64] + bytes([v]))
    except Exception:
        return None


# -- results ----------------------------------------------------------------------------------------
def _result(status: str, tier: str, reason: str, read: Any = None, owner: str = "", binding: dict | None = None,
            now: float | None = None) -> dict:
    out: dict = {"tier": tier, "status": status, "block_number": None, "block_hash": "", "owner": owner.lower(),
                 "checked_at": int(now if now is not None else time.time()), "reason": reason}
    if read is not None:
        try:
            out["block_number"], out["block_hash"] = read.block_number, read.block_hash
        except ChainUnknown:
            pass
    if binding:
        out["binding"] = binding
    return out


class _Stop(Exception):
    def __init__(self, status: str, reason: str):
        self.status, self.reason = status, reason


def _parse(link: dict) -> dict:
    try:
        reg = str(link["agent_registry"])
        parts = reg.split(":")
        if len(parts) != 3 or parts[0] != "eip155" or not _DEC.fullmatch(parts[1]) or not _ADDR.fullmatch(parts[2]):
            raise ValueError("registry")
        agent_id, signer, sid = str(link["agent_id"]), str(link["signer"]), str(link["stable_id"])
        if not _DEC.fullmatch(agent_id) or int(agent_id) >= 1 << 256 or not _ADDR.fullmatch(signer):
            raise ValueError("fields")
        if not _B32.fullmatch(sid):
            raise ValueError("stable id")
        issued = link["issued_at"]
        if isinstance(issued, bool) or not isinstance(issued, int) or not 0 <= issued < 1 << 64:
            raise ValueError("issued_at")
        sig_hex = str(link["wallet_sig"])
        sig_hex = sig_hex[2:] if sig_hex[:2].lower() == "0x" else sig_hex
        if len(sig_hex) > 2 * MAX_SIG_BYTES:
            raise ValueError("signature size")
        sig = bytes.fromhex(sig_hex)
    except (KeyError, ValueError, TypeError):
        raise _Stop("invalid", "the link is malformed") from None
    return {"chain_id": int(parts[1]), "registry": parts[2], "agent_id": int(agent_id), "signer": signer,
            "stable_id": sid, "issued_at": issued, "sig": sig}


def _call(read: Reader, to: str, sel: bytes, types: list[str], values: list) -> bytes:
    from eth_abi import encode
    return read.call(to, sel + (encode(types, values) if types else b""))


def _addr(word: bytes) -> str | None:
    from eth_utils import to_checksum_address
    if len(word) < 32 or any(word[:12]):
        return None
    return to_checksum_address(word[12:32])


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def _signature_ok(read: Reader, p: dict, td: dict) -> None:
    """Return if `signer` signed the digest; else raise _Stop."""
    rec = _recover(td, p["sig"])
    if rec is not None and _same(rec, p["signer"]):
        return
    try:
        has_code = bool(read.code(p["signer"]))
    except ChainUnknown:
        raise _Stop("unknown", "the signer's account could not be read") from None
    if not has_code:
        raise _Stop("invalid", "the signature is not the signer's")
    try:
        ret = _call(read, p["signer"], SEL_IS_VALID_SIG, ["bytes32", "bytes"], [agent_link_digest(td), p["sig"]])
    except ChainRevert:
        raise _Stop("invalid", "the signer's contract rejected the signature") from None
    except ChainUnknown:
        raise _Stop("unknown", "the signer's contract could not be asked") from None
    if ret[:32] != MAGIC_1271 + b"\x00" * 28:
        raise _Stop("invalid", "the signer's contract rejected the signature")


def _read_binding(read: Reader, owner: str, agent_id: int) -> tuple[int, str, int] | None:
    """The owning contract's claimed binding, if it names itself in the registry's metadata and answers."""
    from eth_abi import decode
    reg = ERC8004_REGISTRIES[read.caip2]
    try:
        meta = _call(read, reg, SEL_GET_METADATA, ["uint256", "string"], [agent_id, BINDING_KEY])
        (raw,) = decode(["bytes"], meta)
    except ChainRevert:
        return None
    except Exception as e:
        if isinstance(e, ChainUnknown):
            raise
        return None
    named = raw[-20:] if len(raw) in (20, 32) else b""
    if not named or not _same("0x" + named.hex(), owner):
        return None
    try:
        std, bound, token = decode(["uint256", "address", "uint256"], _call(read, owner, SEL_BINDING_OF, ["uint256"], [agent_id]))
    except ChainRevert:
        return None
    except ChainUnknown:
        raise
    except Exception:
        return None
    return std, bound, token


def _derive(read: Reader, std: int, bound: str, token: int, signer: str) -> bool:
    """Emporia's own read of who holds the binding. A revert or odd answer is 'does not agree'."""
    try:
        if std == STD_ACCOUNT:
            return _same(bound, signer)
        sel, types, values = {STD_ERC721: (SEL_OWNER_OF, ["uint256"], [token]),
                              STD_OWNABLE: (SEL_OWNER, [], [])}[std]
        who = _addr(_call(read, bound, sel, types, values))
    except KeyError:
        return False
    except ChainRevert:
        return False
    return who is not None and _same(who, signer)


def _is_controller(read: Reader, owner: str, agent_id: int, signer: str) -> bool:
    try:
        ret = _call(read, owner, SEL_IS_CONTROLLER, ["uint256", "address"], [agent_id, signer])
    except ChainRevert:
        return False
    return len(ret) >= 32 and int.from_bytes(ret[:32], "big") == 1 and not any(ret[:31])


def verify(link: dict, read: Reader, *, now: float | None = None) -> dict:
    """Verify a link `{agent_registry, agent_id, signer, stable_id, issued_at, wallet_sig}` at the reader's
    pinned block. Returns `{tier, status, block_number, block_hash, owner, binding?, checked_at, reason}`."""
    owner = ""
    pinned: Any = None      # the reader, once it has pinned a block: a link refused before that read nothing
    try:
        p = _parse(link)
        chain = f"eip155:{p['chain_id']}"
        canonical = ERC8004_REGISTRIES.get(chain)
        if canonical is None or not _same(canonical, p["registry"]):
            raise _Stop("invalid", "not the canonical registry for that chain")
        if read.caip2 != chain:
            raise _Stop("invalid", "the reader is on a different chain")
        if not available():
            raise _Stop("not-checked", "no signature library is installed")
        td = agent_link_typed_data(p["chain_id"], canonical, p["agent_id"], p["signer"], p["stable_id"], p["issued_at"])
        read.block_number  # pin the block first: a dead RPC is unknown before anything else
        pinned = read
        _signature_ok(read, p, td)
        try:
            owner = _addr(_call(read, canonical, SEL_OWNER_OF, ["uint256"], [p["agent_id"]])) or ""
        except ChainRevert:
            raise _Stop("not-proven", "the agent does not exist in that registry") from None
        if not owner:
            if not read.code(canonical):
                raise _Stop("unknown", "no contract at the registry address on this chain")
            raise _Stop("not-proven", "the registry named no owner")
        if _same(owner, p["signer"]):
            return _result("proven", "owner", "the signer owns the agent", read, owner, now=now)
        if not read.code(owner):
            raise _Stop("not-proven", "the signer does not own the agent")
        controller = _is_controller(read, owner, p["agent_id"], p["signer"])
        if not controller:
            raise _Stop("not-proven", "the owning contract does not name the signer a controller")
        claim = _read_binding(read, owner, p["agent_id"])
        if claim is not None and claim[0] in STANDARD_NAMES:
            std, bound, token = claim
            if _derive(read, std, bound, token, p["signer"]):
                b = {"standard": STANDARD_NAMES[std], "address": bound.lower()}
                if std != STD_ACCOUNT and std != STD_OWNABLE:
                    b["token_id"] = str(token)
                return _result("proven", "bound-owner", "the bound holder is the signer", read, owner, b, now=now)
        return _result("proven", "controller", "the owning contract names the signer a controller", read, owner, now=now)
    except _Stop as s:
        return _result(s.status, "", s.reason, pinned, owner, now=now)     # a tier only for a proven link
    except ChainUnknown:
        return _result("unknown", "", "the chain could not be read", pinned, owner, now=now)


# -- what a person is told before they sign ---------------------------------------------------------
TIER_WORDING = {"owner": "owner", "bound-owner": "holder of the bound token", "controller": "controller, per the owning contract"}


def preview(agent_id: str | int, signer: str, read: Reader, *, now: float | None = None) -> dict:
    """What `signer` would prove for this agent at the reader's pinned block, without a signature: the owner,
    the binding and the tier verify() would give a good signature. `tier` is "" when the chain does not back
    the signer. Reads only; raises ChainUnknown if the chain cannot be read."""
    agent_id = int(agent_id)
    chain = read.caip2
    registry = ERC8004_REGISTRIES[chain]
    read.block_number
    out: dict = {"tier": "", "owner": "", "block_number": read.block_number, "block_hash": read.block_hash,
                 "checked_at": int(now if now is not None else time.time())}
    try:
        owner = _addr(_call(read, registry, SEL_OWNER_OF, ["uint256"], [agent_id])) or ""
    except ChainRevert:
        out["reason"] = "the agent does not exist in that registry"
        return out
    out["owner"] = owner.lower()
    if not owner:
        out["reason"] = "the registry named no owner"
        return out
    if not signer:
        out["reason"] = ""
        return out
    if _same(owner, signer):
        out["tier"] = "owner"
        return out
    if not read.code(owner):
        out["reason"] = "the signer does not own the agent"
        return out
    if not _is_controller(read, owner, agent_id, signer):
        out["reason"] = "the owning contract does not name the signer a controller"
        return out
    out["tier"] = "controller"
    claim = _read_binding(read, owner, agent_id)
    if claim is not None and claim[0] in STANDARD_NAMES:
        std, bound, token = claim
        if _derive(read, std, bound, token, signer):
            out["tier"] = "bound-owner"
            out["binding"] = {"standard": STANDARD_NAMES[std], "address": bound.lower()}
            if std not in (STD_ACCOUNT, STD_OWNABLE):
                out["binding"]["token_id"] = str(token)
    return out


def typed_data_json(td: dict) -> dict:
    """The typed message as a wallet or `cast` takes it: JSON only (bytes32 as hex, uint256 as decimal strings)."""
    m = dict(td["message"])
    m["emporiaStableId"] = "0x" + bytes(m["emporiaStableId"]).hex()
    m["agentId"] = str(m["agentId"])
    return {**td, "message": m}


def typed_data_from_json(td: dict) -> dict:
    m = dict(td["message"])
    m["emporiaStableId"] = bytes.fromhex(str(m["emporiaStableId"]).removeprefix("0x"))
    m["agentId"], m["issuedAt"] = int(m["agentId"]), int(m["issuedAt"])
    return {**td, "message": m}


def signature_recovers(link: dict) -> bool | None:
    """Whether the wallet signature recovers to `signer` (an ordinary account signed it): True, False, or None when
    this process cannot check (no library, or a malformed link). A contract wallet's signature is False here and is
    settled only by a chain read (ERC-1271)."""
    if not available():
        return None
    try:
        p = _parse(link)
        td = agent_link_typed_data(p["chain_id"], p["registry"], p["agent_id"], p["signer"], p["stable_id"],
                                   p["issued_at"])
    except _Stop:
        return None
    rec = _recover(td, p["sig"])
    return rec is not None and _same(rec, p["signer"])
