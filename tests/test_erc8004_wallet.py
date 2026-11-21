"""The agent's own wallet and ERC-8004 (docs/protocol.md section 3.20): minting from it, setting the identity's
agentWallet, and the guards around both. The typed data and selectors were checked live on Base Sepolia on 11-16
(tests/fixtures/erc8004/set_agent_wallet_live.json); no test touches a network. The wallet's key is a throwaway made
for this file, and every flow is checked for it never appearing in any output."""
import copy
import json
import pathlib
import uuid

import pytest

from emporia import agent_card_cli as card
from emporia import chain_link, chain_read, chain_register, chain_send, chain_wallet, erc8004, mcp_server
from emporia import erc8004_register as R
from emporia import erc8004_wallet as W
from emporia.networks import ERC8004_REGISTRIES
from tests.test_x_link_cli import FakeTarget

pytestmark = pytest.mark.skipif(not erc8004.available(), reason="eth-account is not installed")
FXD = pathlib.Path(__file__).parent / "fixtures" / "erc8004"
LIVE = json.loads((FXD / "set_agent_wallet_live.json").read_text())
REAL = json.loads((FXD / "register_receipt.json").read_text())
SEPOLIA = "eip155:84532"
REGISTRY = ERC8004_REGISTRIES[SEPOLIA]
STABLE = "cd" * 32
AID = LIVE["agent_id"]
OWNER = LIVE["owner"]
KEY = "0x" + "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"      # throwaway, test only
relay_url = "https://relay.example.org"
NOW = 1_794_852_899


def _account():
    from eth_account import Account
    return Account.from_key(KEY)


class Signer:
    """What `agent_wallet.evm_signer` gives: `.address`, `.sign_typed_data`, `.sign_transaction`. Counts its uses."""

    def __init__(self, key=KEY, fail_with=None):
        from eth_account import Account
        self._a = Account.from_key(key)
        self.address = self._a.address
        self.fail_with, self.signed_tx, self.signed_typed = fail_with, [], []

    def sign_typed_data(self, full_message):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.signed_typed.append(full_message)
        return "0x" + bytes(self._a.sign_typed_data(full_message=full_message).signature).hex()

    def sign_transaction(self, tx):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.signed_tx.append(tx)
        return bytes(self._a.sign_transaction(tx).raw_transaction)


class Chain:
    """An in-memory Base Sepolia: the registry's own checks for `setAgentWallet` (owner sends, deadline window, the new
    wallet's signature over the typed data with the owner in it), balances, nonces and fees."""

    def __init__(self):
        self.caip2, self.block_number, self.block_hash = SEPOLIA, 1_000, "0x" + "ab" * 32
        self.owners, self.wallets, self.uris, self.receipts, self.block_hashes = {AID: OWNER}, {AID: OWNER}, {}, {}, {}
        self.balance_wei, self.nonce_n, self.priority, self.base = 10**17, 7, 10**6, 5_000_000
        self.gas, self.now, self.sent, self.calls = 200_000, NOW, [], []
        self.revert = self.down = False
        self.send_error = None
        self.sim_id = 10729

    # reads
    def call(self, to, data):
        sel, n = data[:4], int.from_bytes(data[4:36], "big")
        if sel == erc8004.SEL_OWNER_OF:
            if n not in self.owners:
                raise chain_read.ChainRevert("none")
            return bytes(12) + bytes.fromhex(self.owners[n][2:])
        if sel == W.SEL_GET_AGENT_WALLET:
            return bytes(12) + bytes.fromhex(self.wallets.get(n, W.ZERO)[2:])
        if sel == R.SEL_TOKEN_URI:
            from eth_abi import encode
            return encode(["string"], [self.uris[n]])
        raise chain_read.ChainRevert("no such function")

    def code(self, address):
        return b""                      # every account here is an ordinary one

    def receipt(self, tx_hash):
        return self.receipts.get(tx_hash)

    def block_hash_at(self, number):
        return self.block_hashes.get(number, "0x" + "00" * 32)

    # what a send needs
    def estimate_gas(self, to, data, sender):
        self.calls.append(("gas", sender))
        if self.down:
            raise chain_read.ChainUnknown("down")
        return self.gas

    def fee_data(self):
        return self.priority, self.base

    def balance(self, address):
        return self.balance_wei

    latest_nonce = None

    def nonce(self, address, tag="pending"):
        return self.nonce_n if tag == "pending" or self.latest_nonce is None else self.latest_nonce

    def chain_time(self):
        return self.now

    def simulate(self, to, data, sender):
        self.calls.append(("simulate", sender, bytes(data)))
        if self.down:
            raise chain_read.ChainUnknown("down")
        if self.revert:
            raise chain_read.ChainRevert("no")
        if data[:4] == R.SEL_REGISTER_URI:
            return self.sim_id.to_bytes(32, "big")
        if data[:4] != W.SEL_SET_AGENT_WALLET:
            raise chain_read.ChainRevert("no such function")
        from eth_abi import decode
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        aid, new, deadline, sig = decode(["uint256", "address", "uint256", "bytes"], data[4:])
        if sender.lower() != self.owners[aid].lower():
            raise chain_read.ChainRevert("Not authorized")
        if self.now > deadline:
            raise chain_read.ChainRevert("expired")
        if deadline > self.now + 300:
            raise chain_read.ChainRevert("deadline too far")
        td = W.consent_typed_data(84532, REGISTRY, aid, new, self.owners[aid], deadline)
        if Account.recover_message(encode_typed_data(full_message=td), signature=sig).lower() != new.lower():
            raise chain_read.ChainRevert("invalid wallet sig")
        return b""

    def send_raw(self, raw):
        if self.send_error:
            raise self.send_error
        from eth_utils import keccak
        self.sent.append(bytes(raw))
        return "0x" + keccak(bytes(raw)).hex()


@pytest.fixture
def chain(monkeypatch):
    c = Chain()
    monkeypatch.setattr(chain_register, "_reader", lambda caip2, read=None, tag="finalized": c)
    return c


@pytest.fixture
def wallet(monkeypatch):
    s = Signer()
    from emporia import agent_wallet
    monkeypatch.setattr(agent_wallet, "evm_signer", lambda agent_id: s)
    monkeypatch.setattr(agent_wallet, "remote", lambda: None)
    for k in [k for k in __import__("os").environ if k.startswith("HERMES_SESSION_")]:
        monkeypatch.delenv(k)
    return s


