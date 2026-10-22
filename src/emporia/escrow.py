"""Commerce Payments escrow for x402 holds — the pure half (P97).

Base's Commerce Payments Protocol (`AuthCaptureEscrow`, audited, live on Base
and Base Sepolia at the same addresses) gives USDC the hold-then-capture shape a
card has: the payer signs once, an *operator* authorizes the hold on chain, and
later either captures it to the receiver or voids it back to the payer. If the
operator never acts, the payer reclaims it after `authorizationExpiry`.

What makes it non-custodial is what the payer signs. Their ERC-3009
`ReceiveWithAuthorization` uses as its nonce the escrow's hash of the whole
`PaymentInfo` with the payer field blanked (`TokenCollector.
_getHashPayerAgnostic`) — so the receiver, the token, the maximum, every expiry,
the fee range and the fee receiver are all fixed by the payer's signature. The
operator (this relay) can capture to that receiver or void; it cannot send the
money anywhere else, and the contract bounds its fee to the signed range.

This module is pure: it builds `PaymentInfo`, hashes it exactly as
`AuthCaptureEscrow.getHash` does, and produces the EIP-712 message the payer
signs. It touches no network and holds no key; the chain calls live in
`escrow_chain.py`. Verified against the deployed contract's own `getHash` in
the opt-in testnet lane.

Source: github.com/base/commerce-payments v1.1.0 (`src/AuthCaptureEscrow.sol`,
`src/collectors/ERC3009PaymentCollector.sol`, `TokenCollector.sol`).
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

#: `AuthCaptureEscrow.PAYMENT_INFO_TYPEHASH`, verbatim from the contract.
PAYMENT_INFO_TYPE = (
    "PaymentInfo(address operator,address payer,address receiver,address token,"
    "uint120 maxAmount,uint48 preApprovalExpiry,uint48 authorizationExpiry,"
    "uint48 refundExpiry,uint16 minFeeBps,uint16 maxFeeBps,address feeReceiver,uint256 salt)"
)
PAYMENT_INFO_TYPEHASH = keccak(text=PAYMENT_INFO_TYPE)
_PAYMENT_INFO_ABI = ["bytes32", "address", "address", "address", "address", "uint120",
                     "uint48", "uint48", "uint48", "uint16", "uint16", "address", "uint256"]

ZERO_ADDRESS = "0x" + "00" * 20
MAX_FEE_BPS = 10_000
_UINT48 = 2**48 - 1
_UINT120 = 2**120 - 1


class EscrowError(ValueError):
    """A PaymentInfo the contract would reject, caught before any chain call."""


@dataclass(frozen=True)
class PaymentInfo:
    """`AuthCaptureEscrow.PaymentInfo`, field for field and in order."""

    operator: str
    payer: str
    receiver: str
    token: str
    max_amount: int
    pre_approval_expiry: int
    authorization_expiry: int
    refund_expiry: int
    min_fee_bps: int
    max_fee_bps: int
    fee_receiver: str
    salt: int

    def as_tuple(self) -> tuple:
        """In the contract's field order, for ABI-encoding a call."""
        return (self.operator, self.payer, self.receiver, self.token, self.max_amount,
                self.pre_approval_expiry, self.authorization_expiry, self.refund_expiry,
                self.min_fee_bps, self.max_fee_bps, self.fee_receiver, self.salt)

    def to_json(self) -> dict:
        """Wire form for a 402 offer: integers as decimal strings (uint256 salts
        overflow JSON numbers in most clients)."""
        return {k: (str(v) if isinstance(v, int) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, data: dict) -> "PaymentInfo":
        fields = {f: data[f] for f in cls.__dataclass_fields__}
        for f in ("max_amount", "pre_approval_expiry", "authorization_expiry",
                  "refund_expiry", "min_fee_bps", "max_fee_bps", "salt"):
            fields[f] = int(fields[f])
        for f in ("operator", "payer", "receiver", "token", "fee_receiver"):
            fields[f] = to_checksum_address(fields[f])
        return cls(**fields)


def build_payment_info(
    *, operator: str, payer: str, receiver: str, token: str, amount: int,
    fee_bps: int, fee_receiver: str, now: int, pre_approval_window: int,
    authorization_window: int, refund_window: int, salt: int | None = None,
) -> PaymentInfo:
    """A PaymentInfo for one purchase, refused here if the contract would refuse it.

    The fee is pinned: min == max == `fee_bps`, so the operator has no discretion
    over it at capture. `fee_receiver` must be a real address — the contract lets
    a zero fee receiver be chosen by the operator at capture, which is exactly
    the discretion this relay should not have.
    """
    if not (0 < amount <= _UINT120):
        raise EscrowError(f"amount {amount} is out of range")
    if not (0 <= fee_bps <= MAX_FEE_BPS):
        raise EscrowError(f"fee_bps {fee_bps} is out of range")
    if fee_bps and to_checksum_address(fee_receiver) == to_checksum_address(ZERO_ADDRESS):
        raise EscrowError("a fee needs a fee receiver; a zero receiver would let the "
                          "operator choose it at capture")
    pre = now + pre_approval_window
    auth = pre + authorization_window
    refund = auth + refund_window
    if not (now < pre <= auth <= refund <= _UINT48):
        raise EscrowError("expiries must be in the future and ordered "
                          "preApproval <= authorization <= refund")
    return PaymentInfo(
        operator=to_checksum_address(operator), payer=to_checksum_address(payer),
        receiver=to_checksum_address(receiver), token=to_checksum_address(token),
        max_amount=amount, pre_approval_expiry=pre, authorization_expiry=auth,
        refund_expiry=refund, min_fee_bps=fee_bps, max_fee_bps=fee_bps,
        fee_receiver=to_checksum_address(fee_receiver),
        salt=secrets.randbits(256) if salt is None else salt,
    )


def payment_info_hash(info: PaymentInfo, *, chain_id: int, escrow: str) -> bytes:
    """`AuthCaptureEscrow.getHash(info)`:
    keccak(abi.encode(chainid, escrow, keccak(abi.encode(TYPEHASH, info))))."""
    inner = keccak(encode(_PAYMENT_INFO_ABI, [PAYMENT_INFO_TYPEHASH, *info.as_tuple()]))
    return keccak(encode(["uint256", "address", "bytes32"],
                         [chain_id, to_checksum_address(escrow), inner]))


def payer_agnostic_nonce(info: PaymentInfo, *, chain_id: int, escrow: str) -> bytes:
    """The ERC-3009 nonce the payer signs: the hash with payer set to zero
    (`TokenCollector._getHashPayerAgnostic`)."""
    blanked = PaymentInfo(**{**asdict(info), "payer": ZERO_ADDRESS})
    return payment_info_hash(blanked, chain_id=chain_id, escrow=escrow)


def receive_with_authorization_message(
    info: PaymentInfo, *, chain_id: int, escrow: str, collector: str,
    token_name: str, token_version: str,
) -> dict:
    """The EIP-712 message the payer signs, in eth-account `full_message` form.

    Exactly what `ERC3009PaymentCollector` submits: from the payer, to the
    collector, value `maxAmount`, valid from 0 until `preApprovalExpiry`, with
    the payer-agnostic hash as nonce. `token_name`/`token_version` are the
    token's own EIP-712 domain (USDC on Base Sepolia: "USDC", "2"), taken from
    configuration rather than guessed — the same rule `_x402_extra_for` follows.
    """
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "ReceiveWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "ReceiveWithAuthorization",
        "domain": {"name": token_name, "version": token_version, "chainId": chain_id,
                   "verifyingContract": to_checksum_address(info.token)},
        "message": {
            "from": info.payer, "to": to_checksum_address(collector),
            "value": info.max_amount, "validAfter": 0,
            "validBefore": info.pre_approval_expiry,
            "nonce": payer_agnostic_nonce(info, chain_id=chain_id, escrow=escrow),
        },
    }


def fee_amount(info: PaymentInfo, amount: int) -> int:
    """The fee to pass to `capture`: within the contract's bounds by construction
    (`amount * bps / 10000`, min == max)."""
    return amount * info.min_fee_bps // MAX_FEE_BPS
