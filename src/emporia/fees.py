"""The relay's fee: one number, published on its signed card and shown in the terms before any payment.

The relay is a notary and a post office, never a broker: it never holds money. So its fee is not taken from
a pot. A paid entry (a room, an Agora topic, a lab) is two x402 `exact` payments made in one act by the
payer: the creator's share straight to the creator, and the fee straight to the relay's published fee
address. The price the creator set is the total: the fee is part of it (the creator's share is the price
less the fee), so the payer never pays more than the price on the page. Entry is granted only once both
payments have settled. Free rooms stay free.

Settings (environment of the relay):

* `EMPORIA_FEE_BPS`: the fee in basis points; 100 (1%) by default, the same for everyone on the relay.
* `EMPORIA_FEE_ADDRESS`: where the fee is paid on EVM networks (a wallet the operator controls); it is
  also the escrow's fee receiver, so deals pay the same address `emporia setup relay` asks for.
* `EMPORIA_FEE_ADDRESS_SVM`: the same on Solana networks (optional).

With no fee address the fee is skipped everywhere and the card says `fee_bps: 0`. A network whose family has no fee
address is not offered for a paid entry while a fee applies, so the fee cannot be avoided by choosing it.
One published number, `fee_bps`: the same value is the fee on paid entries, deals (escrow capture) and the
application fee of a card charge, and it is 0 for all of them with no fee address. Revisit before mainnet.
"""

from __future__ import annotations

import os

DEFAULT_FEE_BPS = 100
#: A payer refuses a fee leg above this share of the price (the relay's word is not enough).
PAYER_FEE_CAP_BPS = 300


def configured_bps() -> int:
    """`EMPORIA_FEE_BPS`, 100 by default; clamped to 0-1000."""
    raw = os.getenv("EMPORIA_FEE_BPS", "").strip()
    try:
        bps = int(raw) if raw else DEFAULT_FEE_BPS
    except ValueError:
        bps = DEFAULT_FEE_BPS
    return max(0, min(bps, 1000))


def addresses() -> dict[str, str]:
    """{family: fee address} the operator set (`evm`, `svm`)."""
    out = {}
    evm = os.getenv("EMPORIA_FEE_ADDRESS", "").strip()
    if evm:
        out["evm"] = evm
    if svm := os.getenv("EMPORIA_FEE_ADDRESS_SVM", "").strip():
        out["svm"] = svm
    return out


def fee_bps() -> int:
    """The fee on a paid entry, in basis points: 0 when there is no fee address."""
    return configured_bps() if addresses() else 0


def fee_cents(amount_cents: int) -> int:
    """The fee part of `amount_cents`: rounded to the cent, and 0 when it would round below one cent."""
    bps = fee_bps()
    if amount_cents <= 0 or not bps:
        return 0
    fee = round(amount_cents * bps / 10000)
    return fee if fee >= 1 else 0


def split(amount_cents: int) -> tuple[int, int]:
    """(creator's share, fee) in cents; they add up to `amount_cents`."""
    fee = fee_cents(amount_cents)
    return amount_cents - fee, fee


def card() -> dict:
    """What the relay card publishes: `fee_bps`, `fee_address` (EVM), `fee_address_svm` when set."""
    found = addresses()
    return {"fee_bps": fee_bps(), "fee_address": found.get("evm"), "fee_address_svm": found.get("svm")}


def terms(amount_cents: int) -> dict:
    """The fee as a payer is told it before paying: total, creator's share, fee, who gets it."""
    creator, fee = split(amount_cents)
    found = addresses()
    return {"total_cents": amount_cents, "creator_cents": creator, "fee_cents": fee,
            "fee_bps": fee_bps(), "fee_address": found.get("evm"), "fee_address_svm": found.get("svm"),
            "note": ("two payments in one action: the creator's share to the creator, the relay's fee to its "
                     "published fee address; the relay never holds either" if fee else
                     "this relay takes no fee on entries; the whole price goes to the creator")}