@pytest.fixture
def target(monkeypatch, wallet):
    name = f"walletagent_{uuid.uuid4().hex[:6]}"
    t = FakeTarget(name)
    monkeypatch.setattr(card, "_Target", lambda n: t)
    monkeypatch.setattr(mcp_server, "AGENT_ID", name)
    monkeypatch.setattr(mcp_server, "RELAY_URL", relay_url)
    monkeypatch.setattr(mcp_server, "_signing_identity", lambda aid: STABLE)
    monkeypatch.setattr(chain_register, "_about", lambda: "Wanders the Agora.")
    monkeypatch.setattr(chain_register, "_x402_support", lambda: True)
    return t


@pytest.fixture
def terminal(monkeypatch):
    """A person's terminal that answers y."""
    from emporia import cli
    asked = []
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, d="": asked.append(q) or "y")
    return asked


def owned_by_wallet(chain, wallet):
    chain.owners[AID] = wallet.address.lower()
    chain.wallets[AID] = wallet.address.lower()


def no_key(*texts):
    bare = KEY[2:].lower()
    for t in texts:
        assert bare not in str(t).lower(), "the private key leaked"


# -- the registry's call, as checked live -----------------------------------------------------------------------
def test_selectors_topic_and_typehash_are_the_keccak_of_their_text():
    from eth_utils import keccak
    for sig, sel in W.SELECTORS.items():
        assert keccak(text=sig)[:4] == sel, sig
    assert "0x" + keccak(text=W.TYPE_STRING).hex() == LIVE["typehash"]
    assert "0x" + keccak(text="MetadataSet(uint256,string,string,bytes)").hex() == W.TOPIC_METADATA_SET
    assert W.DEADLINE_SECONDS < W.MAX_DEADLINE_SECONDS == 300


def test_the_consent_digest_is_what_the_registrys_own_formula_gives():
    """keccak(0x1901 || domainSeparator || structHash), built by hand from the live eip712Domain() answer."""
    from eth_abi import encode
    from eth_utils import keccak
    dom = LIVE["eip712Domain"]
    new, deadline = _account().address, NOW + 240
    td = W.consent_typed_data(84532, REGISTRY, AID, new, OWNER, deadline)
    assert td["domain"]["name"] == dom["name"] and td["domain"]["version"] == dom["version"]
    assert td["domain"]["chainId"] == dom["chainId"] and td["domain"]["verifyingContract"].lower() == dom["verifyingContract"]
    dtype = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    sep = keccak(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"],
                        [dtype, keccak(text=dom["name"]), keccak(text=dom["version"]), dom["chainId"], REGISTRY]))
    struct = keccak(encode(["bytes32", "uint256", "address", "address", "uint256"],
                           [bytes.fromhex(LIVE["typehash"][2:]), AID, new, OWNER, deadline]))
    from eth_account.messages import encode_typed_data
    m = encode_typed_data(full_message=td)
    assert keccak(b"\x19" + m.version + m.header + m.body) == keccak(b"\x19\x01" + sep + struct)


def test_the_registrys_checks_accept_our_consent_and_refuse_the_cases_checked_live(chain):
    """The in-memory registry applies the contract's rules (owner, window, signature over the owner-bound typed data);
    the live results for the same cases are in the fixture."""
    from eth_account import Account
    new, other = _account(), Account.create()
    dl = NOW + W.DEADLINE_SECONDS

    def attempt(signer, deadline=dl, owner=OWNER, frm=OWNER):
        td = W.consent_typed_data(84532, REGISTRY, AID, new.address, owner, deadline)
        from eth_account.messages import encode_typed_data
        sig = signer.sign_message(encode_typed_data(full_message=td)).signature
        return chain.simulate(REGISTRY, W.set_wallet_calldata(AID, new.address, deadline, bytes(sig)), frm)

    assert attempt(new) == b""
    for bad, words in ((lambda: attempt(other), "invalid wallet sig"), (lambda: attempt(new, owner=other.address), "invalid wallet sig"),
                       (lambda: attempt(new, frm=other.address), "Not authorized"), (lambda: attempt(new, deadline=NOW - 10), "expired"),
                       (lambda: attempt(new, deadline=NOW + 3600), "deadline too far")):
        with pytest.raises(chain_read.ChainRevert, match=words):
            bad()
    assert set(LIVE["live_simulations"].values()) >= {"accepted (returns 0x)", "execution reverted: invalid wallet sig",
                                                       "execution reverted: Not authorized", "execution reverted: expired",
                                                       "execution reverted: deadline too far"}


def test_the_receipt_of_a_wallet_change_is_read_from_the_registrys_own_metadata_log():
    rec = copy.deepcopy(REAL["receipt"])
    rec["logs"] = [l for l in rec["logs"] if l["topics"][0] == W.TOPIC_METADATA_SET]
    got = W.wallet_from_receipt(rec, REGISTRY, rec["transactionHash"], 10727)
    assert got["wallet"].startswith("0xcea60a35") and got["block_number"] == int(rec["blockNumber"], 16)
    for over, words in (({"status": "0x0"}, "failed"), ({"to": "0x" + "11" * 20}, "not sent to this chain"),
                        ({"transactionHash": "0x" + "00" * 32}, "another transaction")):
        bad = {**rec, **over}
        with pytest.raises(W.WalletError, match=words):
            W.wallet_from_receipt(bad, REGISTRY, rec["transactionHash"], 10727)
    with pytest.raises(W.WalletError, match="did not set"):
        W.wallet_from_receipt(rec, REGISTRY, rec["transactionHash"], 10728)         # another agent's metadata
    foreign = copy.deepcopy(rec)
    foreign["logs"][0]["address"] = "0x" + "22" * 20
    with pytest.raises(W.WalletError, match="did not set"):
        W.wallet_from_receipt(foreign, REGISTRY, rec["transactionHash"], 10727)
    with pytest.raises(W.WalletError, match="no receipt"):
        W.wallet_from_receipt(None, REGISTRY, rec["transactionHash"], 10727)


# -- the image seam (the picture itself is not built here) -------------------------------------------------------
def test_the_registration_file_takes_an_optional_data_image_and_nothing_else():
    plain = R.registration_file("kestrel", "x", relay_url, STABLE, True)
    assert "image" not in plain
    svg = "data:image/svg+xml;base64,PHN2Zy8+"
    assert R.registration_file("kestrel", "x", relay_url, STABLE, True, svg)["image"] == svg
    for bad in ("https://example.org/a.png", "ipfs://x", "data:text/html;base64,AA==", "data:image/png;base64," + "A" * 3000):
        with pytest.raises(R.RegisterError):
            R.registration_file("kestrel", "x", relay_url, STABLE, True, bad)


