"""An agent's side of an ERC-8004 link (docs/protocol.md §3.20): what a person is told before they sign, the local
check of what they signed, and the signed record posted to the relay. Run as the agent (its own settings) by
`emporia agent link-8004 | unlink-8004`; it is a person's act, so there is no agent tool for it.

A link is a read plus an off-chain wallet signature: nothing is sent on-chain and no money moves, so the mainnet
money gate does not apply. Reads go through `chain_read.PinnedReader` at one block; every answer carries it.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from collections.abc import Callable

from emporia import erc8004, networks, signing, wire
from emporia.chain_read import ChainUnknown, PinnedReader

MAX_AGE_DAYS = 30
PRIVACY_NOTE = ("Linking publishes this wallet address next to the agent (and next to an X handle if one is "
                "linked). Relays copy it and the transparency log keeps it: unlinking removes it from the card, "
                "it cannot erase what was already published. To keep your main wallet off the card, use a "
                "delegate wallet where the owning contract allows it. Nothing is sent on-chain and no money moves.")


class LinkError(Exception):
    """Said to the person as it is."""


def person_only(what: str = "this") -> None:
    """Refuse unless a person is running this: not an agent's terminal (Hermes marks those with HERMES_SESSION_*), not a
    live relay-started or rounds turn. The commands check this too; the functions that sign or send check it again
    themselves, so importing them directly gets no further. Raises LinkError."""
    import os
    if any(k.startswith("HERMES_SESSION_") for k in os.environ):
        raise LinkError(f"{what} is a person's to do, in their own terminal: not from an agent's session")
    try:
        from emporia import autonomy
        origin = autonomy.current().origin
    except Exception:  # noqa: BLE001 — the state could not be read: the session check above stands
        return
    if origin in ("relay", "rounds"):
        raise LinkError(f"{what} is a person's to do: a {origin} turn is live")


def chains() -> dict[str, str]:
    """Short name -> CAIP-2 id, for the chains with a canonical registry."""
    return {k: v for k, v in networks.ALIASES.items() if v in networks.ERC8004_REGISTRIES}


def chain_of(name: str) -> str:
    caip2 = networks.resolve(name)
    if caip2 not in networks.ERC8004_REGISTRIES:
        raise LinkError(f"no ERC-8004 registry is known for {name!r}; --chain is one of {', '.join(sorted(chains()))}")
    return caip2


def _reader(caip2: str, read: Any = None) -> Any:
    if read is not None:
        return read
    try:
        return PinnedReader(caip2)
    except ChainUnknown as e:
        raise LinkError(str(e)) from None


def _agent_id(value: Any) -> str:
    text = str(value).strip()
    if not (text.isascii() and text.isdecimal()) or int(text) >= 1 << 256:
        raise LinkError("--agent-id is the ERC-8004 agent's number (a decimal integer)")
    return str(int(text))


def _signer(value: str) -> str:
    value = (value or "").strip()
    if value and not (len(value) == 42 and value.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in value[2:])):
        raise LinkError("--signer is an EVM address (0x and 40 hex characters)")
    return value


def _me(stable: str | None) -> tuple[str, str]:
    from emporia import mcp_server as m
    if not m.AGENT_ID:
        raise LinkError("no agent: EMPORIA_AGENT_ID is not set")
    return m.AGENT_ID, stable or m._signing_identity(m.AGENT_ID)


def _label(caip2: str) -> str:
    return networks.label(caip2)


def ask(chain: str, agent_id: Any, signer: str = "", *, issued_at: int | None = None, read: Any = None,
        stable: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Read the agent on-chain and say what linking would show; give the typed message to sign."""
    if not erc8004.available():
        raise LinkError("signing checks need eth-account (the x402 extra): pip install 'emporia[x402]'")
    caip2, aid = chain_of(chain), _agent_id(agent_id)
    signer = _signer(signer)
    _, stable_id = _me(stable)
    reader = _reader(caip2, read)
    try:
        first = erc8004.preview(aid, "", reader, now=now)
        if not first["owner"]:
            raise LinkError(first.get("reason") or "the agent has no owner")
        who = signer or _checksum(first["owner"])
        seen = erc8004.preview(aid, who, reader, now=now)
    except ChainUnknown as e:
        raise LinkError(f"the chain could not be read ({e}); nothing changed") from None
    registry = networks.ERC8004_REGISTRIES[caip2]
    chain_id = int(caip2.split(":", 1)[1])
    issued = int(issued_at if issued_at is not None else (now if now is not None else time.time()))
    td = erc8004.agent_link_typed_data(chain_id, registry, aid, who, stable_id, issued)
    return {"chain": caip2, "chain_label": _label(caip2), "testnet": networks.is_testnet(caip2),
            "agent_registry": erc8004.registry_string(chain_id, registry), "agent_id": aid, "signer": who,
            "owner": seen["owner"], "tier": seen["tier"], "tier_wording": erc8004.TIER_WORDING.get(seen["tier"], ""),
            "binding": seen.get("binding"), "reason": seen.get("reason", ""), "block_number": seen["block_number"],
            "block_hash": seen["block_hash"], "issued_at": issued, "typed_data": erc8004.typed_data_json(td),
            "privacy": PRIVACY_NOTE}


def _checksum(address: str) -> str:
    from eth_utils import to_checksum_address
    return to_checksum_address(address)


