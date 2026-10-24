"""The x402 `exact` scheme on Solana (SVM), the payer's side (P155).

Builds what the x402 v2 spec (`specs/schemes/exact/scheme_exact_svm.md`) asks a
client for: a versioned transaction, partially signed by the payer, carrying
exactly

    1. ComputeBudget SetComputeUnitLimit
    2. ComputeBudget SetComputeUnitPrice   (within the 5 lamports/CU ceiling)
    3. SPL Token TransferChecked           payer's ATA -> payTo's ATA, the exact amount
    4. Memo                                `extra.memo`, or a random 16-byte hex nonce

with the facilitator's `extra.feePayer` as fee payer — account 0, whose
signature slot is left empty for the facilitator, which may sign only to pay
fees. The payer's own signature is the only other one. Nothing else: no ATA
creation, no other programs.

Requires `solders` (Solana's own primitives); imported lazily so a relay or an
agent that never pays on Solana does not need it.
"""

from __future__ import annotations

import base64
import secrets
import struct
from typing import Any

import httpx

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PXnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

#: Enough for one TransferChecked and a memo.
COMPUTE_UNIT_LIMIT = 20_000
#: In micro-lamports per CU; far under the spec's 5 lamports/CU ceiling.
COMPUTE_UNIT_PRICE = 1
#: Public RPCs by CAIP-2 network, for the recent blockhash when the
#: requirement does not hint one. EMPORIA_X402_SOLANA_RPC_URL overrides.
PUBLIC_RPC = {
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": "https://api.mainnet-beta.solana.com",
    "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1": "https://api.devnet.solana.com",
}


class SvmPayError(RuntimeError):
    """The requirement cannot be paid on Solana, and the message says why."""


def keypair_from_seed(seed: bytes):
    from solders.keypair import Keypair
    if len(seed) != 32:
        raise SvmPayError(f"a Solana key seed is 32 bytes, got {len(seed)}")
    return Keypair.from_seed(seed)


def address_of(seed: bytes) -> str:
    return str(keypair_from_seed(seed).pubkey())


def associated_token_account(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    from solders.pubkey import Pubkey
    ata, _ = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
         bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM))
    return str(ata)


def _recent_blockhash(network: str, extra: dict[str, Any], rpc_url: str | None) -> str:
    hinted = extra.get("recentBlockhash")
    if isinstance(hinted, str) and hinted:
        return hinted
    import os
    url = rpc_url or os.getenv("EMPORIA_X402_SOLANA_RPC_URL", "") or PUBLIC_RPC.get(network, "")
    if not url:
        raise SvmPayError(f"no RPC for {network}: set EMPORIA_X402_SOLANA_RPC_URL")
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                              "params": [{"commitment": "confirmed"}]}, timeout=15)
    r.raise_for_status()
    try:
        return r.json()["result"]["value"]["blockhash"]
    except (KeyError, TypeError) as e:
        raise SvmPayError(f"the RPC returned no blockhash: {r.text[:200]}") from e


def build_payment_payload(requirement: dict[str, Any], seed: bytes, *,
                          resource: dict[str, Any] | None = None,
                          rpc_url: str | None = None) -> dict[str, Any]:
    """Sign one Solana requirement into an x402 v2 PaymentPayload."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0, to_bytes_versioned
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction

    extra = requirement.get("extra") or {}
    fee_payer, mint = extra.get("feePayer"), requirement.get("asset")
    pay_to, amount = requirement.get("payTo"), requirement.get("amount")
    if not fee_payer:
        raise SvmPayError("the requirement names no feePayer (extra.feePayer)")
    if not mint or not pay_to or amount is None:
        raise SvmPayError("the requirement is missing asset/payTo/amount")
    token_program = extra.get("tokenProgram") or TOKEN_PROGRAM
    if token_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise SvmPayError(f"unexpected token program {token_program}")
    decimals = int(extra.get("decimals", 6))
    memo = extra.get("memo") or secrets.token_hex(16)
    if len(memo.encode()) > 256:
        raise SvmPayError("extra.memo is longer than the 256 bytes the spec allows")

    keypair = keypair_from_seed(seed)
    owner = keypair.pubkey()
    if str(owner) == fee_payer:
        raise SvmPayError("the fee payer must not be the payer")
    source = Pubkey.from_string(associated_token_account(str(owner), mint, token_program))
    dest = Pubkey.from_string(associated_token_account(pay_to, mint, token_program))
    instructions = [
        Instruction(Pubkey.from_string(COMPUTE_BUDGET_PROGRAM),
                    bytes([2]) + struct.pack("<I", COMPUTE_UNIT_LIMIT), []),
        Instruction(Pubkey.from_string(COMPUTE_BUDGET_PROGRAM),
                    bytes([3]) + struct.pack("<Q", COMPUTE_UNIT_PRICE), []),
        Instruction(Pubkey.from_string(token_program),
                    bytes([12]) + struct.pack("<Q", int(amount)) + bytes([decimals]),
                    [AccountMeta(source, is_signer=False, is_writable=True),
                     AccountMeta(Pubkey.from_string(mint), is_signer=False, is_writable=False),
                     AccountMeta(dest, is_signer=False, is_writable=True),
                     AccountMeta(owner, is_signer=True, is_writable=False)]),
        Instruction(Pubkey.from_string(MEMO_PROGRAM), memo.encode(), []),
    ]
    blockhash = _recent_blockhash(requirement.get("network") or "", extra, rpc_url)
    message = MessageV0.try_compile(Pubkey.from_string(fee_payer), instructions, [],
                                    Hash.from_string(blockhash))
    # Signature slots follow the message's signers: the fee payer first (left
    # empty for the facilitator), then the payer.
    signers = list(message.account_keys[:message.header.num_required_signatures])
    signatures = [keypair.sign_message(to_bytes_versioned(message)) if k == owner
                  else Signature.default() for k in signers]
    tx = VersionedTransaction.populate(message, signatures)
    payload: dict[str, Any] = {
        "x402Version": 2,
        "accepted": requirement,
        "payload": {"transaction": base64.b64encode(bytes(tx)).decode()},
    }
    if resource:
        payload["resource"] = resource
    return payload