# -- quote and send, the guards ---------------------------------------------------------------------------------
def quote(chain, max_gas=300_000, **over):
    for k, v in over.items():
        setattr(chain, k, v)
    return chain_send.quote(chain, "0x" + "11" * 20, REGISTRY, b"\x01", max_gas, chain_id=84532)


def test_a_quote_caps_gas_at_max_gas_and_prices_the_worst_case(chain):
    q = quote(chain)
    assert q["gas_estimate"] == 200_000 and q["gas_limit"] == 250_000 <= q["max_gas"]
    assert q["max_fee_per_gas"] == 10**6 + 2 * 5_000_000 and q["max_cost_wei"] == 250_000 * q["max_fee_per_gas"]
    assert quote(chain, 210_000)["gas_limit"] == 210_000, "the limit never passes --max-gas"


@pytest.mark.parametrize("max_gas", [0, "", None, "abc", -5])
def test_max_gas_is_required(chain, max_gas):
    with pytest.raises(chain_link.LinkError, match="--max-gas is required"):
        quote(chain, max_gas)
    assert chain.calls == [], "nothing is asked of the chain first"


def test_an_estimate_over_max_gas_stops_and_a_low_balance_says_where_to_get_test_eth(chain):
    with pytest.raises(chain_link.LinkError, match="over your --max-gas"):
        quote(chain, 100_000)
    with pytest.raises(chain_link.LinkError, match=r"get Base Sepolia test ETH for 0x1111111111111111111111111111111111111111"):
        quote(chain, balance_wei=1_000)


def test_an_absurd_fee_quote_is_not_believed_and_a_reverting_call_is_not_sent(chain):
    with pytest.raises(chain_link.LinkError, match="above the"):
        quote(chain, priority=10**12)
    chain.priority = 10**6

    class Reverting(Chain):
        def estimate_gas(self, *a):
            raise chain_read.ChainRevert("nope")
    with pytest.raises(chain_link.LinkError, match="would refuse"):
        chain_send.quote(Reverting(), "0x" + "11" * 20, REGISTRY, b"\x01", 300_000, chain_id=84532)


@pytest.mark.parametrize("chain_id", [8453, 1, 46630, 4217])
def test_only_base_sepolia_is_ever_sent_on(chain, chain_id):
    with pytest.raises(chain_link.LinkError, match="Base Sepolia"):
        chain_send.quote(chain, "0x" + "11" * 20, REGISTRY, b"\x01", 300_000, chain_id=chain_id)
    with pytest.raises(chain_link.LinkError, match="Base Sepolia"):
        chain_send.send(chain, Signer(), {"chain_id": chain_id}, b"")
    assert chain.sent == []


def test_send_signs_one_eip1559_transaction_that_recovers_to_the_wallet(chain, wallet):
    q = quote(chain)
    q["sender"], q["nonce"] = wallet.address, 7
    h = chain_send.send(chain, wallet, q, b"\xde\xad")
    assert len(chain.sent) == 1 and h.startswith("0x") and len(h) == 66
    from eth_account import Account
    raw = chain.sent[0]
    assert raw[0] == 2, "a type-2 (EIP-1559) transaction"
    assert Account.recover_transaction(raw) == wallet.address
    tx = wallet.signed_tx[0]
    assert tx["chainId"] == 84532 and tx["to"] == REGISTRY and tx["value"] == 0 and tx["data"] == "0xdead"
    assert tx["gas"] <= 300_000 and tx["nonce"] == 7 and tx["maxFeePerGas"] == q["max_fee_per_gas"]


def test_a_lost_answer_still_gives_the_person_the_hash_and_a_refusal_says_nothing_was_sent(chain, wallet):
    q = quote(chain)
    q["sender"] = wallet.address
    chain.send_error = chain_read.ChainUnknown("timed out")
    with pytest.raises(chain_link.LinkError, match=r"may still have arrived.*--tx 0x[0-9a-f]{64}"):
        chain_send.send(chain, wallet, q, b"")
    chain.send_error = chain_read.ChainRpcError("the RPC returned an error: nonce too low", -32000)
    with pytest.raises(chain_link.LinkError, match="nothing was sent"):
        chain_send.send(chain, wallet, q, b"")


def test_a_signer_error_that_echoes_the_key_is_never_shown(chain):
    evil = Signer(fail_with=f"bad key {KEY} {KEY[2:]}")
    q = quote(chain)
    with pytest.raises(chain_link.LinkError) as e:
        chain_send.send(chain, evil, q, b"")
    no_key(e.value, repr(e.value))
    assert "RuntimeError" in str(e.value)


# -- §1 the mint from the agent's wallet ------------------------------------------------------------------------
def test_quote_register_uses_the_wallet_as_sender_and_gives_a_digest(chain, wallet, target):
    out = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    assert out["wallet"] == wallet.address and out["quote"]["sender"] == wallet.address
    assert chain.calls[0] == ("simulate", wallet.address, bytes.fromhex(out["calldata"][2:])) or chain.calls[0][1] == wallet.address
    assert out["calldata_digest"].startswith("0x") and out["would_register_id"] == "10729"


def test_send_register_sends_exactly_one_transaction_with_the_registration(chain, wallet, target):
    q = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    out = chain_register.send_register("base-sepolia", 400_000, q["calldata_digest"], q["quote"], stable=STABLE)
    assert len(chain.sent) == 1 and out["tx_hash"].startswith("0x") and out["wallet"] == wallet.address
    tx = wallet.signed_tx[0]
    assert tx["to"] == REGISTRY and tx["data"] == q["calldata"] and tx["gas"] <= 400_000 and tx["chainId"] == 84532


def test_a_registration_that_changed_since_the_question_is_not_sent(chain, wallet, target):
    with pytest.raises(chain_link.LinkError, match="changed since you confirmed"):
        q = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
        chain_register.send_register("base-sepolia", 400_000, "0x" + "00" * 32, q["quote"], stable=STABLE)
    assert chain.sent == []


@pytest.mark.parametrize("name", ["base", "robinhood", "tempo"])
def test_the_agent_wallet_never_sends_on_any_other_chain(chain, wallet, target, name):
    with pytest.raises(chain_link.LinkError, match="release owner's go|no ERC-8004 registry"):
        chain_register.quote_register(name, 400_000, stable=STABLE)
    with pytest.raises(chain_link.LinkError, match="release owner's go|no ERC-8004 registry"):
        chain_register.send_register(name, 400_000, "0x00", {}, stable=STABLE)
    assert chain.sent == [] and chain.calls == []