def _person_sign(agent_id: str, *args: Any) -> str:
    from emporia import identity, signer, signing
    if identity.identity_in_signer(agent_id) is not None:
        return signer.person_sign(agent_id, "erc8004-link", *args)
    return identity.sign(signing.payload("erc8004-link", *args), agent_id)


def record(chain: str, agent_id: Any, signer: str, issued_at: int, signature: str, *, read: Any = None,
           stable: str | None = None, now: float | None = None, post: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Verify the wallet's signature here first, then sign the record as the agent and post it."""
    if not erc8004.available():
        raise LinkError("signing checks need eth-account (the x402 extra): pip install 'emporia[x402]'")
    caip2, aid = chain_of(chain), _agent_id(agent_id)
    signer = _signer(signer)
    if not signer:
        raise LinkError("--signer is the wallet that signed (with --signature)")
    me, stable_id = _me(stable)
    clock = now if now is not None else time.time()
    if not isinstance(issued_at, int) or issued_at <= 0:
        raise LinkError("--issued-at is the unix time printed with the message you signed")
    if clock - issued_at > MAX_AGE_DAYS * 86400:
        raise LinkError(f"that signature is older than {MAX_AGE_DAYS} days: sign a new one")
    if issued_at - clock > 300:
        raise LinkError("that signature is dated in the future")
    registry = networks.ERC8004_REGISTRIES[caip2]
    reg = erc8004.registry_string(int(caip2.split(":", 1)[1]), registry)
    link = {"agent_registry": reg, "agent_id": aid, "signer": signer, "stable_id": stable_id,
            "issued_at": issued_at, "wallet_sig": signature}
    got = erc8004.verify(link, _reader(caip2, read), now=now)
    if got["status"] != "proven":
        raise LinkError(f"not recorded: the check says {got['status']}: {got['reason']}")
    at = datetime.fromtimestamp(clock, UTC).isoformat()
    sig = _person_sign(me, stable_id, reg, aid, signer, issued_at, signature, at)
    body = {"agent_registry": reg, "agent_id": aid, "signer": signer, "issued_at": issued_at,
            "wallet_sig": signature, "at": at, "signature": sig}
    if post is None:
        from emporia import mcp_server as m
        r = m._bearer_request("post", f"{m._base()}/agents/{m._seg(me)}/links/erc8004", agent_id=me, json=body,
                              timeout=30)
        answer, code = r.json(), r.status_code
    else:
        answer, code = post(me, body)
    if code != 200:
        raise LinkError(f"the relay refused it ({code}): {answer.get('detail', answer)}")
    return {"checked_here": got, "link": answer.get("link"), "links": answer.get("links"), "chain": caip2,
            "chain_label": _label(caip2), "testnet": networks.is_testnet(caip2)}


def own_wallet_ask(chain: str, agent_id: Any, *, read: Any = None, signer: Any = None, stable: str | None = None,
                   now: float | None = None) -> dict[str, Any]:
    """`ask` with this agent's own EVM wallet as the signer, and the wallet's signature of the statement: what a link
    of an identity the agent's own wallet owns needs. Nothing is sent on-chain and no money moves; the key stays in
    the signer. The answer is `ask`'s, plus `signature`; `tier` says what the chain backs ('' when the wallet does
    not control the identity)."""
    from emporia import chain_register
    person_only("signing with the agent's own wallet")
    wallet = signer or chain_register.own_wallet()
    out = ask(chain, agent_id, wallet.address, read=read, stable=stable, now=now)
    if not out["tier"]:
        return out
    try:
        out["signature"] = wallet.sign_typed_data(erc8004.typed_data_from_json(out["typed_data"]))
    except Exception as e:  # noqa: BLE001 — never echo a signer's words
        raise LinkError(f"the agent's wallet could not sign the statement ({type(e).__name__})") from None
    return out


def link_own_wallet(chain: str, agent_id: Any, *, read: Any = None, signer: Any = None, stable: str | None = None,
                    now: float | None = None, post: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Sign the link statement with the agent's own wallet and record it (`record` checks the signature and the chain
    again first): the link of an identity the agent's own wallet owns, without a browser wallet."""
    person_only("linking with the agent's own wallet")
    out = own_wallet_ask(chain, agent_id, read=read, signer=signer, stable=stable, now=now)
    if not out["tier"]:
        raise LinkError(f"the chain does not back this wallet as controlling it ({out.get('reason') or 'not proven'}): "
                        "nothing recorded")
    return record(chain, agent_id, out["signer"], int(out["issued_at"]), out["signature"], read=read, stable=stable,
                  now=now, post=post)


def unlink(chain: str, agent_id: Any, *, stable: str | None = None) -> dict[str, Any]:
    """The signed removal of one link (`kind` chain-link): it leaves the card, a removal marker stays."""
    from emporia import mcp_server as m
    caip2, aid = chain_of(chain), _agent_id(agent_id)
    me, stable_id = _me(stable)
    signing.remember_stable(me, stable_id)       # the removal names the agent by the stable id the relay holds
    reg = erc8004.registry_string(int(caip2.split(":", 1)[1]), networks.ERC8004_REGISTRIES[caip2])
    out = m._delete_item(wire.CHAIN_LINK_REMOVAL, wire.chain_link_item(stable_id, reg, aid), agent_id=me)
    if not out.get("success"):
        raise LinkError(f"{out.get('error')}: {out.get('detail', '')}".strip(": "))
    return out
