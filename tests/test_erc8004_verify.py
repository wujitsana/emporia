"""The ERC-8004 link verifier against an in-memory fake chain (no network, synthetic keys)."""
import builtins
import json
import pathlib

import pytest

from emporia import erc8004
from emporia.chain_read import ChainRevert, ChainUnknown
from emporia.networks import ERC8004_REGISTRIES

FX = json.loads((pathlib.Path(__file__).parent / "fixtures/erc8004/synthetic_chain.json").read_text())
CHAIN, REG = FX["chain"], ERC8004_REGISTRIES[FX["chain"]]
C = FX["contracts"]
AGENT, TOKEN = int(FX["agent_id"]), int(FX["token_id"])
needs_lib = pytest.mark.skipif(not erc8004.available(), reason="eth-account is not installed")


def w(a):  # an address as a 32-byte word
    return bytes.fromhex(a[2:].lower()).rjust(32, b"\x00")


def u(n):
    return n.to_bytes(32, "big")


class FakeChain:
    """Answers what the verifier asks, by contract and selector. `down` makes every read unknown."""

    def __init__(self, caip2=CHAIN):
        self.caip2, self.block_number, self.block_hash = caip2, 123, "0x" + "ab" * 32
        self.registry = ERC8004_REGISTRIES[caip2]
        self.owners, self.metadata, self.codes = {}, {}, set()
        self.bindings, self.controllers, self.erc721, self.ownable = {}, set(), {}, {}
        self.sig1271, self.down, self.reverting = {}, False, set()

    def code(self, address):
        if self.down:
            raise ChainUnknown("down")
        return b"\x60\x01" if address.lower() in {c.lower() for c in self.codes} else b""

    def call(self, to, data):
        if self.down:
            raise ChainUnknown("down")
        to, sel, body = to.lower(), data[:4], data[4:]
        S = erc8004
        if to == self.registry.lower():
            if sel == S.SEL_OWNER_OF:
                if int.from_bytes(body[:32], "big") not in self.owners:
                    raise ChainRevert("nonexistent")
                return w(self.owners[int.from_bytes(body[:32], "big")])
            if sel == S.SEL_GET_METADATA:
                from eth_abi import encode
                val = self.metadata.get(int.from_bytes(body[:32], "big"), b"")
                return encode(["bytes"], [val])
        if sel == S.SEL_IS_VALID_SIG and to in self.sig1271:
            mode = self.sig1271[to]
            if mode == "revert":
                raise ChainRevert("no")
            return (S.MAGIC_1271 if mode == "ok" else bytes.fromhex("ffffffff")) + b"\x00" * 28
        if to in self.reverting:
            raise ChainRevert("reverts")
        if sel == S.SEL_BINDING_OF and to in self.bindings:
            std, bound, token = self.bindings[to]
            return u(std) + w(bound) + u(token)
        if sel == S.SEL_IS_CONTROLLER:
            return u(1 if (to, int.from_bytes(body[:32], "big"), "0x" + body[44:64].hex()) in self.controllers else 0)
        if sel == S.SEL_OWNER_OF and to in self.erc721:
            return w(self.erc721[to][int.from_bytes(body[:32], "big")])
        if sel == S.SEL_OWNER and to in self.ownable:
            return w(self.ownable[to])
        raise ChainRevert("no such function")


def keypair():
    from eth_account import Account
    k = Account.create()
    return k, k.address


NAME = "alpha"


def sign(key, chain_id, registry, agent_id, signer, stable, issued=1_700_000_000, name=NAME):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    td = erc8004.agent_link_typed_data(chain_id, registry, agent_id, signer, stable, issued, name)
    return Account.sign_message(encode_typed_data(full_message=td), key.key).signature.hex()