def test_behind_the_operator_signer_or_without_a_wallet_nothing_is_sent(chain, monkeypatch, target):
    from emporia import agent_wallet
    monkeypatch.setattr(agent_wallet, "remote", lambda: object())
    with pytest.raises(chain_link.LinkError, match="operator signer"):
        chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    monkeypatch.setattr(agent_wallet, "remote", lambda: None)

    def none(agent_id):
        raise agent_wallet.AgentWalletError("kestrel has no x402 wallet.")
    monkeypatch.setattr(agent_wallet, "evm_signer", none)
    with pytest.raises(chain_link.LinkError, match="no x402 wallet"):
        chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    assert chain.sent == []


def test_register_from_the_agent_wallet_asks_sends_once_and_goes_on_as_tx(chain, wallet, target, terminal, monkeypatch, capsys):
    followed = []
    monkeypatch.setattr(card, "_register_after", lambda *a, **k: followed.append((a[3], k)) or 0)
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 0
    out = capsys.readouterr().out
    for want in (wallet.address, "gas:", "max fee:", "test ETH", "Sent transaction 0x", "only thing it sends"):
        assert want in out, want
    assert len(terminal) == 1 and "Send it from" in terminal[0]
    assert len(chain.sent) == 1 and len(followed) == 1 and followed[0][1]["waiting"] is True
    no_key(out)


def test_declining_the_question_sends_nothing(chain, wallet, target, monkeypatch, capsys):
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, d="": "n")
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 0
    assert "nothing sent" in capsys.readouterr().out and chain.sent == []


def test_it_wants_max_gas_and_a_person_and_refuses_the_other_ways_with_it(chain, wallet, target, terminal, capsys):
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True) == 1
    assert "--max-gas is required" in capsys.readouterr().out
    for kw in ({"browser": True}, {"tx": "0x" + "ab" * 32}, {"sender": "0x" + "11" * 20}):
        assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=1, **kw) == 1
        assert "do not give" in capsys.readouterr().out
    assert card.register_8004(target.name, "base-sepolia", max_gas=1) == 1
    assert "goes with --from-agent-wallet" in capsys.readouterr().out
    assert chain.sent == []


def test_a_terminal_that_is_not_a_persons_is_refused_even_when_the_flags_are_right(chain, wallet, target, monkeypatch, capsys):
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 1
    assert "your own terminal" in capsys.readouterr().out and chain.sent == []


def test_an_agent_session_cannot_send_from_the_wallet_even_with_yes(chain, wallet, target, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    for fn in (lambda: card.register_8004(target.name, "base-sepolia", yes=True, from_agent_wallet=True, max_gas=400_000),
               lambda: card.set_8004_wallet(target.name, "base-sepolia", str(AID), yes=True, from_agent_wallet=True, max_gas=400_000),
               lambda: card.set_8004_wallet(target.name, "base-sepolia", str(AID)),
               lambda: card.link_8004(target.name, "base-sepolia", str(AID), yes=True, from_agent_wallet=True)):
        assert fn() == 1
        assert "person's to change" in capsys.readouterr().out
    assert chain.sent == [] and wallet.signed_tx == [] and wallet.signed_typed == []


def test_a_live_unattended_turn_blocks_it(chain, wallet, target, capsys):
    target.live = "relay-started"
    assert card.register_8004(target.name, "base-sepolia", yes=True, from_agent_wallet=True, max_gas=400_000) == 1
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), yes=True, from_agent_wallet=True, max_gas=1) == 1
    assert capsys.readouterr().out.count("turn is running") == 2 and chain.sent == []


def test_mainnet_is_refused_by_the_command(chain, wallet, target, terminal, capsys):
    assert card.register_8004(target.name, "base", from_agent_wallet=True, max_gas=400_000) == 1
    assert "release owner's go" in capsys.readouterr().out
    assert card.set_8004_wallet(target.name, "base", str(AID), from_agent_wallet=True, max_gas=400_000) == 1
    assert "release owner's go" in capsys.readouterr().out and chain.sent == []


def test_a_wallet_without_test_eth_is_told_where_to_get_it_and_nothing_is_sent(chain, wallet, target, terminal, capsys):
    chain.balance_wei = 5
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 1
    assert f"get Base Sepolia test ETH for {wallet.address}" in capsys.readouterr().out and chain.sent == []


def test_after_the_mint_an_identity_the_wallet_owns_is_linked_by_the_wallet_itself(chain, wallet, target, terminal, monkeypatch, capsys):
    """`--tx` for a mint owned by the agent's own wallet: confirm says so, and the link is signed by the wallet."""
    from tests.test_erc8004_register import minted
    chain.block_number = 10**9
    minted_chain = minted(_As(chain))
    owned = [l for l in REAL["receipt"]["logs"] if l["topics"][0] == R.TOPIC_REGISTERED][0]["topics"][2][-40:]
    chain.owners[10727] = "0x" + owned
    monkeypatch.setattr(chain_register, "own_wallet", lambda: type("W", (), {"address": "0x" + owned})())
    linked = []
    monkeypatch.setattr(card, "_link_own_wallet", lambda *a, **k: linked.append(a[3:]) or 0)
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: pytest.fail("the person's wallet path must not run"))
    assert minted_chain is not None
    assert card.register_8004(target.name, "base-sepolia", REAL["receipt"]["transactionHash"]) == 0
    out = capsys.readouterr().out
    assert "signs the link itself" in out and linked == [("10727", False, False)]


class _As:
    """`minted` fills a FakeReg's attributes; here they land on our Chain."""

    def __init__(self, chain):
        object.__setattr__(self, "_c", chain)

    def __setattr__(self, k, v):
        setattr(self._c, k, v)

    def __getattr__(self, k):
        return getattr(self._c, k)


