"""Tempo — stablecoin payments paid straight to the payee, verified on chain (P156).

MPP's Tempo `charge` method (paymentauth.org/draft-tempo-charge-00), in its
**push** mode: the payer broadcasts a TIP-20 `transferWithMemo` to the payee and
hands the relay the transaction hash; the relay reads the receipt. Nothing is
custodial — the money goes payer → payee, as with x402 `exact`.

Checked live on Tempo's Moderato testnet (chain 42431, 2026-10-24): a standard
EIP-1559 transaction signed by an ordinary EVM key is accepted, its fee is
paid in the stablecoin (no gas token), and the receipt carries a
`TransferWithMemo(from, to, amount, memo)` log with `to` and `memo` indexed.
So an agent's existing EVM wallet pays here unchanged.

The offer binds everything the relay will check — payer, resource, amount,
token, recipient, chain, expiry — under an HMAC keyed with the relay's own
secret, and the on-chain memo is derived from that binding. A hash can be
used once (the payments table's unique reference refuses a replay).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

import httpx

TRANSFER_WITH_MEMO_TOPIC = "0x57bc7354aa85aed339e000bccffabbc529466af35f0772c8f8ee1145927de7f0"
PUBLIC_RPC = {42431: "https://rpc.moderato.tempo.xyz", 4217: "https://rpc.tempo.xyz"}
OFFER_TTL_S = 15 * 60
HEADER = "PAYMENT-TEMPO"


class TempoError(RuntimeError):
    """A Tempo payment could not be offered, made or verified; the message says why."""


def config() -> dict[str, Any] | None:
    """The relay's Tempo settings, or None when not fully configured:
    EMPORIA_TEMPO_NETWORK (CAIP-2, e.g. eip155:42431), EMPORIA_TEMPO_TOKEN (the
    TIP-20 stablecoin), EMPORIA_TEMPO_RPC_URL (default: Tempo's public RPC)."""
    network = os.getenv("EMPORIA_TEMPO_NETWORK", "").strip()
    token = os.getenv("EMPORIA_TEMPO_TOKEN", "").strip()
    if not network.startswith("eip155:") or not token:
        return None
    chain_id = int(network.split(":", 1)[1])
    rpc = os.getenv("EMPORIA_TEMPO_RPC_URL", "").strip() or PUBLIC_RPC.get(chain_id, "")
    if not rpc:
        return None
    return {"network": network, "chain_id": chain_id, "token": token.lower(), "rpc": rpc}


def _atomic(amount_cents: int) -> int:
    return amount_cents * 10_000    # TIP-20 stablecoins carry 6 decimals


def _binding(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def memo_for(fields: dict[str, Any]) -> str:
    """The bytes32 memo the transfer must carry: derived from the bound offer,
    so a transfer made for one offer can never satisfy another."""
    return "0x" + hashlib.sha256(b"emporia-tempo:" + _binding(fields)).hexdigest()


def build_offer(*, amount_cents: int, resource: str, recipient: str, payer_id: str,
                secret: bytes, cfg: dict[str, Any], now: int | None = None) -> dict[str, Any]:
    """The `tempo` part of a 402: MPP's charge request, plus the binding."""
    fields = {
        "id": secrets.token_hex(16), "payer": payer_id, "resource": resource,
        "amount": str(_atomic(amount_cents)), "currency": cfg["token"],
        "recipient": recipient.lower(), "chainId": cfg["chain_id"],
        "expires": (int(time.time()) if now is None else now) + OFFER_TTL_S,
    }
    mac = hmac.new(secret, _binding(fields), hashlib.sha256).hexdigest()
    return {
        "method": "tempo", "intent": "charge", "network": cfg["network"], "binding": fields,
        "mac": mac,
        "request": {"amount": fields["amount"], "currency": fields["currency"],
                    "recipient": fields["recipient"],
                    "methodDetails": {"chainId": cfg["chain_id"], "memo": memo_for(fields),
                                      "supportedModes": ["push"]}},
        "pay": (f"Send a TIP-20 transferWithMemo of `amount` of `currency` to `recipient` with "
                f"`memo` on chain {cfg['chain_id']}, then retry with header {HEADER}: "
                f"base64(JSON {{offer, payload: {{type: 'hash', hash}}}})."),
    }


def _rpc(url: str, method: str, params: list) -> Any:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise TempoError(f"{method}: {body['error']}")
    return body.get("result")


def verify(credential: str, *, payer_id: str, resource: str, amount_cents: int,
           recipient: str, secret: bytes, cfg: dict[str, Any],
           now: int | None = None) -> str:
    """The transaction hash, once the receipt shows this exact payment."""
    try:
        sent = json.loads(base64.b64decode(credential))
        offer, payload = sent["offer"], sent["payload"]
        fields = offer["binding"]
    except Exception as e:
        raise TempoError(f"{HEADER} must be base64(JSON {{offer, payload}})") from e
    if not hmac.compare_digest(hmac.new(secret, _binding(fields), hashlib.sha256).hexdigest(),
                               str(offer.get("mac", ""))):
        raise TempoError("this offer was not issued by this relay, or was altered")
    expected = {"payer": payer_id, "resource": resource, "amount": str(_atomic(amount_cents)),
                "currency": cfg["token"], "recipient": recipient.lower(), "chainId": cfg["chain_id"]}
    for k, v in expected.items():
        if fields.get(k) != v:
            raise TempoError(f"this offer was for a different {k}")
    if (int(time.time()) if now is None else now) > int(fields["expires"]):
        raise TempoError("this offer expired; ask for a new one")
    if payload.get("type") != "hash" or not str(payload.get("hash", "")).startswith("0x"):
        raise TempoError("only push mode is accepted: payload {type: 'hash', hash}")
    tx_hash = payload["hash"]
    receipt = _rpc(cfg["rpc"], "eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        raise TempoError(f"{tx_hash} has no receipt yet; retry once it is mined")
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise TempoError(f"{tx_hash} failed on chain")
    want_to = "0x" + recipient.lower().removeprefix("0x").rjust(64, "0")
    memo = memo_for(fields)
    for log in receipt.get("logs", []):
        topics = [t.lower() for t in log.get("topics", [])]
        if (log.get("address", "").lower() == cfg["token"] and len(topics) == 4
                and topics[0] == TRANSFER_WITH_MEMO_TOPIC and topics[2] == want_to
                and topics[3] == memo and int(log.get("data", "0x0"), 16) >= int(expected["amount"])):
            return tx_hash
    raise TempoError(f"{tx_hash} carries no transferWithMemo of {expected['amount']} to "
                     f"{recipient} with this offer's memo")


def pay(offer: dict[str, Any], private_key: str, *, rpc_url: str | None = None,
        wait_s: float = 60.0) -> str:
    """The payer's side: send the transferWithMemo the offer asks for, wait for
    it, and return the credential for the retry. The agent wallet applies its
    caps before calling this."""
    from eth_abi import encode
    from eth_account import Account
    from eth_utils import keccak, to_checksum_address

    req = offer["request"]
    chain_id = int(req["methodDetails"]["chainId"])
    url = rpc_url or os.getenv("EMPORIA_TEMPO_RPC_URL", "") or PUBLIC_RPC.get(chain_id, "")
    if not url:
        raise TempoError(f"no RPC for Tempo chain {chain_id}: set EMPORIA_TEMPO_RPC_URL")
    acct = Account.from_key(private_key)
    token = to_checksum_address(req["currency"])
    data = keccak(text="transferWithMemo(address,uint256,bytes32)")[:4] + encode(
        ["address", "uint256", "bytes32"],
        [to_checksum_address(req["recipient"]), int(req["amount"]),
         bytes.fromhex(req["methodDetails"]["memo"].removeprefix("0x"))])
    call = {"from": acct.address, "to": token, "data": "0x" + data.hex()}
    gas = int(_rpc(url, "eth_estimateGas", [call]), 16)
    base = int((_rpc(url, "eth_getBlockByNumber", ["latest", False]) or {}).get("baseFeePerGas", "0x0"), 16)
    tip = int(_rpc(url, "eth_maxPriorityFeePerGas", []), 16)
    tx = {"type": 2, "chainId": chain_id, "to": token, "value": 0, "data": "0x" + data.hex(),
          "gas": int(gas * 1.3), "maxFeePerGas": base * 2 + tip, "maxPriorityFeePerGas": tip,
          "nonce": int(_rpc(url, "eth_getTransactionCount", [acct.address, "pending"]), 16)}
    tx_hash = _rpc(url, "eth_sendRawTransaction", ["0x" + acct.sign_transaction(tx).raw_transaction.hex()])
    deadline = time.time() + wait_s
    while time.time() < deadline:
        receipt = _rpc(url, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if int(receipt.get("status", "0x0"), 16) != 1:
                raise TempoError(f"the transfer {tx_hash} failed on chain")
            break
        time.sleep(1)
    else:
        raise TempoError(f"no receipt for {tx_hash} within {wait_s:.0f}s")
    return base64.b64encode(json.dumps(
        {"offer": offer, "payload": {"type": "hash", "hash": tx_hash}}).encode()).decode()