def make_link(key, signer, agent_id=AGENT, stable=FX["stable_id"], chain_id=84532, registry=REG, sig=None, **kw):
    sig = sig if sig is not None else sign(key, chain_id, registry, agent_id, signer, stable)
    return {"agent_registry": f"eip155:{chain_id}:{registry.lower()}", "agent_id": str(agent_id), "signer": signer,
            "stable_id": stable, "issued_at": 1_700_000_000, "agent_name": NAME, "wallet_sig": sig, **kw}


def bound_chain(std=erc8004.STD_ERC721, holder=None, controller=True, meta=True):
    """An agent owned by a contract that binds it to something `holder` holds."""
    ch = FakeChain()
    ch.owners[AGENT] = C["owning_contract"]
    ch.codes.add(C["owning_contract"])
    if meta:
        ch.metadata[AGENT] = bytes.fromhex(C["owning_contract"][2:])
    if std == erc8004.STD_ERC721:
        ch.bindings[C["owning_contract"].lower()] = (std, C["bound_token"], TOKEN)
        ch.erc721[C["bound_token"].lower()] = {TOKEN: holder}
    elif std == erc8004.STD_ACCOUNT:
        ch.bindings[C["owning_contract"].lower()] = (std, holder, 0)
    else:
        ch.bindings[C["owning_contract"].lower()] = (std, C["ownable_target"], 0)
        ch.ownable[C["ownable_target"].lower()] = holder
    if controller:
        ch.controllers.add((C["owning_contract"].lower(), AGENT, holder.lower()))
    return ch


# -- selectors and the signed message ---------------------------------------------------------------
@needs_lib
def test_selectors_match_their_signatures():
    from eth_utils import keccak
    for text, sel in erc8004.SELECTORS.items():
        assert keccak(text=text)[:4] == sel


@needs_lib
def test_typed_data_shape_and_digest_are_stable():
    td = erc8004.agent_link_typed_data(84532, REG, "42", "0x" + "11" * 20, FX["stable_id"], 5, NAME)
    assert td["domain"] == {"name": "Emporia agent link", "version": "1", "chainId": 84532}
    assert "verifyingContract" not in td["domain"]
    assert td["message"]["agentRegistry"] == f"eip155:84532:{REG.lower()}"
    assert td["message"]["statement"] == ("Link ERC-8004 agent #42 on Base Sepolia (testnet) to the Emporia agent alpha. "
                                          "This moves no funds and grants no permission.")
    d = erc8004.agent_link_digest(td)
    assert len(d) == 32 and d == erc8004.agent_link_digest(td)
    td2 = erc8004.agent_link_typed_data(84532, REG, "43", "0x" + "11" * 20, FX["stable_id"], 5, NAME)
    assert erc8004.agent_link_digest(td2) != d


# -- tier owner -------------------------------------------------------------------------------------
@needs_lib
def test_eoa_owner_is_proven_with_block_pin():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    r = erc8004.verify(make_link(key, addr), ch, now=100)
    assert (r["status"], r["tier"], r["owner"]) == ("proven", "owner", addr.lower())
    assert (r["block_number"], r["block_hash"], r["checked_at"]) == (123, ch.block_hash, 100)
    assert "binding" not in r


@needs_lib
def test_lowercase_signer_and_prefixed_signature_work():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    link = make_link(key, addr.lower(), sig="0x" + sign(key, 84532, REG, AGENT, addr.lower(), FX["stable_id"]).removeprefix("0x"))
    assert erc8004.verify(link, ch)["tier"] == "owner"


@needs_lib
@pytest.mark.parametrize("mode,status", [("ok", "proven"), ("wrong", "invalid"), ("revert", "invalid")])
def test_contract_owner_uses_erc1271(mode, status):
    key, _ = keypair()  # any 65 bytes will do: the contract decides
    ch = FakeChain()
    acct = C["smart_account"]
    ch.codes.add(acct)
    ch.owners[AGENT] = acct
    ch.sig1271[acct.lower()] = mode
    r = erc8004.verify(make_link(key, acct), ch)
    assert r["status"] == status
    if status == "proven":
        assert r["tier"] == "owner"


