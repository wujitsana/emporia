"""P155: an agent pays x402 on Solana — the spec's `exact` SVM transaction,
partially signed, the facilitator as fee payer."""
import base64
import json
import os
import stat
from unittest.mock import patch

import pytest

pytest.importorskip("solders")
from solders.keypair import Keypair  # noqa: E402
from solders.message import to_bytes_versioned  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from emporia import agent_wallet, identity, x402_payer, x402_svm  # noqa: E402
from emporia.agent_wallet import AgentWalletError  # noqa: E402

DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
USDC_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
FEE_PAYER = str(Keypair().pubkey())
PAY_TO = str(Keypair().pubkey())


def _requirement(amount=250_000, **extra):
    return {"scheme": "exact", "network": DEVNET, "asset": USDC_DEVNET, "payTo": PAY_TO,
            "amount": str(amount), "maxTimeoutSeconds": 300,
            "extra": {"feePayer": FEE_PAYER, "recentBlockhash": "11111111111111111111111111111111", **extra}}


def _decode(payload):
    return VersionedTransaction.from_bytes(base64.b64decode(payload["payload"]["transaction"]))


def test_the_transaction_is_exactly_what_the_spec_asks_for():
    seed = os.urandom(32)
    tx = _decode(x402_svm.build_payment_payload(_requirement(memo="order-17"), seed))
    m = tx.message
    payer = str(Keypair.from_seed(seed).pubkey())
    assert m.header.num_required_signatures == 2
    assert [str(k) for k in m.account_keys[:2]] == [FEE_PAYER, payer], "fee payer first, then the payer"
    assert tx.signatures[0] == tx.signatures[0].default(), "the facilitator's slot is left empty"
    assert tx.signatures[1].verify(m.account_keys[1], to_bytes_versioned(m))
    programs = [str(m.account_keys[ix.program_id_index]) for ix in m.instructions]
    assert programs == [x402_svm.COMPUTE_BUDGET_PROGRAM, x402_svm.COMPUTE_BUDGET_PROGRAM,
                        x402_svm.TOKEN_PROGRAM, x402_svm.MEMO_PROGRAM]
    limit, price, transfer, memo = (bytes(ix.data) for ix in m.instructions)
    assert limit[0] == 2 and price[0] == 3, "SetComputeUnitLimit, then SetComputeUnitPrice"
    assert int.from_bytes(price[1:], "little") <= 5_000_000, "within 5 lamports per CU"
    assert transfer[0] == 12 and int.from_bytes(transfer[1:9], "little") == 250_000 and transfer[9] == 6
    dest = m.account_keys[m.instructions[2].accounts[2]]
    assert str(dest) == x402_svm.associated_token_account(PAY_TO, USDC_DEVNET)
    assert memo == b"order-17"
    keys_in_instructions = {str(m.account_keys[i]) for ix in m.instructions for i in ix.accounts}
    assert FEE_PAYER not in keys_in_instructions, "the fee payer only pays fees"


def test_without_a_hinted_memo_a_random_nonce_is_used():
    tx = _decode(x402_svm.build_payment_payload(_requirement(), os.urandom(32)))
    memo = bytes(tx.message.instructions[3].data)
    assert len(memo) == 32 and int(memo, 16) >= 0


def test_no_fee_payer_no_payment():
    req = _requirement()
    del req["extra"]["feePayer"]
    with pytest.raises(x402_svm.SvmPayError, match="feePayer"):
        x402_svm.build_payment_payload(req, os.urandom(32))


@pytest.fixture
def wallet_env(tmp_path):
    env = {"EMPORIA_X402_PAY_NETWORKS": DEVNET, "EMPORIA_X402_MAX_PAYMENT_CENTS": "500",
           "EMPORIA_X402_MAX_DAILY_CENTS": "60"}
    with patch.object(identity, "KEY_DIR", tmp_path), patch.dict(os.environ, env):
        yield tmp_path


def _challenge(amount):
    return {"x402": {"x402Version": 2, "resource": {"url": "http://relay/rooms/r1/join"},
                     "accepts": [_requirement(amount)]}}


def test_an_agent_with_a_solana_wallet_pays_on_solana_within_its_caps(wallet_env):
    addr = agent_wallet.generate_solana("sol_payer")
    path = agent_wallet.solana_wallet_path("sol_payer")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(path.read_bytes()) == 32
    assert agent_wallet.solana_address("sol_payer") == addr
    header = agent_wallet.sign_exact("sol_payer", _challenge(250_000))
    payload = json.loads(base64.b64decode(header))
    assert payload["accepted"]["network"] == DEVNET
    assert str(_decode(payload).message.account_keys[1]) == addr
    agent_wallet.sign_exact("sol_payer", _challenge(250_000))
    with pytest.raises(AgentWalletError, match="daily cap"):
        agent_wallet.sign_exact("sol_payer", _challenge(250_000))   # $0.75 > $0.60 a day
    with pytest.raises(AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact("sol_payer", _challenge(6_000_000))


def test_a_payer_without_a_solana_wallet_does_not_pick_solana():
    challenge = {"accepts": [_requirement()]}
    with pytest.raises(x402_payer.X402PayerError, match="no svm wallet"):
        x402_payer.select_requirement(challenge, networks=[DEVNET], families=("evm",))
    assert x402_payer.select_requirement(challenge, networks=[DEVNET], families=("evm", "svm"))
