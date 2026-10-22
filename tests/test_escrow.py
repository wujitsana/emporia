"""The Commerce Payments escrow, pure half (P97).

Two values here were read from Base Sepolia on 2026-10-22 and are pinned so the
offline suite catches any drift from what the deployed contracts compute:
the escrow's `getHash` for a fixed PaymentInfo, and USDC's DOMAIN_SEPARATOR.
"""
import pytest

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

from emporia import escrow  # noqa: E402

ESCROW = "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c"
COLLECTOR = "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
CHAIN = 84532


def _info(**kw):
    args = dict(
        operator="0x8B5bECa1Debad054342A128D285a26dbFd54d1d4",
        payer="0x72BE1b0EC70d02FAFCA849638B9218dd6464B382",
        receiver="0x1111111111111111111111111111111111111111",
        token=USDC, amount=1_000_000, fee_bps=250,
        fee_receiver="0x8d96f65aA58421F976D83743B28dbf4929998209",
        now=1_800_000_000, pre_approval_window=3600, authorization_window=86400,
        refund_window=86400, salt=123456789,
    )
    args.update(kw)
    return escrow.build_payment_info(**args)


def test_the_hash_matches_the_deployed_contract():
    """AuthCaptureEscrow.getHash for this exact PaymentInfo, read from chain."""
    assert escrow.payment_info_hash(_info(), chain_id=CHAIN, escrow=ESCROW).hex() == (
        "590cd53e50745c4a5c85e7404de3f60e7838bf57201c425fddc0e6a15a10a4b1")


def test_the_nonce_ignores_the_payer_and_nothing_else():
    """The payer signs a nonce bound to every field but their own address —
    change the receiver, the fee or the fee receiver and the signature is void."""
    base = escrow.payer_agnostic_nonce(_info(), chain_id=CHAIN, escrow=ESCROW)
    assert escrow.payer_agnostic_nonce(
        _info(payer="0x" + "22" * 20), chain_id=CHAIN, escrow=ESCROW) == base
    for change in ({"receiver": "0x" + "33" * 20}, {"fee_bps": 300},
                   {"fee_receiver": "0x" + "44" * 20}, {"amount": 2_000_000}):
        assert escrow.payer_agnostic_nonce(
            _info(**change), chain_id=CHAIN, escrow=ESCROW) != base, change


def test_the_signing_domain_is_usdcs_own():
    """USDC's DOMAIN_SEPARATOR on Base Sepolia, read from chain."""
    msg = escrow.receive_with_authorization_message(
        _info(), chain_id=CHAIN, escrow=ESCROW, collector=COLLECTOR,
        token_name="USDC", token_version="2")
    assert encode_typed_data(full_message=msg).header.hex() == (
        "71f17a3b2ff373b803d70a5a07c046c1a2bc8e89c09ef722fcb047abe94c9818")


def test_the_payer_signs_exactly_what_the_collector_submits():
    info = _info()
    msg = escrow.receive_with_authorization_message(
        info, chain_id=CHAIN, escrow=ESCROW, collector=COLLECTOR,
        token_name="USDC", token_version="2")
    m = msg["message"]
    assert (m["from"], m["to"], m["value"], m["validAfter"], m["validBefore"]) == (
        info.payer, COLLECTOR, info.max_amount, 0, info.pre_approval_expiry)
    payer = Account.create()
    sig = payer.sign_typed_data(full_message=msg).signature
    assert Account.recover_message(encode_typed_data(full_message=msg), signature=sig) == payer.address


def test_the_fee_is_pinned_and_needs_a_real_receiver():
    info = _info()
    assert (info.min_fee_bps, info.max_fee_bps) == (250, 250)
    assert escrow.fee_amount(info, 1_000_000) == 25_000
    with pytest.raises(escrow.EscrowError, match="zero receiver"):
        _info(fee_receiver=escrow.ZERO_ADDRESS)
    assert _info(fee_bps=0, fee_receiver=escrow.ZERO_ADDRESS).max_fee_bps == 0


def test_what_the_contract_would_refuse_is_refused_first():
    with pytest.raises(escrow.EscrowError):
        _info(amount=0)
    with pytest.raises(escrow.EscrowError):
        _info(fee_bps=10_001)
    with pytest.raises(escrow.EscrowError):
        _info(pre_approval_window=0)


def test_the_wire_form_round_trips():
    info = _info(salt=2**255 + 7)
    wire = info.to_json()
    assert wire["salt"] == str(2**255 + 7), "a uint256 must not pass through a JSON number"
    assert escrow.PaymentInfo.from_json(wire) == info