@needs_lib
def test_erc1271_read_failure_is_unknown_not_invalid():
    key, _ = keypair()
    ch = FakeChain()
    acct = C["smart_account"]
    ch.codes.add(acct)
    ch.owners[AGENT] = acct

    def broken(to, data):
        raise ChainUnknown("flaky")
    ch.call = broken
    r = erc8004.verify(make_link(key, acct), ch)
    assert r["status"] == "unknown" and r["tier"] == ""


# -- bound owner ------------------------------------------------------------------------------------
@needs_lib
@pytest.mark.parametrize("std,name,token_key", [(erc8004.STD_ERC721, "erc721", True), (erc8004.STD_ACCOUNT, "account", False),
                                                (erc8004.STD_OWNABLE, "contract-ownable", False)])
def test_bound_owner_for_each_readable_binding(std, name, token_key):
    key, addr = keypair()
    ch = bound_chain(std, addr)
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"], r["owner"]) == ("proven", "bound-owner", C["owning_contract"].lower())
    assert r["binding"]["standard"] == name
    assert ("token_id" in r["binding"]) is token_key
    if token_key:
        assert r["binding"] == {"standard": "erc721", "address": C["bound_token"].lower(), "token_id": str(TOKEN)}


@needs_lib
def test_bound_token_id_above_uint64_stays_exact():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr)
    big = int(FX["big_token_id"])
    ch.bindings[C["owning_contract"].lower()] = (0, C["bound_token"], big)
    ch.erc721[C["bound_token"].lower()] = {big: addr}
    assert erc8004.verify(make_link(key, addr), ch)["binding"]["token_id"] == FX["big_token_id"]


@needs_lib
def test_agent_id_above_2_pow_53_stays_exact():
    key, addr = keypair()
    big = int(FX["big_agent_id"])
    ch = FakeChain()
    ch.owners[big] = addr
    assert erc8004.verify(make_link(key, addr, agent_id=FX["big_agent_id"]), ch)["tier"] == "owner"
    # a neighbouring id (what a float round trip would give) is a different agent
    ch.owners[big - 1] = "0x" + "99" * 20
    assert erc8004.verify(make_link(key, addr, agent_id=str(big - 1)), ch)["status"] == "not-proven"


@needs_lib
def test_stale_after_the_bound_token_moves():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr)
    assert erc8004.verify(make_link(key, addr), ch)["tier"] == "bound-owner"
    ch.erc721[C["bound_token"].lower()][TOKEN] = "0x" + "77" * 20  # sold
    ch.controllers.discard((C["owning_contract"].lower(), AGENT, addr.lower()))
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"]) == ("not-proven", "")


@needs_lib
def test_owning_contract_that_lies_is_capped_at_controller():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, "0x" + "77" * 20)       # the token is held by someone else
    ch.controllers.add((C["owning_contract"].lower(), AGENT, addr.lower()))  # yet it names the signer
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"]) == ("proven", "controller")
    assert "binding" not in r


@needs_lib
def test_bound_token_that_reverts_counts_as_not_agreeing():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr)
    ch.reverting.add(C["bound_token"].lower())
    ch.erc721.clear()
    assert erc8004.verify(make_link(key, addr), ch)["tier"] == "controller"


@needs_lib
def test_controller_tier_when_it_cannot_be_derived():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr, meta=False)  # no agent-binding metadata
    assert erc8004.verify(make_link(key, addr), ch)["tier"] == "controller"
    ch = bound_chain(erc8004.STD_ERC721, addr)
    ch.bindings[C["owning_contract"].lower()] = (7, C["bound_token"], 0)  # an admin-role binding
    assert erc8004.verify(make_link(key, addr), ch)["tier"] == "controller"


@needs_lib
def test_metadata_naming_another_contract_is_not_a_binding():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr)
    ch.metadata[AGENT] = bytes.fromhex(C["other_registry"][2:])
    assert erc8004.verify(make_link(key, addr), ch)["tier"] == "controller"