def test_link_from_the_agent_wallet_checks_the_chain_and_signs_with_the_wallet(chain, wallet, target):
    owned_by_wallet(chain, wallet)
    chain.block_number = 1_000
    out = chain_link.own_wallet_ask("base-sepolia", AID, read=chain, stable=STABLE, now=NOW)
    assert out["tier"] == "owner" and out["signer"] == wallet.address and out["signature"].startswith("0x")
    link = {"agent_registry": out["agent_registry"], "agent_id": str(AID), "signer": wallet.address, "stable_id": STABLE,
            "issued_at": out["issued_at"], "agent_name": out["agent_name"], "wallet_sig": out["signature"]}
    assert erc8004.signature_recovers(link) is True
    chain.owners[AID] = OWNER
    again = chain_link.own_wallet_ask("base-sepolia", AID, read=chain, stable=STABLE, now=NOW)
    assert again["tier"] == "" and "signature" not in again, "a wallet that does not own it signs nothing"
    with pytest.raises(chain_link.LinkError, match="does not back"):
        chain_link.link_own_wallet("base-sepolia", AID, read=chain, stable=STABLE, now=NOW)


# -- §2 setting the identity's agentWallet -----------------------------------------------------------------------
def test_build_signs_the_consent_with_the_agent_wallet_and_proves_it_by_simulating_from_the_owner(chain, wallet, target):
    out = chain_wallet.build("base-sepolia", AID, now=NOW)
    assert out["owner"] == OWNER and out["new_wallet"] == wallet.address and out["already"] is False
    assert out["owner_is_agent_wallet"] is False and out["deadline"] == NOW + W.DEADLINE_SECONDS
    sim = [c for c in chain.calls if c[0] == "simulate"][0]
    assert sim[1] == OWNER, "simulated from the owner"
    assert bytes.fromhex(out["calldata"][2:]) == sim[2] and out["calldata"].startswith(out["selector"])
    td = wallet.signed_typed[0]
    assert td["primaryType"] == "AgentWalletSet" and td["message"]["owner"] == OWNER and td["message"]["newWallet"] == wallet.address
    assert td["domain"]["verifyingContract"] == REGISTRY and td["domain"]["chainId"] == 84532


def test_the_registry_refusing_the_simulation_means_nothing_is_offered_to_send(chain, wallet, target):
    chain.revert = True
    with pytest.raises(chain_link.LinkError, match="would refuse"):
        chain_wallet.build("base-sepolia", AID, now=NOW)
    chain.revert, chain.down = False, True
    with pytest.raises(chain_link.LinkError, match="could not be asked"):
        chain_wallet.build("base-sepolia", AID, now=NOW)


def test_an_unknown_agent_or_a_bad_number_and_an_identity_that_already_names_the_wallet(chain, wallet, target):
    with pytest.raises(chain_link.LinkError, match="does not exist"):
        chain_wallet.build("base-sepolia", 999_999, now=NOW)
    with pytest.raises(chain_link.LinkError, match="decimal integer"):
        chain_wallet.build("base-sepolia", "abc", now=NOW)
    chain.wallets[AID] = wallet.address.lower()
    assert chain_wallet.build("base-sepolia", AID, now=NOW)["already"] is True


def test_a_wallet_that_cannot_sign_the_consent_says_so_without_its_words(chain, monkeypatch, target):
    evil = Signer(fail_with=f"key {KEY}")
    with pytest.raises(chain_link.LinkError) as e:
        chain_wallet.build("base-sepolia", AID, signer=evil, now=NOW)
    no_key(e.value)


def test_only_the_owner_sends_so_the_agent_wallet_sends_only_when_it_owns_the_identity(chain, wallet, target):
    with pytest.raises(chain_link.LinkError, match="only the owner sends this"):
        chain_wallet.quote("base-sepolia", AID, 400_000, now=NOW)
    with pytest.raises(chain_link.LinkError, match="--max-gas is required"):
        owned_by_wallet(chain, wallet)
        chain_wallet.quote("base-sepolia", AID, 0, now=NOW)
    assert chain.sent == []


def test_the_agent_wallet_as_owner_signs_the_consent_and_sends_one_transaction(chain, wallet, target):
    owned_by_wallet(chain, wallet)
    chain.wallets[AID] = "0x" + "99" * 20       # the identity points elsewhere now
    cq = chain_wallet.quote("base-sepolia", AID, 400_000, now=NOW)["quote"]
    out = chain_wallet.send("base-sepolia", AID, 400_000, wallet.address, cq, now=NOW)
    assert len(chain.sent) == 1 and out["wallet"] == wallet.address
    tx = wallet.signed_tx[0]
    assert tx["to"] == REGISTRY and tx["data"].startswith("0x2d1ef5ae") and tx["gas"] <= 400_000
    with pytest.raises(chain_link.LinkError, match="not the wallet you confirmed"):
        chain_wallet.send("base-sepolia", AID, 400_000, "0x" + "11" * 20, cq, now=NOW)
    assert len(chain.sent) == 1


def wallet_receipt(chain, wallet, tx="0x" + "ab" * 32):
    rec = copy.deepcopy(REAL["receipt"])
    rec["logs"] = [l for l in rec["logs"] if l["topics"][0] == W.TOPIC_METADATA_SET]
    log = rec["logs"][0]
    from eth_abi import encode
    from eth_utils import keccak
    log["topics"] = [W.TOPIC_METADATA_SET, "0x" + AID.to_bytes(32, "big").hex(), "0x" + keccak(text="agentWallet").hex()]
    log["data"] = "0x" + encode(["string", "bytes"], ["agentWallet", bytes.fromhex(wallet.address[2:])]).hex()
    rec["transactionHash"] = tx
    blk = int(rec["blockNumber"], 16)
    chain.block_number = blk + 5
    chain.block_hashes[blk] = rec["blockHash"].lower()
    chain.receipts[tx] = rec
    chain.wallets[AID] = wallet.address.lower()
    return tx


def test_confirm_reads_the_receipt_then_getagentwallet_and_says_whether_it_is_the_agents_own(chain, wallet, target):
    tx = wallet_receipt(chain, wallet)
    out = chain_wallet.confirm("base-sepolia", AID, tx)
    assert out["agent_wallet"] == wallet.address.lower() and out["is_own_wallet"] is True and out["set_by_tx"] == wallet.address.lower()
    chain.wallets[AID] = "0x" + "77" * 20            # moved on since
    assert chain_wallet.confirm("base-sepolia", AID, tx)["is_own_wallet"] is False
    chain.block_number = int(chain.receipts[tx]["blockNumber"], 16) - 1
    with pytest.raises(chain_link.LinkError, match="safe block"):
        chain_wallet.confirm("base-sepolia", AID, tx)
    with pytest.raises(chain_link.LinkError, match="not mined yet"):
        chain_wallet.confirm("base-sepolia", AID, "0x" + "cc" * 32)
    with pytest.raises(chain_link.LinkError, match="transaction hash"):
        chain_wallet.confirm("base-sepolia", AID, "0x12")