@needs_lib
def test_approvals_and_non_controllers_do_not_count():
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr, controller=False)
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"]) == ("not-proven", "")
    ch2 = FakeChain()
    ch2.owners[AGENT] = "0x" + "55" * 20   # an EOA owner, signer is someone else
    assert erc8004.verify(make_link(key, addr), ch2)["status"] == "not-proven"


@needs_lib
def test_missing_agent_is_not_proven():
    key, addr = keypair()
    assert erc8004.verify(make_link(key, addr), FakeChain())["status"] == "not-proven"


# -- refusals ---------------------------------------------------------------------------------------
@needs_lib
def test_non_canonical_registry_is_refused():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    r = erc8004.verify(make_link(key, addr, registry=C["other_registry"]), ch)
    assert r["status"] == "invalid" and "canonical" in r["reason"]


@needs_lib
def test_unlisted_chain_and_wrong_reader_chain_are_refused():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    link = make_link(key, addr)
    link["agent_registry"] = f"eip155:1:{REG.lower()}"
    assert erc8004.verify(link, ch)["status"] == "invalid"
    other = FakeChain(FX["other_chain"])
    assert erc8004.verify(make_link(key, addr), other)["status"] == "invalid"


@needs_lib
def test_signature_for_another_chain_id_does_not_verify():
    key, addr = keypair()
    ch = FakeChain(FX["other_chain"])
    ch.owners[AGENT] = addr
    reg8453 = ERC8004_REGISTRIES[FX["other_chain"]]
    wrong = sign(key, 84532, REG, AGENT, addr, FX["stable_id"])   # signed for the testnet
    r = erc8004.verify(make_link(key, addr, chain_id=8453, registry=reg8453, sig=wrong), ch)
    assert r["status"] == "invalid"
    assert erc8004.verify(make_link(key, addr, chain_id=8453, registry=reg8453), ch)["tier"] == "owner"


@needs_lib
def test_replay_for_another_agent_or_stable_id_fails():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = ch.owners[AGENT + 1] = addr
    link = make_link(key, addr)
    assert erc8004.verify({**link, "agent_id": str(AGENT + 1)}, ch)["status"] == "invalid"
    assert erc8004.verify({**link, "stable_id": FX["other_stable_id"]}, ch)["status"] == "invalid"
    assert erc8004.verify({**link, "issued_at": link["issued_at"] + 1}, ch)["status"] == "invalid"
    assert erc8004.verify(link, ch)["tier"] == "owner"


@needs_lib
def test_tampered_signature_and_other_signers_are_invalid():
    key, addr = keypair()
    other_key, other = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    good = sign(key, 84532, REG, AGENT, addr, FX["stable_id"])
    flipped = good[:-4] + ("0000" if good[-4:] != "0000" else "1111")
    assert erc8004.verify(make_link(key, addr, sig=flipped), ch)["status"] == "invalid"
    # a stranger's signature claiming the owner's address
    forged = sign(other_key, 84532, REG, AGENT, addr, FX["stable_id"])
    assert erc8004.verify(make_link(key, addr, sig=forged), ch)["status"] == "invalid"


@needs_lib
def test_high_s_and_64_byte_signatures_are_not_accepted():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    sig = bytes.fromhex(sign(key, 84532, REG, AGENT, addr, FX["stable_id"]))
    r, s, v = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"), sig[64]
    high = r.to_bytes(32, "big") + (erc8004.SECP256K1_N - s).to_bytes(32, "big") + bytes([v ^ 1])
    assert erc8004.verify(make_link(key, addr, sig=high.hex()), ch)["status"] == "invalid"
    assert erc8004.verify(make_link(key, addr, sig=sig[:64].hex()), ch)["status"] == "invalid"
    assert erc8004.verify(make_link(key, addr, sig=sig.hex()), ch)["tier"] == "owner"


@needs_lib
@pytest.mark.parametrize("field,value", [("agent_id", "-1"), ("agent_id", "1.5"), ("signer", "0x12"), ("stable_id", "abc"),
                                         ("issued_at", "5"), ("issued_at", -1), ("wallet_sig", "zz"),
                                         ("wallet_sig", "00" * 4097), ("agent_registry", "garbage")])
def test_malformed_links_are_invalid(field, value):
    key, addr = keypair()
    link = {**make_link(key, addr), field: value}
    assert erc8004.verify(link, FakeChain())["status"] == "invalid"
    assert erc8004.verify({k: v for k, v in make_link(key, addr).items() if k != field}, FakeChain())["status"] == "invalid"


# -- unknown, never invalid -------------------------------------------------------------------------
@needs_lib
def test_rpc_down_is_unknown():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    ch.down = True
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"]) == ("unknown", "")


@needs_lib
def test_pin_failure_is_unknown():
    key, addr = keypair()

    class NoPin(FakeChain):
        @property
        def block_number(self):
            raise ChainUnknown("no block")

        @block_number.setter
        def block_number(self, v):
            pass
    r = erc8004.verify(make_link(key, addr), NoPin())
    assert r["status"] == "unknown" and r["block_number"] is None


@needs_lib
def test_owner_read_failure_mid_verification_is_unknown():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = C["owning_contract"]
    ch.codes.add(C["owning_contract"])
    real = ch.call

    def flaky(to, data):
        if data[:4] == erc8004.SEL_IS_CONTROLLER:
            raise ChainUnknown("flaky")
        return real(to, data)
    ch.call = flaky
    assert erc8004.verify(make_link(key, addr), ch)["status"] == "unknown"