def test_set_wallet_prints_the_call_and_the_deadline_and_sends_nothing(chain, wallet, target, monkeypatch, capsys):
    monkeypatch.setattr(chain, "chain_time", lambda: NOW)
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID)) == 0
    out = capsys.readouterr().out
    for want in (OWNER, wallet.address, "the registry would accept it from the owner", "valid until", "0x2d1ef5ae",
                 "chain id 84532", "--browser", "--tx 0xHASH", "not proof of control"):
        assert want in out, want
    assert chain.sent == [] and wallet.signed_tx == []
    no_key(out)


def test_set_wallet_says_so_when_the_identity_already_names_the_wallet(chain, wallet, target, capsys):
    chain.wallets[AID] = wallet.address.lower()
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID)) == 0
    assert "nothing to do" in capsys.readouterr().out and chain.sent == []


def test_set_wallet_from_the_agent_wallet_is_refused_when_someone_else_owns_it(chain, wallet, target, terminal, capsys):
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), from_agent_wallet=True, max_gas=400_000) == 1
    assert "only the owner sends this" in capsys.readouterr().out and chain.sent == []


def test_set_wallet_from_the_agent_wallet_asks_sends_once_and_reads_the_result(chain, wallet, target, terminal, monkeypatch, capsys):
    owned_by_wallet(chain, wallet)
    chain.wallets[AID] = "0x" + "99" * 20
    seen = []
    monkeypatch.setattr(card, "_wallet_after", lambda *a, **k: seen.append(a[4]) or 0)
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), from_agent_wallet=True, max_gas=400_000) == 0
    out = capsys.readouterr().out
    assert len(chain.sent) == 1 and len(terminal) == 1 and seen and seen[0].startswith("0x") and "Sent transaction" in out
    no_key(out)


def test_set_wallet_tx_reports_getagentwallet_at_the_safe_block(chain, wallet, target, capsys):
    tx = wallet_receipt(chain, wallet)
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), tx) == 0
    out = capsys.readouterr().out
    assert "paid at its own wallet" in out and "safe block" in out and "not proof of control" in out
    chain.wallets[AID] = "0x" + "77" * 20
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), tx) == 1
    assert "not " + target.name + "'s own wallet" in capsys.readouterr().out.replace("That is not", "not").replace("not not", "not")


def test_set_wallet_flag_combinations(chain, wallet, target, capsys):
    tx = "0x" + "ab" * 32
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), tx, browser=True) == 1
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), max_gas=5) == 1
    assert card.set_8004_wallet(target.name, "base-sepolia", str(AID), wait=True) == 1
    assert card.set_8004_wallet(target.name, "", str(AID)) == 1
    assert card.set_8004_wallet(target.name, "base-sepolia", "") == 1
    capsys.readouterr()
    assert chain.sent == []


# -- the key never shows ------------------------------------------------------------------------------------------
def test_the_private_key_is_in_no_output_log_or_error_across_every_flow(chain, wallet, target, terminal, monkeypatch, capsys, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    owned = Signer()
    seen = []
    monkeypatch.setattr(card, "_register_after", lambda *a, **k: 0)
    card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000)
    card.register_8004(target.name, "base-sepolia", from_agent_wallet=True)
    chain.balance_wei = 1
    card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000)
    chain.balance_wei = 10**17
    chain.send_error = chain_read.ChainUnknown("boom")
    card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000)
    chain.send_error = None
    owned_by_wallet(chain, owned)
    monkeypatch.setattr(card, "_wallet_after", lambda *a, **k: 0)
    card.set_8004_wallet(target.name, "base-sepolia", str(AID), from_agent_wallet=True, max_gas=400_000)
    card.set_8004_wallet(target.name, "base-sepolia", str(AID))
    # a helper that crashes with the key in its own words: only the kind and a redacted tail reach the person
    import subprocess
    crashed = subprocess.CompletedProcess([], 1, "", f"Traceback...\nValueError: {KEY[2:]} and 0x{KEY[2:]}")
    seen.append(card._result(crashed))
    cap = capsys.readouterr()
    no_key(cap.out, cap.err, caplog.text, seen)
    assert "[hex removed]" in seen[0]["error"]


# -- person-only: the key guard, no agent tool, no money path ------------------------------------------------------
def test_the_terminal_guard_blocks_the_new_commands_for_an_agent():
    from hermes_plugin.emporia import key_guard as g
    for cmd in ("emporia agent register-8004 shop --chain base-sepolia --from-agent-wallet --max-gas 400000 --yes",
                "emporia agent set-8004-wallet shop --chain base-sepolia --agent-id 5",
                "emporia agent set-8004-wallet shop --chain base-sepolia --agent-id 5 --from-agent-wallet --max-gas 300000 --yes",
                "emporia agent link-8004 shop --chain base-sepolia --agent-id 5 --from-agent-wallet --yes",
                "hermes emporia agent set-8004-wallet shop --chain base-sepolia --agent-id 5 --browser"):
        assert g.pre_tool_call("terminal", {"command": cmd})["action"] == "block", cmd


def test_no_agent_tool_mints_sets_a_wallet_or_sends_and_none_is_a_money_tool():
    from emporia import autonomy
    tools = {n for n in dir(mcp_server) if not n.startswith("__")}
    for n in tools:
        low = n.lower()
        assert not ("8004" in low and any(w in low for w in ("register", "set_", "mint", "send", "wallet"))), n
    assert not any("8004" in t or "agent_wallet" in t for t in autonomy.MONEY_TOOLS | autonomy.WRITE_TOOLS)


def test_the_send_modules_are_only_reachable_from_the_person_run_commands():
    """No MCP server, route, console module, plugin file or other module imports or calls the send and own-wallet
    functions; only the CLI's child code (agent_card_cli) and the modules that define them do."""
    import re
    base = pathlib.Path(__file__).resolve().parent.parent
    allowed = {"chain_send.py", "chain_register.py", "chain_wallet.py", "chain_link.py", "agent_card_cli.py", "key_guard.py"}
    use = re.compile(r"import[^\n]*\b(?:chain_send|chain_wallet)\b|\b(?:chain_send|chain_wallet)\.\w|\bsend_register\s*\(|"
                     r"\blink_own_wallet\s*\(|\bown_wallet(?:_ask)?\s*\(|\.send_raw\s*\(")
    files = [f for root in ("src/emporia", "hermes_plugin") for f in (base / root).rglob("*.py")]
    assert len(files) > 100
    for f in files:
        if f.name in allowed or "/ui/" in str(f):
            continue
        for line in f.read_text().splitlines():
            if line.lstrip().startswith("#") or '"""' in line:
                continue
            assert not use.search(line), f"{f.name}: {line.strip()}"