# -- without the signature library ------------------------------------------------------------------
def test_without_eth_account_the_result_is_not_checked(monkeypatch):
    real = builtins.__import__

    def hide(name, *a, **k):
        if name.split(".")[0] in ("eth_account", "eth_abi", "eth_utils"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", hide)
    assert not erc8004.available()
    link = {"agent_registry": f"eip155:84532:{REG.lower()}", "agent_id": "42", "signer": "0x" + "11" * 20,
            "stable_id": FX["stable_id"], "issued_at": 1, "wallet_sig": "00" * 65}
    r = erc8004.verify(link, FakeChain())
    assert r["status"] == "not-checked" and r["tier"] == ""


def test_module_has_no_hermes_or_web_imports():
    imports = [ln for ln in pathlib.Path(erc8004.__file__).read_text().splitlines() if ln.lstrip().startswith(("import ", "from "))]
    for banned in ("hermes", "fastapi", "starlette"):
        assert not any(banned in ln.lower() for ln in imports)


class _Watched(FakeChain):
    """A fake chain that records whether anything was read from it."""

    def __init__(self, *a, **k):
        self.reads = 0
        super().__init__(*a, **k)

    @property
    def block_number(self):
        self.reads += 1
        return 123

    @block_number.setter
    def block_number(self, v):
        pass


@needs_lib
def test_an_invalid_or_not_proven_link_has_no_tier():
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    bad = erc8004.verify(make_link(key, addr, sig="0x" + "11" * 65), ch)
    assert bad["status"] == "invalid" and bad["tier"] == ""
    assert erc8004.verify(make_link(key, addr, registry=C["other_registry"]), ch)["tier"] == ""
    other = FakeChain()
    other.owners[AGENT] = C["owning_contract"]
    r = erc8004.verify(make_link(key, addr), other)
    assert (r["status"], r["tier"]) == ("not-proven", "")


@needs_lib
def test_a_refused_link_reads_nothing_from_the_chain():
    key, addr = keypair()
    for link in ({"agent_registry": "nonsense"}, {**make_link(key, addr), "agent_id": "x"},
                 make_link(key, addr, registry=C["other_registry"]),
                 {**make_link(key, addr), "agent_registry": f"eip155:1:{REG.lower()}"}):
        ch = _Watched()
        r = erc8004.verify(link, ch)
        assert r["status"] == "invalid"
        assert ch.reads == 0 and r["block_number"] is None and r["block_hash"] == ""
    ch = _Watched()
    ch.owners[AGENT] = addr
    assert erc8004.verify(make_link(key, addr), ch)["block_number"] == 123


@needs_lib
def test_only_a_proven_link_has_a_tier():
    key, addr = keypair()
    other = FakeChain()
    other.owners[AGENT] = C["owning_contract"]
    assert erc8004.verify(make_link(key, addr), other)["tier"] == ""


class _EmptyOwner(FakeChain):
    def call(self, to, data):
        if to.lower() == self.registry.lower() and data[:4] == erc8004.SEL_OWNER_OF:
            return b""
        return super().call(to, data)


@needs_lib
def test_an_empty_owner_answer_is_unknown_without_code_at_the_registry_else_not_proven():
    key, addr = keypair()
    ch = _EmptyOwner()
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"]) == ("unknown", "")
    ch.codes.add(ch.registry)
    r = erc8004.verify(make_link(key, addr), ch)
    assert (r["status"], r["tier"]) == ("not-proven", "")


# -- what a wallet shows: readable statement and date, a real EIP-712 round trip ---------------------------------
@needs_lib
def test_the_wallet_shows_the_statement_the_date_and_the_named_fields_in_that_order():
    td = erc8004.agent_link_typed_data(84532, REG, "12", "0x" + "11" * 20, FX["stable_id"], 1_795_116_600, "alpha")
    names = [f["name"] for f in td["types"]["AgentLink"]]
    assert names == ["statement", "date", "agentRegistry", "agentId", "signer", "emporiaAgentKey", "issuedAt"]
    m = td["message"]
    assert m["statement"] == ("Link ERC-8004 agent #12 on Base Sepolia (testnet) to the Emporia agent alpha. "
                              "This moves no funds and grants no permission.")
    assert m["date"] == erc8004.date_text(1_795_116_600) and m["date"].endswith(" UTC") and len(m["date"]) == 20
    assert m["agentRegistry"] == f"eip155:84532:{REG.lower()}" and m["issuedAt"] == 1_795_116_600


@needs_lib
def test_a_wallet_json_round_trip_signs_and_the_relay_path_verifies_it_exactly_as_signed():
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    key, addr = keypair()
    ch = FakeChain()
    ch.owners[AGENT] = addr
    td = erc8004.agent_link_typed_data(84532, REG, AGENT, addr, FX["stable_id"], 1_700_000_000, NAME)
    wire_json = erc8004.typed_data_json(td)               # what the page hands the wallet (eth_signTypedData_v4)
    back = erc8004.typed_data_from_json(wire_json)
    sig = Account.sign_message(encode_typed_data(full_message=back), key.key).signature.hex()
    assert erc8004.verify(make_link(key, addr, sig=sig), ch)["tier"] == "owner"
    # the rename of the agent later does not matter: the stored name rebuilds the digest; a different name does not
    assert erc8004.verify(make_link(key, addr, sig=sig, agent_name="beta"), ch)["status"] == "invalid"
    for bad in ("", "x" * 81, "line\nbreak"):
        assert erc8004.verify(make_link(key, addr, sig=sig, agent_name=bad), ch)["status"] == "invalid", bad
    assert erc8004.signature_recovers(make_link(key, addr, sig=sig)) is True


@needs_lib
def test_the_date_is_the_issued_time_formatted_so_a_wallet_reader_and_the_math_agree():
    assert erc8004.date_text(0) == "1970-01-01 00:00 UTC"
    assert erc8004.date_text(1_700_000_000) == "2023-11-14 22:13 UTC"
    a = erc8004.agent_link_typed_data(84532, REG, "1", "0x" + "11" * 20, FX["stable_id"], 1_700_000_000, NAME)
    b = erc8004.agent_link_typed_data(84532, REG, "1", "0x" + "11" * 20, FX["stable_id"], 1_700_000_060, NAME)
    assert erc8004.agent_link_digest(a) != erc8004.agent_link_digest(b)