# -- the security review's fixes ------------------------------------------------------------------------------------
def test_yes_never_answers_a_send_from_the_agent_wallet_and_no_hint_offers_it(chain, wallet, target, monkeypatch, capsys):
    """B1: no terminal + --yes, no Hermes variables: nothing is signed or sent, on all three commands."""
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: pytest.fail("there is no one to ask"))
    owned_by_wallet(chain, wallet)
    chain.wallets[AID] = "0x" + "99" * 20
    for fn in (lambda: card.register_8004(target.name, "base-sepolia", yes=True, from_agent_wallet=True, max_gas=900_000),
               lambda: card.set_8004_wallet(target.name, "base-sepolia", str(AID), yes=True, from_agent_wallet=True, max_gas=900_000),
               lambda: card.link_8004(target.name, "base-sepolia", str(AID), yes=True, from_agent_wallet=True)):
        assert fn() == 1
        out = capsys.readouterr().out
        assert "your own terminal" in out and "add --yes" not in out and "or add --yes" not in out
    assert chain.sent == [] and wallet.signed_tx == [] and wallet.signed_typed == []


def test_with_a_terminal_yes_still_does_not_skip_the_send_question(chain, wallet, target, monkeypatch, capsys):
    from emporia import cli
    asked = []
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, d="": asked.append(q) or "n")
    monkeypatch.setattr(card, "_register_after", lambda *a, **k: 0)
    assert card.register_8004(target.name, "base-sepolia", yes=True, from_agent_wallet=True, max_gas=900_000) == 0
    assert len(asked) == 1 and chain.sent == []


def _raise_base(chain, monkeypatch, factor):
    """The first fee read is the quote the person sees; every later one has the base fee `factor` times higher."""
    real, n = chain.fee_data, {"n": 0}

    def fee():
        n["n"] += 1
        p, b = real()
        return (p, b) if n["n"] == 1 else (p, b * factor)
    monkeypatch.setattr(chain, "fee_data", fee)
    return n


def test_a_base_fee_above_the_confirmed_max_fee_is_refused_not_signed(chain, wallet, target, monkeypatch, capsys):
    """S1 (the review's probe): the second quote sees a base fee 4000 times higher."""
    from emporia import cli
    n = _raise_base(chain, monkeypatch, 4000)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: "y")
    chain.balance_wei = 10**18
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=900_000) == 1
    assert "above the max fee" in capsys.readouterr().out and chain.sent == []
    owned_by_wallet(chain, wallet)
    n["n"] = 0
    cq = chain_wallet.quote("base-sepolia", AID, 900_000, now=NOW)["quote"]
    with pytest.raises(chain_link.LinkError, match="above the max fee"):
        chain_wallet.send("base-sepolia", AID, 900_000, wallet.address, cq, now=NOW)
    assert chain.sent == []


def test_a_rise_inside_the_confirmed_max_fee_is_signed_with_the_confirmed_limit_and_fee(chain, wallet, target, monkeypatch):
    """R1: base fee 5 -> 9 million wei: the confirmed max fee (priority + 2 x 5 million) still covers it. The
    transaction carries the confirmed gas limit and max fee, not the fresh ones."""
    q = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)["quote"]
    chain.base = 9_000_000
    fresh = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)["quote"]
    assert fresh["max_fee_per_gas"] > q["max_fee_per_gas"] and fresh["base_fee"] <= q["max_fee_per_gas"]
    out = chain_register.send_register("base-sepolia", 400_000, chain_register.quote_register(
        "base-sepolia", 400_000, stable=STABLE)["calldata_digest"], q, stable=STABLE)
    tx = wallet.signed_tx[-1]
    assert len(chain.sent) == 1 and tx["gas"] == q["gas_limit"] and tx["maxFeePerGas"] == q["max_fee_per_gas"]
    assert tx["maxPriorityFeePerGas"] == q["priority_fee_per_gas"] and out["max_cost_wei"] == q["max_cost_wei"]


def test_the_confirmed_quote_must_be_whole_and_the_call_must_still_fit_its_gas_limit(chain, wallet, target):
    q = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    digest = q["calldata_digest"]
    for bad in ({}, None, {**q["quote"], "max_cost_wei": 1}, {**q["quote"], "gas_limit": 0}):
        with pytest.raises(chain_link.LinkError, match="confirmed cost"):
            chain_register.send_register("base-sepolia", 400_000, digest, bad, stable=STABLE)
    chain.gas = 260_000                  # the call now needs more than the limit the person confirmed (250,000)
    with pytest.raises(chain_link.LinkError, match="over the limit"):
        chain_register.send_register("base-sepolia", 400_000, digest, q["quote"], stable=STABLE)
    assert chain.sent == [] and wallet.signed_tx == []


def test_the_send_guards_all_still_hold_with_the_pinned_quote(chain, wallet, target, monkeypatch, capsys):
    """person-only, a person's terminal, --yes, the cost cap, a pending nonce and Base Sepolia only."""
    q = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    # cost cap: --max-gas below the estimate refuses, and a confirmed quote on another chain id is not sent
    with pytest.raises(chain_link.LinkError, match="over your --max-gas"):
        chain_register.send_register("base-sepolia", 100_000, q["calldata_digest"], q["quote"], stable=STABLE)
    with pytest.raises(chain_link.LinkError, match="Base Sepolia"):
        chain_send.send(chain, wallet, {**q["quote"], "chain_id": 8453}, b"")
    # pending nonce
    chain.latest_nonce = chain.nonce_n - 1
    with pytest.raises(chain_link.LinkError, match="transaction waiting"):
        chain_register.send_register("base-sepolia", 400_000, q["calldata_digest"], q["quote"], stable=STABLE)
    chain.latest_nonce = None
    # person-only
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    with pytest.raises(chain_link.LinkError, match="person's"):
        chain_register.send_register("base-sepolia", 400_000, q["calldata_digest"], q["quote"], stable=STABLE)
    monkeypatch.delenv("HERMES_SESSION_ID")
    # a person's terminal is always needed, and --yes never answers the send
    from emporia import cli
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert card.register_8004(target.name, "base-sepolia", yes=True, from_agent_wallet=True, max_gas=400_000) == 1
    assert chain.sent == [] and wallet.signed_tx == []


def test_the_send_writes_its_hash_to_stderr_before_it_broadcasts(chain, wallet, capsys):
    q = quote(chain)
    q["sender"] = wallet.address
    order = []
    real = chain.send_raw
    chain.send_raw = lambda raw: order.append(capsys.readouterr().err) or real(raw)
    h = chain_send.send(chain, wallet, q, b"")
    assert order and order[0].strip() == chain_send.HASH_MARK + h


def test_a_timed_out_send_prints_the_hash_it_read_from_stderr_with_the_wallet(chain, wallet, target, terminal, monkeypatch, capsys):
    import subprocess
    h = "0x" + "ab" * 32
    for err in (f"noise\n{chain_send.HASH_MARK}{h}\n".encode(), f"{chain_send.HASH_MARK}{h}\n"):
        def boom(code, _err=err):
            raise subprocess.TimeoutExpired("x", 120, output=b"", stderr=_err)
        monkeypatch.setattr(target, "run", boom)
        monkeypatch.setattr(card, "_register_after", lambda *a, **k: 0)
        # the quote step also goes through run: let it through, time out only the send
        real_run = type(target).run

        def pick(code, _boom=boom, _real=real_run):
            return _boom(code) if "send_register" in code else _real(target, code)
        monkeypatch.setattr(target, "run", pick)
        assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 1
        out = capsys.readouterr().out
        assert f"--tx {h}" in out and "The wallet is 0x" in out and "may have been sent" in out
    # a timeout with no hash line says only what it did before
    monkeypatch.setattr(target, "run", lambda code: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 120)) if "send_register" in code else real_run(target, code))
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 1
    assert "--tx 0x" not in capsys.readouterr().out


def test_the_functions_refuse_an_agent_session_and_a_live_unattended_turn_themselves(chain, wallet, target, monkeypatch):
    """S2: calling them directly, as `python -c` would."""
    q = chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    calls = (lambda: chain_send.send(chain, wallet, q["quote"], b""),
             lambda: chain_register.send_register("base-sepolia", 400_000, q["calldata_digest"], q["quote"], stable=STABLE),
             lambda: chain_register.own_wallet(),
             lambda: chain_wallet.build("base-sepolia", AID, now=NOW),
             lambda: chain_wallet.send("base-sepolia", AID, 400_000, OWNER, q["quote"], now=NOW),
             lambda: chain_link.link_own_wallet("base-sepolia", AID, read=chain, stable=STABLE, now=NOW),
             lambda: chain_link.own_wallet_ask("base-sepolia", AID, read=chain, stable=STABLE, now=NOW))
    for call in calls:
        with pytest.raises(chain_link.LinkError, match="person's"):
            call()
    monkeypatch.delenv("HERMES_SESSION_ID")
    from emporia import autonomy
    monkeypatch.setattr(autonomy, "current", lambda now=None: type("T", (), {"origin": "relay"})())
    for call in calls:
        with pytest.raises(chain_link.LinkError, match="relay turn is live"):
            call()
    assert chain.sent == [] and wallet.signed_tx == [] and wallet.signed_typed == []


def test_the_key_guard_blocks_direct_use_of_the_send_modules():
    """S2: the review's probe commands, as a terminal command and as code."""
    from hermes_plugin.emporia import key_guard as g
    cmds = ["python -c \"from emporia import chain_register as c; q=c.quote_register('base-sepolia', 900000); "
            "print(c.send_register('base-sepolia', 900000, q['calldata_digest']))\"",
            "python3 -c \"from emporia import chain_wallet as w; print(w.send('base-sepolia', 5, 300000, '0x00'))\"",
            "python -c \"from emporia import chain_link as c; print(c.link_own_wallet('base-sepolia', 5))\"",
            "python -c \"import emporia.chain_send as s\"",
            "python -c \"from emporia.chain_register import own_wallet; own_wallet()\"",
            "env -u HERMES_SESSION_ID emporia agent register-8004 shop --chain base-sepolia --from-agent-wallet --max-gas 400000 --yes",
            "python -m emporia.cli agent set-8004-wallet shop --chain base-sepolia --agent-id 5",
            "E=emporia; $E agent register-8004 shop --chain base-sepolia --from-agent-wallet --max-gas 400000 --yes"]
    for c in cmds:
        for tool, arg in (("terminal", "command"), ("execute_code", "code")):
            assert g.pre_tool_call(tool, {arg: c})["action"] == "block", (tool, c)
    for fine in ("git log --oneline", "python -c \"from emporia import chain_link\"", "ls -la"):
        assert (g.pre_tool_call("terminal", {"command": fine}) or {}).get("action", "allow") != "block", fine


def test_a_wallet_with_a_transaction_waiting_is_not_sent_from_and_a_child_timeout_says_it_may_have_been_sent(chain, wallet, target, monkeypatch, capsys):
    """S3."""
    import subprocess
    from emporia import cli
    chain.nonce_n, chain.latest_nonce = 8, 7
    with pytest.raises(chain_link.LinkError, match=rf"{wallet.address}.*transaction waiting|transaction waiting"):
        chain_register.quote_register("base-sepolia", 400_000, stable=STABLE)
    chain.latest_nonce = None
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: "y")
    real = target.run

    def slow(code):
        if "send_register" in code:
            raise subprocess.TimeoutExpired("x", 120)
        return real(code)
    monkeypatch.setattr(target, "run", slow)
    assert card.register_8004(target.name, "base-sepolia", from_agent_wallet=True, max_gas=400_000) == 1
    out = capsys.readouterr().out
    assert "may have been sent" in out and wallet.address in out and "mint twice" in out
    out = card._chain_run(target, "quote_register", "base-sepolia", 1, mod="chain_register")
    assert out.get("timeout") is None


def test_the_browser_page_ends_with_the_consent(chain, wallet, target, monkeypatch, capsys):
    """N6."""
    from emporia import browser_sign
    from emporia import cli
    seen = {}

    class Page:
        url, timeout = "http://127.0.0.1:1/x", 0
        port, listen, token = 1, "127.0.0.1", "x"

        def __init__(self, tx, **kw):
            seen.update(kw)
            self.timeout = kw["timeout"]

        def start(self):
            return self

        def wait(self):
            raise browser_sign.BrowserSignError("no signature arrived")
    monkeypatch.setattr(browser_sign, "SendPage", Page)
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda *a, **k: "y")
    card.set_8004_wallet(target.name, "base-sepolia", str(AID), browser=True)
    assert seen["timeout"] == float(W.DEADLINE_SECONDS)
